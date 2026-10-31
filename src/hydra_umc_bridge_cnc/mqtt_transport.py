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
  hydra/bridges/cnc/mtconnect                <- published, RETAINED, only when switched on (HYDRA_UMC_CNC_MTCONNECT=1): the same state as MTConnect observations (avail, execution, estop, door, spindle_speed, path_feedrate, system)
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
import os
import re
import threading
import time
from dataclasses import asdict
from typing import Callable

from hydra_umc_sdk.bridge_contract import CellState, JobPhase, MachineState, decision_to_dict
from hydra_umc_sdk.job_intake import IdempotencyStore, JobIntake
from hydra_umc_sdk.bridge_transport import MessageDispatcher as SharedMessageDispatcher
from hydra_umc_sdk.bridge_transport import MqttPublish, connect_with_retry, open_bridge_client  # noqa: F401  (re-exported)

from .cell import CncCellBridge, CncSnapshot, spindle_gate_from_environment
from .controller import ControllerLink, GrblController
from .mtconnect_publish import DEFAULT_DEVICE, observations_payload
from .serial_transport import SerialLike

TOPIC_PREFIX = "hydra/bridges/cnc/"
AVAILABILITY_TOPIC = f"{TOPIC_PREFIX}availability"
MTCONNECT_TOPIC = f"{TOPIC_PREFIX}mtconnect"
MTCONNECT_ENVIRONMENT = "HYDRA_UMC_CNC_MTCONNECT"

# A command is a few hundred bytes; anything larger is not a command, and parsing it is how a hostile payload costs the bridge its memory or its stack.
MAX_PAYLOAD_BYTES = 16 * 1024

# Handled on their own lane: a stop must never wait behind a status query.
STOP_VERBS = frozenset({"cmd/feed_hold", "cmd/soft_reset"})

_LOG = logging.getLogger(__name__)


def _snapshot_payload(snapshot: CncSnapshot, observed_at_ms: int) -> str:
    payload = asdict(snapshot)
    payload["machine_state"] = snapshot.machine_state().value
    # when this reading was taken: a retained state without it cannot be told from one that is hours old
    payload["observed_at"] = observed_at_ms
    return json.dumps(payload)


_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def request_id_of(payload: bytes) -> str | None:
    """The `request_id` a caller put in a JSON-object payload, when it is a short plain token; None otherwise (no payload, not JSON, not an object, no id, or an id that is not a plain token)."""
    if not payload or not payload.lstrip().startswith(b"{"):
        return None
    try:
        value = json.loads(payload).get("request_id")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and _REQUEST_ID.fullmatch(value) else None


