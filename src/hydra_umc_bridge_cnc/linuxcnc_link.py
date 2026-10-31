# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - LinuxCNC link
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Read a LinuxCNC installation, and pause or abort it; never run a program.

LinuxCNC is the industrial controller the READMEs of the ecosystem always named. This link reads what the Python module
``linuxcnc`` exposes through ``linuxcnc.stat()`` (task state, interpreter state, emergency stop, spindle) and sends only the
requests that bring a machine to rest or let a paused one go on, through ``linuxcnc.command()``:

- ``feed_hold``  -> ``auto(AUTO_PAUSE)``     always allowed, like a GRBL feed hold;
- ``soft_reset`` -> ``abort()``              always allowed, like a GRBL soft reset;
- ``cycle_start_resume`` -> ``auto(AUTO_RESUME)``  only for a READY cell and a machine that is really paused.

It never loads or runs a program (no ``program_open``, no ``auto(AUTO_RUN)``, no MDI), never changes the task state (no
``state(STATE_ESTOP_RESET)``) and never sets a spindle: LinuxCNC keeps all trajectory, limit and safety authority.

LinuxCNC has no door signal. ``door_closed`` is read from the cell (a HAL pin the integrator exposes), exactly as for GRBL,
and a missing one fails closed. The emergency stop is the OR of what LinuxCNC reports and of the cell's own signal.

The module is imported lazily by ``open_linuxcnc()``; everything else is written against two small objects (``stat`` and
``command``), so the mapping is tested here with ``SimulatedLinuxcnc`` and needs neither LinuxCNC nor Linux.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from hydra_umc_sdk.bridge_contract import CellState
from hydra_umc_sdk.serial_link import RealtimeCommandResult, call_bool

from .cell import CncSnapshot
from .controller import ReadOnlyController, resume_refusal

# The integer values of the constants of the linuxcnc module (linuxcnc.STATE_*, INTERP_*, AUTO_*, MODE_*). A real module
# is asked for its own value first; these are used when it does not have the name and by the simulator.
DEFAULTS = {
    "STATE_ESTOP": 1,
    "STATE_ESTOP_RESET": 2,
    "STATE_OFF": 3,
    "STATE_ON": 4,
    "INTERP_IDLE": 1,
    "INTERP_READING": 2,
    "INTERP_PAUSED": 3,
    "INTERP_WAITING": 4,
    "AUTO_RUN": 0,
    "AUTO_PAUSE": 1,
    "AUTO_RESUME": 2,
    "AUTO_STEP": 3,
    "MODE_MANUAL": 1,
    "MODE_AUTO": 2,
    "MODE_MDI": 3,
}

COMMAND_TIMEOUT_SECONDS = 2.0


