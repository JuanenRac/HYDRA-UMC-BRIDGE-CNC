# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - CNCjs link
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Observe a machine through CNCjs, and pause it, without owning its serial port.

A GRBL controller has one serial port, and a G-code sender such as CNCjs usually holds it. This link is the way for the
bridge to work beside that sender instead of fighting it for the port: CNCjs owns the controller and is the *trusted sender*
of programs; the bridge connects to the CNCjs socket.io API, listens to the ``controller:state`` it already broadcasts, and
sends only the three requests it has always sent:

- ``feed_hold``  -> ``command`` ``feedhold``     always allowed;
- ``soft_reset`` -> ``command`` ``reset``        always allowed;
- ``cycle_start_resume`` -> ``command`` ``cyclestart``  only for a READY cell and a machine that is really holding.

It never sends ``gcode`` (a program or a line of one), ``unlock``, ``homing`` or ``sleep``. The state it reads is only trusted
while it is fresh: a CNCjs that stopped talking is not an idle machine, so an old reading is ``OFFLINE``.

The socket is a ``SocketLike`` (``emit`` and ``on``), so the whole link is tested against ``SimulatedCncjs``; ``open_cncjs``
is the one place that imports a socket.io client, lazily.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from hydra_umc_sdk.bridge_contract import CellState
from hydra_umc_sdk.serial_link import RealtimeCommandResult, call_bool

from .cell import CncSnapshot
from .controller import ReadOnlyController, resume_refusal

# The only CNCjs `command` words this link ever emits. Anything else (gcode, unlock, homing, sleep, macro...) is a program or a
# motion, and is not this bridge's to send.
COMMAND_FEED_HOLD = "feedhold"
COMMAND_RESET = "reset"
COMMAND_CYCLE_START = "cyclestart"
ALLOWED_COMMANDS = frozenset({COMMAND_FEED_HOLD, COMMAND_RESET, COMMAND_CYCLE_START})

DEFAULT_MAX_AGE_SECONDS = 3.0


class SocketLike(Protocol):
    """The two calls of a socket.io client that the link uses."""

    def emit(self, event: str, *arguments: object) -> object: ...
    def on(self, event: str, handler: Callable[..., object]) -> object: ...


def _number(value: object) -> float | None:
    """CNCjs sends some numbers as numbers and some (positions) as strings; anything else is not a reading."""

    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def _axes(value: object) -> tuple[float, ...] | None:
    if not isinstance(value, dict):
        return None
    numbers = tuple(_number(value.get(axis)) for axis in ("x", "y", "z") if axis in value)
    return numbers if numbers and all(number is not None for number in numbers) else None  # type: ignore[return-value]


def snapshot_from_cncjs_state(state: object, *, estop: object, door_closed: object) -> CncSnapshot:
    """Normalize the ``state`` of a ``controller:state`` event of a GRBL controller.

    ``state["status"]["activeState"]`` is GRBL's own state word (with ``subState`` for Hold and Door). A payload that is not
    shaped like that is an unknown state, not an idle one.
    """

    safeguard_estop = estop if isinstance(estop, bool) else True
    safeguard_door = door_closed if isinstance(door_closed, bool) else False
    status = state.get("status") if isinstance(state, dict) else None
    if not isinstance(status, dict):
        return CncSnapshot("", safeguard_estop, safeguard_door)
    active = status.get("activeState")
    if not isinstance(active, str) or not active:
        return CncSnapshot("", safeguard_estop, safeguard_door)
    sub_state = status.get("subState")
    if isinstance(sub_state, (str, int)) and not isinstance(sub_state, bool) and str(sub_state) != "":
        active = f"{active}:{sub_state}"
    overrides = status.get("ov")
    override_numbers = None
    if isinstance(overrides, (list, tuple)) and len(overrides) == 3:
        parsed = tuple(_number(item) for item in overrides)
        override_numbers = parsed if all(item is not None for item in parsed) else None
    return CncSnapshot(
        active,
        safeguard_estop,
        safeguard_door,
        spindle_speed=_number(status.get("spindle")),
        feed_rate=_number(status.get("feedrate")),
        machine_position=_axes(status.get("mpos")),
        work_position=_axes(status.get("wpos")),
        work_offset=_axes(status.get("wco")),
        overrides=override_numbers,  # type: ignore[arg-type]
    )


