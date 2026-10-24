# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - MTConnect observations of the CNC
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Say what the CNC is doing in the words of MTConnect, read only.

An MTConnect agent (cppagent, or the HYDRA-UMC-MTCONNECT-ADAPTER) shows the machines of a shop in one vocabulary. This
module turns the snapshot the bridge already reads from the controller into MTConnect observations (``avail``,
``execution``, ``estop``, ``door``, ``spindle_speed``, ``path_feedrate`` and a ``system`` condition) and offers them in the
two shapes a consumer needs:

- ``shdr_lines``: SHDR lines (``time|id|value``, conditions as ``time|id|LEVEL|nativeCode|||message``), the text protocol an
  agent reads from an adapter, served over TCP by ``ShdrAdapter`` (read only: the only line it honours from a client is
  ``* PING``);
- ``observations_payload``: the same observations as the JSON the bridge publishes (retained) on
  ``hydra/bridges/cnc/mtconnect``, for a consumer that already reads the broker.

Values are never made up: a controller that does not report its spindle speed gives ``UNAVAILABLE``, and a controller that
is not reporting at all makes ``avail`` and ``execution`` ``UNAVAILABLE`` rather than ``READY``.
"""

from __future__ import annotations

import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from hydra_umc_sdk.bridge_contract import MachineState

from .cell import CncSnapshot

DEFAULT_DEVICE = "CncCell"
MAX_CLIENTS = 16
MAX_INPUT_LINE = 1024
DEFAULT_HEARTBEAT_MS = 10_000

# MachineState -> MTConnect Execution
_EXECUTION = {
    MachineState.IDLE: "READY",
    MachineState.RUNNING: "ACTIVE",
    MachineState.HOLDING: "FEED_HOLD",
    MachineState.FAULT: "STOPPED",
    MachineState.SAFE_STOP: "INTERRUPTED",
}


@dataclass(frozen=True)
class Observation:
    id: str
    category: str  # EVENT, SAMPLE or CONDITION
    type: str
    value: str  # a value, or a condition level (NORMAL, WARNING, FAULT, UNAVAILABLE)
    native_code: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {"id": self.id, "category": self.category, "type": self.type, "value": self.value}
        if self.category == "CONDITION":
            payload["nativeCode"] = self.native_code
            payload["message"] = self.message
        return payload


def _number(value: float | None) -> str:
    return "UNAVAILABLE" if value is None else (f"{value:.3f}".rstrip("0").rstrip(".") or "0")


def observations_from_snapshot(snapshot: CncSnapshot) -> list[Observation]:
    """The MTConnect observations that say what a snapshot says."""

    state = snapshot.machine_state()
    reporting = state is not MachineState.OFFLINE
    execution = _EXECUTION.get(state, "UNAVAILABLE")
    if state is MachineState.OFFLINE:
        condition = Observation("system", "CONDITION", "SYSTEM", "UNAVAILABLE")
    elif snapshot.estop:
        condition = Observation("system", "CONDITION", "SYSTEM", "FAULT", "ESTOP", "emergency stop")
    elif not snapshot.door_closed:
        condition = Observation("system", "CONDITION", "SYSTEM", "FAULT", "DOOR_OPEN", "safety door open")
    elif state is MachineState.FAULT:
        condition = Observation("system", "CONDITION", "SYSTEM", "FAULT", snapshot.controller_state.upper()[:32], "controller fault")
    else:
        condition = Observation("system", "CONDITION", "SYSTEM", "NORMAL")
    return [
        Observation("avail", "EVENT", "AVAILABILITY", "AVAILABLE" if reporting else "UNAVAILABLE"),
        Observation("execution", "EVENT", "EXECUTION", execution),
        Observation("estop", "EVENT", "EMERGENCY_STOP", "TRIGGERED" if snapshot.estop else "ARMED"),
        Observation("door", "EVENT", "DOOR_STATE", "CLOSED" if snapshot.door_closed else "OPEN"),
        Observation("spindle_speed", "SAMPLE", "SPINDLE_SPEED", _number(snapshot.spindle_speed) if reporting else "UNAVAILABLE"),
        Observation("path_feedrate", "SAMPLE", "PATH_FEEDRATE", _number(snapshot.feed_rate) if reporting else "UNAVAILABLE"),
        condition,
    ]


def escape_shdr(text: str) -> str:
    """SHDR is line and pipe separated: a pipe in a value is escaped and a line break becomes a space."""
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def format_shdr_line(observation: Observation, timestamp_ms: int) -> str:
    time_text = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{timestamp_ms % 1000:03d}Z"
    key = escape_shdr(observation.id)
    if observation.category == "CONDITION":
        if observation.value == "UNAVAILABLE":
            return f"{time_text}|{key}|UNAVAILABLE||||"
        return f"{time_text}|{key}|{observation.value}|{escape_shdr(observation.native_code)}|||{escape_shdr(observation.message)}"
    return f"{time_text}|{key}|{escape_shdr(observation.value)}"


def shdr_lines(snapshot: CncSnapshot, timestamp_ms: int) -> list[str]:
    return [format_shdr_line(item, timestamp_ms) for item in observations_from_snapshot(snapshot)]


def observations_payload(snapshot: CncSnapshot, timestamp_ms: int, device: str = DEFAULT_DEVICE) -> dict[str, object]:
    return {"device": device, "observed_at": timestamp_ms, "observations": [item.to_dict() for item in observations_from_snapshot(snapshot)]}


class _Handler(socketserver.StreamRequestHandler):
    server: "_Server"

    def handle(self) -> None:
        adapter = self.server.adapter
        try:
            if not adapter._greet(self.request):
                return
            while True:
                raw = self.rfile.readline(MAX_INPUT_LINE + 1)
                if not raw or len(raw) > MAX_INPUT_LINE:
                    break
                # the only input honoured is the heartbeat; nothing a client sends can change a value
                if raw.strip() == b"* PING":
                    self.request.sendall(f"* PONG {adapter.heartbeat_ms}\n".encode())
        except OSError:
            pass
        finally:
            adapter._unregister(self.request)


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], adapter: "ShdrAdapter") -> None:
        self.adapter = adapter
        super().__init__(address, _Handler)


class ShdrAdapter:
    """A read-only SHDR adapter for the CNC: serves the observations of the last snapshot to every connected agent.

    Call ``update(snapshot)`` after each status read: a client first receives the latest value of every data item and then
    each change. It listens on ``127.0.0.1`` unless told otherwise.
    """

    def __init__(self, device: str = DEFAULT_DEVICE, *, heartbeat_ms: int = DEFAULT_HEARTBEAT_MS, clock=time.time) -> None:
        self.device = device
        self.heartbeat_ms = heartbeat_ms
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: dict[str, tuple[Observation, str]] = {}
        self._clients: set[socket.socket] = set()
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def listen(self, port: int = 0, host: str = "127.0.0.1") -> int:
        self._server = _Server((host, port), self)
        self._thread = threading.Thread(target=self._server.serve_forever, name="shdr-adapter", daemon=True)
        self._thread.start()
        return int(self._server.server_address[1])

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        with self._lock:
            clients, self._clients = list(self._clients), set()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass

    def update(self, snapshot: CncSnapshot) -> list[str]:
        """Record the observations of a snapshot and send to every client the lines that changed; returns those lines."""

        timestamp_ms = int(self._clock() * 1000)
        fresh: list[str] = []
        with self._lock:
            for observation in observations_from_snapshot(snapshot):
                previous = self._latest.get(observation.id)
                if previous is None or previous[0] != observation:
                    line = format_shdr_line(observation, timestamp_ms)
                    self._latest[observation.id] = (observation, line)
                    fresh.append(line)
            clients = list(self._clients)
        if fresh:
            data = ("\n".join(fresh) + "\n").encode()
            for client in clients:
                try:
                    client.sendall(data)
                except OSError:
                    self._unregister(client)
        return fresh

    def _greet(self, client: socket.socket) -> bool:
        """Send the header and the latest value of every data item, then add the client; both under the lock of ``update``,
        so a change recorded meanwhile reaches the client exactly once, after the values it was greeted with."""

        with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                return False
            greeting = f"* shdrVersion: 2\n* device: {self.device}\n" + "".join(line + "\n" for _, line in self._latest.values())
            client.sendall(greeting.encode())
            self._clients.add(client)
            return True

    def _unregister(self, client: socket.socket) -> None:
        with self._lock:
            self._clients.discard(client)