def _constant(module: object, name: str) -> int:
    value = getattr(module, name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else DEFAULTS[name]


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _spindle_speed(stat: object) -> float | None:
    """The spindle speed in rpm, 0 when the spindle is off, None when this LinuxCNC does not say."""

    spindles = getattr(stat, "spindle", None)
    if spindles:
        try:
            first = spindles[0]
            enabled = bool(first["enabled"])
            speed = _finite(first["speed"])
        except (LookupError, TypeError, ValueError):
            return None
        if speed is None:
            return None
        return abs(speed) if enabled else 0.0
    legacy = _finite(getattr(stat, "spindle_speed", None))  # LinuxCNC before 2.8
    return abs(legacy) if legacy is not None else None


def _position(stat: object) -> tuple[float, ...] | None:
    values = getattr(stat, "actual_position", None)
    try:
        numbers = tuple(_finite(value) for value in list(values)[:6])  # type: ignore[arg-type]
    except TypeError:
        return None
    return numbers if numbers and all(number is not None for number in numbers) else None  # type: ignore[return-value]


def snapshot_from_linuxcnc_stat(
    stat: object,
    *,
    estop: object,
    door_closed: object,
    constants: object = None,
) -> CncSnapshot:
    """Normalize one ``linuxcnc.stat`` reading into the snapshot the bridge gates on.

    The state words are the ones the rest of the bridge already understands (``IDLE``, ``RUN``, ``JOG``, ``HOLD``, ``OFF``,
    ``ALARM``). An emergency stop reported by LinuxCNC or by the cell makes the snapshot an emergency stop; a task state
    that is not ``ON``, or a stat that cannot be read, is never idle.
    """

    safeguard_estop = estop if isinstance(estop, bool) else True
    safeguard_door = door_closed if isinstance(door_closed, bool) else False
    get = lambda name: _constant(constants, name)  # noqa: E731
    try:
        task_state = getattr(stat, "task_state")  # noqa: B009
        interp_state = getattr(stat, "interp_state")  # noqa: B009
        stat_estop = bool(getattr(stat, "estop", False))
    except AttributeError:
        return CncSnapshot("", safeguard_estop, safeguard_door)
    if task_state in (get("STATE_ESTOP"),):
        stat_estop = True
    controller_state = ""
    if task_state == get("STATE_ESTOP"):
        controller_state = "ALARM"
    elif task_state in (get("STATE_ESTOP_RESET"), get("STATE_OFF")):
        controller_state = "OFF"
    elif task_state == get("STATE_ON"):
        if interp_state == get("INTERP_PAUSED"):
            controller_state = "HOLD"
        elif interp_state in (get("INTERP_READING"), get("INTERP_WAITING")):
            controller_state = "RUN"
        elif interp_state == get("INTERP_IDLE"):
            velocity = _finite(getattr(stat, "current_vel", 0.0)) or 0.0
            controller_state = "JOG" if velocity > 0.0 else "IDLE"
    current_velocity = _finite(getattr(stat, "current_vel", None))
    return CncSnapshot(
        controller_state,
        safeguard_estop or stat_estop,
        safeguard_door,
        spindle_speed=_spindle_speed(stat),
        feed_rate=current_velocity * 60.0 if current_velocity is not None else None,  # machine units per second -> per minute
        machine_position=_position(stat),
    )


class LinuxcncController(ReadOnlyController):
    """LinuxCNC through ``linuxcnc.stat`` and ``linuxcnc.command``: it reads, pauses, aborts and resumes a paused program."""

    name = "linuxcnc"

    def __init__(self, stat: object, command: object, *, constants: object = None) -> None:
        self._stat = stat
        self._command = command
        self._constants = constants

    def read_snapshot(self, *, estop: Callable[[], object], door_closed: Callable[[], object]) -> CncSnapshot:
        try:
            self._stat.poll()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - a LinuxCNC that does not answer is not known to be fine
            return CncSnapshot("", call_bool(estop, True), False)
        return snapshot_from_linuxcnc_stat(
            self._stat, estop=call_bool(estop, True), door_closed=call_bool(door_closed, False), constants=self._constants
        )

    def feed_hold(self) -> RealtimeCommandResult:
        return self._send(lambda command: command.auto(_constant(self._constants, "AUTO_PAUSE")), "pause sent to LinuxCNC")

    def soft_reset(self) -> RealtimeCommandResult:
        return self._send(lambda command: command.abort(), "abort sent to LinuxCNC")

    def cycle_start_resume(self, cell_state: CellState, snapshot: CncSnapshot) -> RealtimeCommandResult:
        refusal = resume_refusal(cell_state, snapshot)
        if refusal is not None:
            return refusal
        return self._send(lambda command: command.auto(_constant(self._constants, "AUTO_RESUME")), "resume sent to LinuxCNC")

    def _send(self, action: Callable[[Any], object], ok_reason: str) -> RealtimeCommandResult:
        try:
            action(self._command)
            wait = getattr(self._command, "wait_complete", None)
            if wait is not None:
                outcome = wait(COMMAND_TIMEOUT_SECONDS)
                # wait_complete() returns RCS_DONE (1) when the command was taken, RCS_ERROR (-1) or a timeout otherwise
                if isinstance(outcome, int) and not isinstance(outcome, bool) and outcome != 1:
                    return RealtimeCommandResult(True, False, f"LinuxCNC did not confirm the command (wait_complete returned {outcome})")
        except Exception as error:  # noqa: BLE001 - a failing link is a command that did not happen, not a crash
            return RealtimeCommandResult(True, False, f"LinuxCNC command failed: {error}")
        return RealtimeCommandResult(True, True, ok_reason)


def open_linuxcnc() -> LinuxcncController:
    """Connect to the running LinuxCNC of this host. The only place this module imports ``linuxcnc``, lazily."""

    try:
        import linuxcnc  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError(
            "the linuxcnc Python module is not available - run this on the host of a LinuxCNC installation "
            "(the mapping and the gate work and are tested without it)"
        ) from error
    return LinuxcncController(linuxcnc.stat(), linuxcnc.command(), constants=linuxcnc)


# --- a LinuxCNC that exists only in memory -----------------------------------------------------------------------------


@dataclass
class SimulatedLinuxcnc:
    """The part of LinuxCNC this link touches, in memory: a ``stat`` that can be set and a ``command`` that records.

    It follows what the real thing does for the three requests: ``AUTO_PAUSE`` pauses a running interpreter,
    ``AUTO_RESUME`` resumes a paused one, ``abort()`` returns it to idle. Nothing here runs a program.
    """

    task_state: int = DEFAULTS["STATE_ON"]
    interp_state: int = DEFAULTS["INTERP_IDLE"]
    estop: bool = False
    current_vel: float = 0.0
    actual_position: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    spindle: list[dict[str, object]] = field(default_factory=lambda: [{"enabled": False, "speed": 0.0}])
    commands: list[tuple[str, int | None]] = field(default_factory=list)
    poll_error: Exception | None = None
    wait_result: int = 1

    # the ``stat`` side
    def poll(self) -> None:
        if self.poll_error is not None:
            raise self.poll_error

    # the ``command`` side
    def auto(self, mode: int) -> None:
        self.commands.append(("auto", mode))
        if mode == DEFAULTS["AUTO_PAUSE"] and self.interp_state in (DEFAULTS["INTERP_READING"], DEFAULTS["INTERP_WAITING"]):
            self.interp_state = DEFAULTS["INTERP_PAUSED"]
            self.current_vel = 0.0
        elif mode == DEFAULTS["AUTO_RESUME"] and self.interp_state == DEFAULTS["INTERP_PAUSED"]:
            self.interp_state = DEFAULTS["INTERP_READING"]

    def abort(self) -> None:
        self.commands.append(("abort", None))
        self.interp_state = DEFAULTS["INTERP_IDLE"]
        self.current_vel = 0.0

    def wait_complete(self, timeout: float = 5.0) -> int:
        return self.wait_result

    def controller(self) -> LinuxcncController:
        """A link whose stat and command are this simulation."""
        return LinuxcncController(self, self)