class CncjsController(ReadOnlyController):
    """A CNCjs server that owns the controller: the bridge reads its broadcast and pauses it."""

    name = "cncjs"

    def __init__(
        self,
        socket: SocketLike,
        port: str,
        *,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(port, str) or not port:
            raise ValueError("port must be the name of the serial port CNCjs has open")
        if not (isinstance(max_age_seconds, (int, float)) and math.isfinite(max_age_seconds) and max_age_seconds > 0):
            raise ValueError("max_age_seconds must be a positive number")
        self._socket = socket
        self._port = port
        self._max_age = float(max_age_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._state: object = None
        self._state_at: float | None = None
        self._port_open = False
        socket.on("controller:state", self._on_state)
        socket.on("serialport:open", self._on_open)
        socket.on("serialport:close", self._on_close)

    # events from CNCjs; the first argument of controller:state is the controller type ("Grbl")
    def _on_state(self, *arguments: object) -> None:
        if len(arguments) >= 2 and arguments[0] != "Grbl":
            return  # this link understands GRBL; another controller type is not read as one
        state = arguments[-1] if arguments else None
        with self._lock:
            self._state, self._state_at = state, self._clock()

    def _on_open(self, *arguments: object) -> None:
        with self._lock:
            self._port_open = True

    def _on_close(self, *arguments: object) -> None:
        with self._lock:
            self._port_open = False
            self._state, self._state_at = None, None

    def read_snapshot(self, *, estop: Callable[[], object], door_closed: Callable[[], object]) -> CncSnapshot:
        estop_value, door_value = call_bool(estop, True), call_bool(door_closed, False)
        with self._lock:
            state, at, is_open = self._state, self._state_at, self._port_open
        if not is_open or at is None or self._clock() - at > self._max_age:
            return CncSnapshot("", estop_value, False)  # an old or absent reading is never an idle machine or a closed door
        return snapshot_from_cncjs_state(state, estop=estop_value, door_closed=door_value)

    def feed_hold(self) -> RealtimeCommandResult:
        return self._command(COMMAND_FEED_HOLD, "feed hold sent through CNCjs")

    def soft_reset(self) -> RealtimeCommandResult:
        return self._command(COMMAND_RESET, "reset sent through CNCjs")

    def cycle_start_resume(self, cell_state: CellState, snapshot: CncSnapshot) -> RealtimeCommandResult:
        refusal = resume_refusal(cell_state, snapshot)
        if refusal is not None:
            return refusal
        return self._command(COMMAND_CYCLE_START, "cycle start sent through CNCjs")

    def _command(self, word: str, ok_reason: str) -> RealtimeCommandResult:
        assert word in ALLOWED_COMMANDS  # the whole vocabulary of this link, checked where it leaves
        with self._lock:
            is_open = self._port_open
        if not is_open:
            return RealtimeCommandResult(True, False, "CNCjs has no serial port open: nothing was sent")
        try:
            self._socket.emit("command", self._port, word)
        except Exception as error:  # noqa: BLE001 - a failing link is a command that did not happen, not a crash
            return RealtimeCommandResult(True, False, f"CNCjs command failed: {error}")
        return RealtimeCommandResult(True, True, ok_reason)


def open_cncjs(url: str, port: str, *, token: str | None = None, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> CncjsController:
    """Connect to a CNCjs server and attach to the serial port it has open. The only place a socket.io client is imported, lazily.

    The access token of CNCjs, when it has one, goes in the query of the connection and is never printed.
    """

    try:
        import socketio  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError(
            "python-socketio is not installed - install it to connect to CNCjs "
            "(the mapping and the gate work and are tested without it)"
        ) from error
    client = socketio.Client()
    query = f"?token={token}" if token else ""
    client.connect(url + query)
    link = CncjsController(client, port, max_age_seconds=max_age_seconds)
    # attach to the port CNCjs already has open: this only subscribes to its broadcast, it does not open or reopen the port
    client.emit("open", port, {"baudrate": 115200, "controllerType": "Grbl"})
    return link


# --- a CNCjs that exists only in memory ---------------------------------------------------------------------------------


@dataclass
class SimulatedCncjs:
    """A CNCjs server in memory: it keeps the handlers a client registers, broadcasts a GRBL state and records every command."""

    handlers: dict[str, list[Callable[..., object]]] = field(default_factory=dict)
    emitted: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    fail_emit: Exception | None = None

    def on(self, event: str, handler: Callable[..., object]) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, *arguments: object) -> None:
        if self.fail_emit is not None:
            raise self.fail_emit
        self.emitted.append((event, arguments))

    def broadcast(self, event: str, *arguments: object) -> None:
        for handler in self.handlers.get(event, []):
            handler(*arguments)

    def open_port(self, port: str = "/dev/ttyUSB0") -> None:
        self.broadcast("serialport:open", {"port": port, "baudrate": 115200, "controllerType": "Grbl"})

    def close_port(self, port: str = "/dev/ttyUSB0") -> None:
        self.broadcast("serialport:close", {"port": port})

    def publish_state(self, active: str = "Idle", *, sub_state: int | None = None, spindle: float = 0, feedrate: float = 0) -> None:
        status: dict[str, object] = {
            "activeState": active,
            "mpos": {"x": "0.000", "y": "0.000", "z": "0.000"},
            "wpos": {"x": "0.000", "y": "0.000", "z": "0.000"},
            "ov": [100, 100, 100],
            "feedrate": feedrate,
            "spindle": spindle,
        }
        if sub_state is not None:
            status["subState"] = sub_state
        self.broadcast("controller:state", "Grbl", {"status": status, "parserstate": {}})

    def commands(self) -> list[str]:
        """The command words received, in order."""
        return [str(arguments[1]) for event, arguments in self.emitted if event == "command"]
