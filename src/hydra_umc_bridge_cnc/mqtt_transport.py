# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Real MQTT transport over HYDRA-UMC-MQTT-BROKER
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Reach this bridge's already-real logic over the real MQTT broker.

Every command this module can send is one `serial_transport.py` already
implements (`GrblRealtimeControl.feed_hold`/`soft_reset`/
`cycle_start_resume`) or `cell.py` already decides (`CncCellBridge.plan`
via the shared SDK's `evaluate_job`) - this module adds a new transport
(MQTT, per the ecosystem's own "MQTT via the real broker, real commands
included" decision), it does not grant any new physical authority. It
still never streams G-code: the same real-time-only boundary
`serial_transport.py` documents applies unchanged here.

`CncMqttBridge.handle_message()` is the one real place topic routing
happens, and it is a pure(ish) dispatcher over an already-open
`SerialLike` connection and 3 real signal callables (estop/door_closed/
cell_state) - fully testable with the exact same fakes
`test_serial_transport.py` already uses, no real MQTT broker or serial
port required. `run_forever()` is the thin real-I/O glue that lazily
imports `paho-mqtt` (this bridge's install docs already list it as
optional, matching `pyserial`'s own lazy-import boundary in
`serial_transport.py`) and is not itself unit-tested beyond import-time
behavior, same convention as `open_serial_port()`/`create_ros2_node()`
elsewhere in this ecosystem.

Topic scheme (published under HYDRA-UMC-MQTT-BROKER's own
`hydra/bridges/<name>/...` convention - see that repo's
`docs/BRIDGE_TOPICS.md`):
  hydra/bridges/cnc/state                    <- published, RETAINED (CncSnapshot + derived machine_state + observed_at, epoch milliseconds)
  hydra/bridges/cnc/availability             <- "online" / "offline", RETAINED (the broker publishes "offline" if this bridge dies)
  hydra/bridges/cnc/cmd/status               -> (empty) refresh + publish state
  hydra/bridges/cnc/cmd/feed_hold            -> (empty) real GRBL feed hold (the result says what the controller reported afterwards)
  hydra/bridges/cnc/cmd/soft_reset           -> (empty) real GRBL soft reset
  hydra/bridges/cnc/cmd/cycle_start_resume   -> (empty) real GRBL cycle start/resume
  hydra/bridges/cnc/cmd/job                  -> BridgeJob JSON (job_to_dict shape) - the shared bridge-contract gate (plan only: it never acts)
  hydra/bridges/cnc/cmd/<verb>/result        <- published, one JSON result per command above

feed_hold and soft_reset are handled on a lane of their own, so a status query waiting on the serial port never delays
them. A payload larger than 16 KiB, a payload that cannot be read and any unexpected failure while handling a message
are answered with a refusal on the result topic; they never end the loop.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Callable

from hydra_umc_sdk.bridge_contract import BridgeError, CellState, JobPhase, MachineState, decision_to_dict, job_from_dict
from hydra_umc_sdk.job_intake import IdempotencyStore
from hydra_umc_sdk.mqtt_connection import apply_connection_settings, settings_from_env

from .cell import CncCellBridge, CncSnapshot
from .serial_transport import GrblRealtimeControl, GrblSerialProbe, SerialLike

TOPIC_PREFIX = "hydra/bridges/cnc/"
AVAILABILITY_TOPIC = f"{TOPIC_PREFIX}availability"

# A command is a few hundred bytes; anything larger is not a command, and parsing it is how a hostile payload costs the bridge its memory or its stack.
MAX_PAYLOAD_BYTES = 16 * 1024

# Handled on their own lane: a stop must never wait behind a status query.
STOP_VERBS = frozenset({"cmd/feed_hold", "cmd/soft_reset"})

_LOG = logging.getLogger(__name__)


class MqttPublish:
    """One real outbound MQTT publish this module decided to make."""

    __slots__ = ("topic", "payload", "retain")

    def __init__(self, topic: str, payload: str, retain: bool = False) -> None:
        self.topic = topic
        self.payload = payload
        self.retain = retain

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, MqttPublish)
            and (self.topic, self.payload, self.retain) == (other.topic, other.payload, other.retain)
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"MqttPublish(topic={self.topic!r}, payload={self.payload!r}, retain={self.retain!r})"


def _snapshot_payload(snapshot: CncSnapshot, observed_at_ms: int) -> str:
    payload = asdict(snapshot)
    payload["machine_state"] = snapshot.machine_state().value
    # when this reading was taken: a retained state without it cannot be told from one that is hours old
    payload["observed_at"] = observed_at_ms
    return json.dumps(payload)


def _refusal(suffix: str, reason: str) -> list[MqttPublish]:
    """The answer to a command that could not be handled; nothing for a topic that is not a `cmd/<verb>`."""
    parts = suffix.split("/")
    if len(parts) != 2 or parts[0] != "cmd":
        return []
    return [MqttPublish(f"{TOPIC_PREFIX}{suffix}/result", json.dumps({"allowed": False, "executed": False, "reason": reason}))]


class CncMqttBridge:
    """Real command/telemetry dispatch for this bridge's MQTT topics.

    `estop`/`door_closed`/`cell_state` are callables, not fixed values -
    the real signals they read (an E-STOP line, a door sensor, the cell's
    own coordination state) can change between messages, and this bridge
    must always act on the current reading, never a stale one captured at
    construction time.
    """

    def __init__(
        self,
        connection: SerialLike,
        estop: Callable[[], bool],
        door_closed: Callable[[], bool],
        cell_state: Callable[[], CellState],
        idempotency: IdempotencyStore | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # docs/BRIDGE_CONTRACT.md: the idempotency key of a job is handled once; a repeat gets the answer that was given, nothing is evaluated again
        self._idempotency = idempotency
        self._connection = connection
        self._estop = estop
        self._door_closed = door_closed
        self._cell_state = cell_state
        self._clock = clock
        # one status query at a time on the serial port: two readers would take each other's replies
        self._status_lock = threading.Lock()
        self._probe = GrblSerialProbe()
        self._realtime = GrblRealtimeControl()
        self._gate = CncCellBridge()

    def refresh_status(self) -> CncSnapshot:
        """Query real GRBL status now.

        :
        this used to cache its result in `self._last_snapshot` for
        `cycle_start_resume`/`cmd/job` to reuse instead of querying again -
        a real door/E-STOP change between two messages went unnoticed
        until the next unrelated status poll happened to catch it.
        Deliberately no caching at all anymore: every command that can
        move the machine or start a job calls this fresh, immediately
        before deciding, every time - removed the field entirely rather
        than leaving unused dead state a future edit could be tempted to
        read from again.

        (a deeper version of the same real gap above
        already narrowed): the
        `self._estop`/`self._door_closed` callables are passed straight
        through to `query_status()` rather than called here - GRBL's own
        status query blocks on `readline()` for up to the connection's
        real timeout, and calling these here (before that block) would
        capture a pre-block reading a real E-STOP hit or door open DURING
        the wait could invalidate before this method even returns.
        `query_status()` itself calls them right after the blocking I/O
        completes, immediately before deciding.
        """

        with self._status_lock:
            return self._probe.query_status(self._connection, estop=self._estop, door_closed=self._door_closed)

    def handle_message(self, topic: str, payload: bytes, *, retained: bool = False) -> list[MqttPublish]:
        """Route one real inbound MQTT message to the real command it names.

        An unrecognised topic (this bridge subscribes to `cmd/#`, a
        wildcard) is silently ignored rather than erroring - a future
        sibling topic under `cmd/` this version does not know about yet
        must never crash the whole message loop.

        `retained` is True when the broker delivered this message
        because of the MQTT retain flag (a `mosquitto_pub -r`-style
        mistake, or any publisher that set it), not because a client just
        published it live. `on_connect()`'s own `subscribe("cmd/#")`
        replays every currently-retained message on that wildcard the
        instant it (re)subscribes - which happens on every reconnect, not
        just once at startup - so a retained `cmd/job` or (on the printer
        bridge) `cmd/start`/`cmd/resume` would otherwise re-trigger a real
        physical action every single time this bridge reconnects, with no
        new operator intent behind it at all. A retained delivery is
        always ignored here, before it ever reaches a real command.

        This never raises: an oversized payload and any failure while handling
        the message come back as a refusal on the verb's result topic.
        """

        if retained:
            return []
        if not topic.startswith(TOPIC_PREFIX):
            return []
        suffix = topic[len(TOPIC_PREFIX) :]
        if len(payload) > MAX_PAYLOAD_BYTES:
            return _refusal(suffix, f"payload is larger than {MAX_PAYLOAD_BYTES} bytes")
        try:
            return self._dispatch(suffix, payload)
        except Exception as error:  # noqa: BLE001 - nothing a message does may end the loop
            _LOG.exception("could not handle %s", topic)
            return _refusal(suffix, f"the command could not be handled: {type(error).__name__}")

    def _dispatch(self, suffix: str, payload: bytes) -> list[MqttPublish]:
        if suffix == "cmd/status":
            return [MqttPublish(f"{TOPIC_PREFIX}state", _snapshot_payload(self.refresh_status(), int(self._clock() * 1000)), retain=True)]
        if suffix == "cmd/feed_hold":
            result = self._realtime.feed_hold(self._connection)
            answer = asdict(result)
            if result.executed:
                # GRBL does not answer "!": ask it what it is doing now, so that "sent" is not read as "stopped"
                self._confirm(answer)
            return [MqttPublish(f"{TOPIC_PREFIX}cmd/feed_hold/result", json.dumps(answer))]
        if suffix == "cmd/soft_reset":
            result = self._realtime.soft_reset(self._connection)
            return [MqttPublish(f"{TOPIC_PREFIX}cmd/soft_reset/result", json.dumps(asdict(result)))]
        if suffix == "cmd/cycle_start_resume":
            # this used to reuse self._last_snapshot - whatever
            # was last queried, possibly from a `cmd/status` poll seconds
            # or minutes earlier - instead of re-reading the real,
            # current door/E-STOP state right before authorizing a real
            # motion command. Reproduced: door closed + no E-STOP at the
            # last poll, then the door opens and E-STOP activates, then
            # `cycle_start_resume` arrives - the stale snapshot still said
            # HOLDING (not FAULT), so the real b"~" byte was sent despite
            # the CURRENT physical state being unsafe. Fixed: always
            # refresh_status() live, immediately before deciding - never
            # a cached read for a command that actually moves the machine.
            snapshot = self.refresh_status()
            result = self._realtime.cycle_start_resume(self._connection, self._cell_state(), snapshot)
            return [MqttPublish(f"{TOPIC_PREFIX}cmd/cycle_start_resume/result", json.dumps(asdict(result)))]
        if suffix == "cmd/job":
            return [self._handle_job(payload)]
        return []

    def _confirm(self, answer: dict) -> None:
        """Add what the controller reported after a feed hold to `answer`."""
        try:
            after = self.refresh_status()
        except Exception:  # noqa: BLE001 - the hold was sent; failing to confirm it must not hide that
            answer["confirmed"] = False
            answer["controller_state_after"] = ""
            return
        answer["controller_state_after"] = after.controller_state
        answer["confirmed"] = after.machine_state() is MachineState.HOLDING

    def _handle_job(self, payload: bytes) -> MqttPublish:
        result_topic = f"{TOPIC_PREFIX}cmd/job/result"
        # A malformed job payload (bad JSON, missing field, unknown enum
        # value) must fail closed with a real, honest "not allowed"
        # decision on the same result topic a caller is already listening
        # to - never a raised exception that would kill the whole message
        # loop and silently stop this bridge from answering anything else.
        try:
            job = job_from_dict(json.loads(payload))
        except (json.JSONDecodeError, BridgeError, UnicodeDecodeError) as error:
            decision = {"allowed": False, "reason": f"malformed job payload: {error}"}
            return MqttPublish(result_topic, json.dumps(decision))
        key = f"job:{job.idempotency_key}"
        repeat = self._repeat(key, result_topic, {"allowed": False, "reason": "this idempotency_key was already handled"})
        if repeat is not None:
            return repeat
        try:
            # -
            # same real gap as cycle_start_resume above: gating a new job
            # against a stale self._last_snapshot could let a door opened (or
            # E-STOP activated) since the last poll go unnoticed. Always
            # refresh_status() live before the gate decides.
            snapshot = self.refresh_status()
            decision = self._gate.plan(job, self._cell_state(), snapshot)
            result = decision_to_dict(decision)
            result["mode"] = "gate-only"
            if job.phase is JobPhase.ABORT:
                # the shared gate always lets an abort through, but THIS topic only answers a question: it stops nothing
                result["forwarded"] = False
                result["reason"] = "abort acknowledged by the gate only - nothing was stopped; publish cmd/feed_hold to stop the machine"
            return self._answer(key, MqttPublish(result_topic, json.dumps(result)))
        except Exception as error:  # noqa: BLE001
            _LOG.exception("cmd/job failed")
            failure = {"allowed": False, "reason": f"the job could not be handled: {type(error).__name__}"}
            return self._answer(key, MqttPublish(result_topic, json.dumps(failure)))

    def _repeat(self, key: str, result_topic: str, fallback: dict) -> MqttPublish | None:
        """None when this job key is new (it is claimed now); otherwise the answer given the first time, which is sent again instead of running the job a second time."""
        if self._idempotency is None or self._idempotency.claim(key):
            return None
        stored = self._idempotency.recall(key)
        return MqttPublish(result_topic, stored if stored is not None else json.dumps(fallback))

    def _answer(self, key: str, publish: MqttPublish) -> MqttPublish:
        if self._idempotency is not None:
            self._idempotency.remember(key, publish.payload)
        return publish


class MessageDispatcher:
    """Takes the messages off the network thread: a status query waiting on the serial port must not hold up the MQTT keep-alive, and a stop must not wait behind anything.

    Ordinary commands run one at a time, in order, on one lane; the stop commands (`STOP_VERBS`) run on a second lane.
    """

    def __init__(self, bridge: CncMqttBridge, stop_verbs: frozenset[str] = STOP_VERBS) -> None:
        self._bridge = bridge
        self._stop_verbs = frozenset(stop_verbs)
        self._lane = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hydra-bridge")
        self._stop_lane = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hydra-bridge-stop")

    def on_message(self, client: object, userdata: object, message: object) -> None:
        if bool(getattr(message, "retain", False)):
            return  # a replay by the broker, never a request
        topic = message.topic  # type: ignore[attr-defined]
        suffix = topic[len(TOPIC_PREFIX) :] if topic.startswith(TOPIC_PREFIX) else ""
        lane = self._stop_lane if suffix in self._stop_verbs else self._lane
        lane.submit(self._handle, client, topic, bytes(message.payload))  # type: ignore[attr-defined]

    def _handle(self, client: object, topic: str, payload: bytes) -> None:
        try:
            for publish in self._bridge.handle_message(topic, payload, retained=False):
                client.publish(publish.topic, publish.payload, retain=publish.retain)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            _LOG.exception("could not answer %s", topic)

    def close(self, wait: bool = True) -> None:
        self._lane.shutdown(wait=wait)
        self._stop_lane.shutdown(wait=wait)


def connect_with_retry(
    connect: Callable[[], None],
    *,
    max_attempts: int = 5,
    retry_delay_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Retries `connect()` on OSError (what an unreachable broker actually
    raises - connection refused, timeout), tolerating the real startup
    race a systemd unit for this bridge hits if it starts before
    HYDRA-UMC-MQTT-BROKER is listening yet (both are independent systemd
    units with no ordering guarantee across a real reboot). Only OSError is retried;
    anything else is a real bug, not a transient startup race, and
    propagates immediately. `sleep` is injectable so tests can prove the
    retry/give-up behavior without a real multi-second wait."""
    last_error: OSError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            connect()
            return
        except OSError as error:
            last_error = error
            if attempt < max_attempts:
                sleep(retry_delay_seconds)
    raise RuntimeError(
        f"could not reach the MQTT broker after {max_attempts} attempts "
        f"({retry_delay_seconds}s apart): {last_error}"
    ) from last_error


def run_forever(
    bridge: CncMqttBridge,
    host: str,
    port: int = 1883,
    client_id: str = "hydra-umc-bridge-cnc",
    *,
    max_connect_attempts: int = 5,
    connect_retry_delay_seconds: float = 2.0,
    username: str | None = None,
    password: str | None = None,
) -> None:
    """Connect to a real HYDRA-UMC-MQTT-BROKER and dispatch forever.

    The only place this module imports paho-mqtt - lazily, so the rest of
    this module (and every test) works on a host without it installed.
    The initial connect is retried (see connect_with_retry()) - once
    connected, paho-mqtt's own loop_forever() already handles a
    mid-session drop/reconnect on its own, so only the first connect
    needed this.

    `username`/`password` are optional (matching
    HYDRA-UMC-MQTT-BROKER's own `MQTT_AUTH_JSON` authentication, which is
    itself opt-in) - a broker deployed with authentication required had no
    way to be reached from here at all before this. `password` is only
    meaningful together with `username`; a caller supplying `password`
    alone almost certainly meant to set both, so that combination is
    rejected rather than silently connecting unauthenticated.

    The bridge announces itself on `availability`: "online" once connected, and
    the broker publishes "offline" (the Last Will) if the connection is lost.
    """

    if password is not None and username is None:
        raise ValueError("password was given without a username - MQTT credentials need both")

    try:
        import paho.mqtt.client as mqtt  # type: ignore[import-untyped]
    except ImportError as error:
        raise RuntimeError(
            "paho-mqtt is not installed - install it to connect to a real HYDRA-UMC-MQTT-BROKER "
            "(this module's topic-dispatch/gating logic works and is tested without it)"
        ) from error

    def on_connect(client: object, userdata: object, flags: object, reason_code: object, properties: object = None) -> None:
        if getattr(reason_code, "is_failure", False):
            return  # the broker refused us: paho reports it and retries on its own
        client.subscribe(f"{TOPIC_PREFIX}cmd/#")  # type: ignore[attr-defined]
        client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)  # type: ignore[attr-defined]

    dispatcher = MessageDispatcher(bridge)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    # the credentials and TLS of this service's environment (HYDRA_MQTT_*), unless the caller gave a user name explicitly
    apply_connection_settings(client, settings_from_env(), username=username, password=password)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.will_set(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
    client.on_connect = on_connect
    client.on_message = dispatcher.on_message
    try:
        connect_with_retry(
            lambda: client.connect(host, port),
            max_attempts=max_connect_attempts,
            retry_delay_seconds=connect_retry_delay_seconds,
        )
        client.loop_forever()
    finally:
        dispatcher.close(wait=False)