def with_request_id(publishes: list[MqttPublish], request_id: str | None) -> list[MqttPublish]:
    """Adds `request_id` to every result the bridge publishes for a command, so that a caller with several commands in flight can tell which answer is which. The state topics are left alone."""
    if request_id is None:
        return publishes
    out: list[MqttPublish] = []
    for publish in publishes:
        if publish.topic.endswith("/result"):
            try:
                body = json.loads(publish.payload)
            except ValueError:
                body = None
            if isinstance(body, dict):
                body["request_id"] = request_id
                publish = MqttPublish(publish.topic, json.dumps(body), publish.retain, publish.qos)
        out.append(publish)
    return out


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
        connection: SerialLike | None,
        estop: Callable[[], bool],
        door_closed: Callable[[], bool],
        cell_state: Callable[[], CellState],
        idempotency: IdempotencyStore | None = None,
        *,
        clock: Callable[[], float] = time.time,
        controller: ControllerLink | None = None,
        spindle_gate: str | None = None,
        mtconnect: bool | None = None,
        mtconnect_device: str = DEFAULT_DEVICE,
    ) -> None:
        """``connection`` is the GRBL serial port; pass ``controller`` instead (a ``LinuxcncController``, a ``CncjsController``)
        for a machine that is not a GRBL board on a port of its own. ``spindle_gate`` is the mode of the spindle gate for the
        phases in which an arm reaches into the machine (see ``CncCellBridge``); when it is not given, it is read from
        ``HYDRA_UMC_CNC_SPINDLE_GATE`` (default ``when_known``). ``mtconnect`` switches on the retained MTConnect observations
        topic; when it is not given, it follows ``HYDRA_UMC_CNC_MTCONNECT=1`` (default off)."""

        if controller is None:
            if connection is None:
                raise ValueError("a serial connection or a controller link is required")
            controller = GrblController(connection)
        # docs/BRIDGE_CONTRACT.md: the idempotency key of a job is handled once; a repeat gets the answer that was given, nothing is evaluated again
        self._idempotency = idempotency
        # what arrives as a job goes through the shared intake: a malformed one is refused and a repeated key is answered from the store
        self._intake = JobIntake(idempotency)
        self._connection = connection
        self._estop = estop
        self._door_closed = door_closed
        self._cell_state = cell_state
        self._clock = clock
        # one status query at a time on the serial port: two readers would take each other's replies
        self._status_lock = threading.Lock()
        self._controller = controller
        self._gate = CncCellBridge(spindle_gate if spindle_gate is not None else spindle_gate_from_environment())
        self._mtconnect = mtconnect if mtconnect is not None else os.environ.get(MTCONNECT_ENVIRONMENT, "").strip() == "1"
        self._mtconnect_device = mtconnect_device

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
            return self._controller.read_snapshot(estop=self._estop, door_closed=self._door_closed)

    def handle_message(self, topic: str, payload: bytes, *, retained: bool = False) -> list[MqttPublish]:
        """Route one inbound message and put the `request_id` the caller sent (a plain token in a JSON payload) into each result, so answers can be matched to requests."""

        if retained or len(payload) > MAX_PAYLOAD_BYTES:
            return self._route(topic, payload, retained=retained)
        return with_request_id(self._route(topic, payload, retained=retained), request_id_of(payload))

    def _route(self, topic: str, payload: bytes, *, retained: bool = False) -> list[MqttPublish]:
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
            snapshot = self.refresh_status()
            now_ms = int(self._clock() * 1000)
            publishes = [MqttPublish(f"{TOPIC_PREFIX}state", _snapshot_payload(snapshot, now_ms), retain=True)]
            if self._mtconnect:
                publishes.append(MqttPublish(MTCONNECT_TOPIC, json.dumps(observations_payload(snapshot, now_ms, self._mtconnect_device)), retain=True))
            return publishes
        if suffix == "cmd/feed_hold":
            result = self._controller.feed_hold()
            answer = asdict(result)
            if result.executed:
                # GRBL does not answer "!": ask it what it is doing now, so that "sent" is not read as "stopped"
                self._confirm(answer)
            return [MqttPublish(f"{TOPIC_PREFIX}cmd/feed_hold/result", json.dumps(answer))]
        if suffix == "cmd/soft_reset":
            result = self._controller.soft_reset()
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
            result = self._controller.cycle_start_resume(self._cell_state(), snapshot)
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
            raw = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            decision = {"allowed": False, "reason": f"malformed job payload: {error}"}
            return MqttPublish(result_topic, json.dumps(decision))
        intake = self._intake.accept(raw if isinstance(raw, dict) else {})
        if intake.job is None:
            reason = intake.reason.removeprefix("malformed job: ")
            return MqttPublish(result_topic, json.dumps({"allowed": False, "reason": f"malformed job payload: {reason}"}))
        job = intake.job
        key = job.idempotency_key
        if intake.duplicate:
            stored = self._idempotency.recall(key) if self._idempotency is not None else None
            fallback = {"allowed": False, "reason": "this idempotency_key was already handled"}
            return MqttPublish(result_topic, stored if stored is not None else json.dumps(fallback))
        try:
            # -
            # same real gap as cycle_start_resume above: gating a new job
            # against a stale self._last_snapshot could let a door opened (or
            # E-STOP activated) since the last poll go unnoticed. Always
            # refresh_status() live before the gate decides.
            snapshot = self.refresh_status()
            gate_decision = self._gate.plan(job, self._cell_state(), snapshot)
            result = decision_to_dict(gate_decision)
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

    def _answer(self, key: str, publish: MqttPublish) -> MqttPublish:
        if self._idempotency is not None:
            self._idempotency.remember(key, publish.payload)
        return publish


class MessageDispatcher(SharedMessageDispatcher):
    """Takes the messages off the network thread: a status query waiting on the serial port must not hold up the MQTT keep-alive, and a stop must not wait behind anything.

    Ordinary commands run one at a time, in order, on one lane; the stop commands (`STOP_VERBS`) run on a second lane.
    """

    def __init__(self, bridge: object, stop_verbs: frozenset[str] = STOP_VERBS) -> None:
        super().__init__(bridge, TOPIC_PREFIX, stop_verbs)  # type: ignore[arg-type]


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

    dispatcher = MessageDispatcher(bridge)
    try:
        client = open_bridge_client(
            host,
            port,
            client_id=client_id,
            topic_prefix=TOPIC_PREFIX,
            dispatcher=dispatcher,
            username=username,
            password=password,
            max_connect_attempts=max_connect_attempts,
            connect_retry_delay_seconds=connect_retry_delay_seconds,
        )
        client.loop_forever()  # type: ignore[attr-defined]
    finally:
        dispatcher.close(wait=False)
