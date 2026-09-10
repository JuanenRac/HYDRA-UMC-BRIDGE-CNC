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
from typing import Callable, Protocol

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


def _call_bool(getter: Callable[[], object], safe_default: bool) -> bool:
    """Calls a real-time safety-signal getter and fails closed exactly like
    `observation.py`'s own `_strict_bool()` does - only a real `bool` result
    is trusted, and if `getter` itself raises (a real sensor read failing),
    that is treated the same as a missing/wrong-typed reading rather than
    propagating and aborting the whole status query."""
    try:
        value = getter()
    except Exception:  # noqa: BLE001 - any real signal-read failure fails closed, deliberately broad
        return safe_default
    return value if isinstance(value, bool) else safe_default


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

    def query_status(
        self,
        connection: SerialLike,
        *,
        estop: Callable[[], object],
        door_closed: Callable[[], object],
    ) -> CncSnapshot:
        # REV-005: `estop`/
        # `door_closed` are callables, not already-read values, and are
        # deliberately called AFTER the blocking write()/readline() below,
        # never before. `readline()` can block for up to this connection's
        # real timeout waiting on GRBL's own reply - a real E-STOP hit or
        # door opened DURING that wait must still be reflected in the
        # snapshot this call returns. Reading the signals first (the
        # previous shape of this function) captured a pre-block snapshot
        # of a signal that a physical event could invalidate before the
        # blocking call even returned - a real interlock allowed to go
        # stale for the full length of one status query, not just between
        # queries (CNC-01's own already-fixed gap).
        try:
            connection.write(_STATUS_QUERY)
            line = connection.readline()
            status_line = line.decode("ascii", errors="replace").strip()
        except (OSError, ValueError, UnicodeDecodeError):
            return CncSnapshot("", _call_bool(estop, True), False)
        return snapshot_from_grbl_status(status_line, estop=_call_bool(estop, True), door_closed=_call_bool(door_closed, False))


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
            written = connection.write(command)
        except OSError as error:
            return RealtimeCommandResult(True, False, f"serial write failed: {error}")
        # REV-006: a real
        # pyserial connection's write() returns the real number of bytes
        # actually written - a `0`-byte (or short) write raises no
        # exception at all, so it used to be reported as `executed=True`
        # just because nothing crashed.
        #
        # V07-019 (P2):
        # REV-006's own fix only caught a reported SHORT count -
        # `None`/`False`/a non-int value (a serial-like implementation
        # that does not conform to pyserial's own real int-byte-count
        # contract) was still silently trusted as a genuine confirmed
        # write, just because it wasn't a *short* int. A real serial
        # write() always returns the real int byte count, and it always
        # equals `len(command)` for a real, fully-sent command - both are
        # now required exactly, distinguishing "bytes sent" from "action
        # physically completed" is still this function's own honest
        # limit (see class docstring), never claimed otherwise. The real
        # fakes/stubs in this bridge's own test suite were fixed to
        # report a real byte count by default, rather than relaxing this
        # production contract for them (see test_serial_transport.py's
        # own FakeSerial).
        if not isinstance(written, int) or isinstance(written, bool):
            return RealtimeCommandResult(
                True, False, f"serial write did not report a real byte count sent (got {written!r})"
            )
        if written != len(command):
            return RealtimeCommandResult(True, False, f"serial write incomplete: {written}/{len(command)} bytes sent")
        return RealtimeCommandResult(True, True, ok_reason)
