# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Real GRBL serial transport
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Real, fail-closed GRBL serial transport - never a G-code streaming path.

This module can now genuinely open a serial port and exchange bytes with a
real GRBL controller - a first for this bridge, which until now only parsed
an already-collected status string (`observation.py`). It stays inside the
exact same boundary this bridge has always documented: it queries real-time
status and sends only GRBL's own real-time single-byte control characters
(status query, feed hold, cycle start/resume, soft reset) - it never streams
a G-code program. LinuxCNC or the native controller keeps all real-time
trajectory, limits, spindle and safety authority, unchanged.

The GRBL-facing logic is written against a small `SerialLike` protocol
(`write`/`readline`/`close`) rather than importing `pyserial` directly in
every function, so the safety-relevant parts are unit-testable with a plain
in-memory fake - no real hardware, OS pty or `pyserial` install required to
prove the logic is correct. `open_serial_port()` is the one real place
`pyserial` is imported, lazily, so a host without it installed still gets a
clean "pyserial not installed" error instead of an ImportError at module
load time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from hydra_umc_sdk.bridge_contract import CellState, MachineState

from .cell import CncSnapshot
from .observation import snapshot_from_grbl_status

# Real GRBL v1.1 real-time command bytes (github.com/gnea/grbl/wiki/
# Grbl-v1.1-Interface#real-time-commands) - each is processed immediately by
# GRBL's ISR, out-of-band from the normal line-buffered G-code channel, so
# sending one never requires (or waits on) a G-code send queue.
_STATUS_QUERY = b"?"
_FEED_HOLD = b"!"
_CYCLE_START_RESUME = b"~"
_SOFT_RESET = b"\x18"  # Ctrl-X


class SerialLike(Protocol):
    """The minimal real serial interface this module depends on."""

    def write(self, data: bytes) -> object: ...
    def readline(self) -> bytes: ...
    def close(self) -> object: ...


def open_serial_port(port: str, baud: int = 115200, timeout_seconds: float = 1.0) -> SerialLike:
    """Open a real serial port. The only place this module imports pyserial.

    Raises RuntimeError with a clear message if pyserial isn't installed,
    rather than letting an ImportError surface from deep inside this module.
    """

    try:
        import serial  # type: ignore[import-untyped]
    except ImportError as error:
        raise RuntimeError(
            "pyserial is not installed - install it to talk to a real GRBL controller "
            "(this module's parsing/gating logic works and is tested without it)"
        ) from error
    return serial.Serial(port, baudrate=baud, timeout=timeout_seconds)


class GrblSerialProbe:
    """Query real GRBL status over an already-open serial-like connection."""

    def query_status(self, connection: SerialLike, *, estop: object, door_closed: object) -> CncSnapshot:
        # A transport-level failure (closed port, unplugged USB-serial
        # adapter, garbage bytes) must fail the same safe way every other
        # failure mode in this bridge does - OFFLINE, not a crash.
        try:
            connection.write(_STATUS_QUERY)
            line = connection.readline()
            status_line = line.decode("ascii", errors="replace").strip()
        except (OSError, ValueError, UnicodeDecodeError):
            return CncSnapshot("", estop if isinstance(estop, bool) else True, False)
        return snapshot_from_grbl_status(status_line, estop=estop, door_closed=door_closed)


@dataclass(frozen=True)
class RealtimeCommandResult:
    """Mirrors the sibling PRINTER3D bridge's `JobCommandResult` shape - a
    real command outcome with a reason, not a bare bool that loses why."""

    allowed: bool
    executed: bool
    reason: str


class GrblRealtimeControl:
    """Send only GRBL's own real-time control bytes - never a G-code program.

    Feed hold and soft reset are always allowed (same de-escalation reasoning
    as ABORT/HOLD_POSITION elsewhere in this ecosystem - an operator must
    always be able to pause or reset); resume uses a standalone gate
    requiring a genuinely HOLDING machine, not the generic IDLE-based gate
    (which is backwards for resuming a paused job) - same reasoning already
    applied to the sibling PRINTER3D bridge's `resume_job()`.
    """

    def feed_hold(self, connection: SerialLike) -> RealtimeCommandResult:
        return self._write(connection, _FEED_HOLD, "feed hold sent")

    def soft_reset(self, connection: SerialLike) -> RealtimeCommandResult:
        return self._write(connection, _SOFT_RESET, "soft reset sent")

    def cycle_start_resume(
        self, connection: SerialLike, cell_state: CellState, cnc: CncSnapshot
    ) -> RealtimeCommandResult:
        if cell_state is not CellState.READY:
            return RealtimeCommandResult(False, False, f"cell is {cell_state.value}, not READY")
        machine_state = cnc.machine_state()
        if machine_state is not MachineState.HOLDING:
            return RealtimeCommandResult(
                False, False, f"CNC is {machine_state.value}, not HOLDING (nothing to resume)"
            )
        return self._write(connection, _CYCLE_START_RESUME, "cycle start/resume sent")

    @staticmethod
    def _write(connection: SerialLike, command: bytes, ok_reason: str) -> RealtimeCommandResult:
        try:
            connection.write(command)
            return RealtimeCommandResult(True, True, ok_reason)
        except OSError as error:
            return RealtimeCommandResult(True, False, f"serial write failed: {error}")
