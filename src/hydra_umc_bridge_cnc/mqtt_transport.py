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
  hydra/bridges/cnc/state                    <- published, RETAINED (CncSnapshot + derived machine_state)
  hydra/bridges/cnc/cmd/status               -> (empty) refresh + publish state
  hydra/bridges/cnc/cmd/feed_hold            -> (empty) real GRBL feed hold
  hydra/bridges/cnc/cmd/soft_reset           -> (empty) real GRBL soft reset
  hydra/bridges/cnc/cmd/cycle_start_resume   -> (empty) real GRBL cycle start/resume
  hydra/bridges/cnc/cmd/job                  -> BridgeJob JSON (job_to_dict shape) - the shared bridge-contract gate
  hydra/bridges/cnc/cmd/<verb>/result        <- published, one JSON result per command above
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Callable

from hydra_umc_sdk.bridge_contract import BridgeError, CellState, decision_to_dict, job_from_dict

from .cell import CncCellBridge, CncSnapshot
from .serial_transport import GrblRealtimeControl, GrblSerialProbe, SerialLike

TOPIC_PREFIX = "hydra/bridges/cnc/"


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


def _snapshot_payload(snapshot: CncSnapshot) -> str:
    payload = asdict(snapshot)
    payload["machine_state"] = snapshot.machine_state().value
    return json.dumps(payload)


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
    ) -> None:
        self._connection = connection
        self._estop = estop
        self._door_closed = door_closed
        self._cell_state = cell_state
        self._probe = GrblSerialProbe()
        self._realtime = GrblRealtimeControl()
        self._gate = CncCellBridge()
        self._last_snapshot: CncSnapshot | None = None

    def refresh_status(self) -> CncSnapshot:
        """Query real GRBL status now and remember it for `cycle_start_resume`/`cmd/job`."""

        snapshot = self._probe.query_status(self._connection, estop=self._estop(), door_closed=self._door_closed())
        self._last_snapshot = snapshot
        return snapshot

    def handle_message(self, topic: str, payload: bytes) -> list[MqttPublish]:
        """Route one real inbound MQTT message to the real command it names.

        An unrecognised topic (this bridge subscribes to `cmd/#`, a
        wildcard) is silently ignored rather than erroring - a future
        sibling topic under `cmd/` this version does not know about yet
        must never crash the whole message loop.
        """

        if not topic.startswith(TOPIC_PREFIX):
            return []
        suffix = topic[len(TOPIC_PREFIX) :]

        if suffix == "cmd/status":
            return [MqttPublish(f"{TOPIC_PREFIX}state", _snapshot_payload(self.refresh_status()), retain=True)]
        if suffix == "cmd/feed_hold":
            result = self._realtime.feed_hold(self._connection)
            return [MqttPublish(f"{TOPIC_PREFIX}cmd/feed_hold/result", json.dumps(asdict(result)))]
        if suffix == "cmd/soft_reset":
            result = self._realtime.soft_reset(self._connection)
            return [MqttPublish(f"{TOPIC_PREFIX}cmd/soft_reset/result", json.dumps(asdict(result)))]
        if suffix == "cmd/cycle_start_resume":
            snapshot = self._last_snapshot or self.refresh_status()
            result = self._realtime.cycle_start_resume(self._connection, self._cell_state(), snapshot)
            return [MqttPublish(f"{TOPIC_PREFIX}cmd/cycle_start_resume/result", json.dumps(asdict(result)))]
        if suffix == "cmd/job":
            return [self._handle_job(payload)]
        return []

    def _handle_job(self, payload: bytes) -> MqttPublish:
        # A malformed job payload (bad JSON, missing field, unknown enum
        # value) must fail closed with a real, honest "not allowed"
        # decision on the same result topic a caller is already listening
        # to - never a raised exception that would kill the whole message
        # loop and silently stop this bridge from answering anything else.
        try:
            job = job_from_dict(json.loads(payload))
        except (json.JSONDecodeError, BridgeError, UnicodeDecodeError) as error:
            decision = {"allowed": False, "reason": f"malformed job payload: {error}"}
            return MqttPublish(f"{TOPIC_PREFIX}cmd/job/result", json.dumps(decision))
        snapshot = self._last_snapshot or self.refresh_status()
        decision = self._gate.plan(job, self._cell_state(), snapshot)
        return MqttPublish(f"{TOPIC_PREFIX}cmd/job/result", json.dumps(decision_to_dict(decision)))


def run_forever(
    bridge: CncMqttBridge,
    host: str,
    port: int = 1883,
    client_id: str = "hydra-umc-bridge-cnc",
) -> None:
    """Connect to a real HYDRA-UMC-MQTT-BROKER and dispatch forever.

    The only place this module imports paho-mqtt - lazily, so the rest of
    this module (and every test) works on a host without it installed.
    """

    try:
        import paho.mqtt.client as mqtt  # type: ignore[import-untyped]
    except ImportError as error:
        raise RuntimeError(
            "paho-mqtt is not installed - install it to connect to a real HYDRA-UMC-MQTT-BROKER "
            "(this module's topic-dispatch/gating logic works and is tested without it)"
        ) from error

    def on_connect(client: object, userdata: object, flags: object, reason_code: object, properties: object = None) -> None:
        client.subscribe(f"{TOPIC_PREFIX}cmd/#")  # type: ignore[attr-defined]

    def on_message(client: object, userdata: object, message: object) -> None:
        for publish in bridge.handle_message(message.topic, message.payload):  # type: ignore[attr-defined]
            client.publish(publish.topic, publish.payload, retain=publish.retain)  # type: ignore[attr-defined]

    client = mqtt.Client(client_id=client_id)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port)
    client.loop_forever()
