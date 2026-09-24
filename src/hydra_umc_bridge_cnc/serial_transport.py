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

import os
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


def resolve_serial_device_path(device_id: str, *, list_ports: Callable[[], list] | None = None) -> str:
    """Resolve a real, stable USB device identifier to whatever OS device
    path/port name it currently has - never a static `COM3`/`/dev/ttyUSB0`
    that a cable replug (or a reboot enumerating USB in a different order)
    can silently reassign to a different physical device, or away from this
    one entirely.

    Two real identifier shapes are accepted:

    - An already-existing path (e.g. a Linux `/dev/serial/by-id/usb-...`
      symlink). These are themselves stable per-physical-device identifiers
      maintained by the OS/udev, keyed off the device's own USB serial
      number - resolving one is just confirming it still exists and handing
      it back; no port enumeration needed.
    - A bare USB identifier (the device's serial number, or a substring of
      its `hwid`) that is matched against `list_ports()` - what a Windows
      `COM*` reconnect needs, since Windows has no by-id path convention.
      `list_ports` defaults to `serial.tools.list_ports.comports` (imported
      lazily, same lazy-pyserial convention as `open_serial_port()`), and is
      injectable so this is unit-testable without any real hardware or
      pyserial install.

    Raises RuntimeError (never returns a guess) if `device_id` names neither
    an existing path nor a currently-enumerated device - a real "not plugged
    in right now" condition a reconnect loop is expected to retry, not a bug
    to crash on.
    """

    if os.path.exists(device_id):
        return device_id

    if list_ports is None:
        try:
            from serial.tools.list_ports import comports as list_ports  # type: ignore[import-untyped]
        except ImportError as error:
            raise RuntimeError(
                "pyserial is not installed - install it to resolve a USB device by id "
                "(this module's parsing/gating logic works and is tested without it)"
            ) from error

    for port in list_ports():
        hwid = getattr(port, "hwid", "") or ""
        serial_number = getattr(port, "serial_number", None)
        if serial_number == device_id or device_id in hwid:
            return port.device

    raise RuntimeError(f"no currently-connected serial device matches id {device_id!r}")


def open_serial_port_by_id(
    device_id: str,
    baud: int = 115200,
    timeout_seconds: float = 1.0,
    *,
    list_ports: Callable[[], list] | None = None,
) -> SerialLike:
    """Open a real serial port identified by a stable USB device id (a
    `/dev/serial/by-id/*` path, or a serial number/hwid substring matched
    against currently-enumerated ports) rather than a static, OS-assigned
    port name. Combine with `ReconnectingSerialConnection` to keep finding
    the same physical device across a cable replug even when the OS hands
    it a different port name/number afterwards."""

    port = resolve_serial_device_path(device_id, list_ports=list_ports)
    return open_serial_port(port, baud=baud, timeout_seconds=timeout_seconds)


class ReconnectingSerialConnection:
    """A `SerialLike` that transparently reopens the same physical USB
    device (by id, via `resolve_serial_device_path`) after a real I/O
    failure - a cable replug, a controller power-cycle, a USB device
    re-enumerating under a different port name - instead of staying
    permanently dead until the whole bridge process is restarted.

    `connect` is the real, injectable "open one connection" callable (in
    production, `lambda: open_serial_port_by_id(device_id, ...)`; in tests,
    a fake factory) - this class owns none of the actual device-resolution
    logic itself, only the reconnect-on-failure behavior, so it stays
    testable with the exact same in-memory `FakeSerial` style already used
    across this bridge's test suite.

    A write/readline that raises OSError (what a real unplugged/broken
    serial device raises) closes the dead underlying connection, attempts
    exactly one reconnect via `connect()`, and retries the same operation
    once on the new connection. If `connect()` itself fails (device still
    not present), the original OSError propagates unchanged - callers
    (`GrblSerialProbe.query_status`, `GrblRealtimeControl._write`) already
    fail closed on OSError, so this never weakens the existing safety
    behavior, it only gives a *future* call a real chance to succeed again
    once the device comes back.
    """

    def __init__(self, connect: Callable[[], SerialLike]) -> None:
        self._connect = connect
        self._connection: SerialLike | None = None

    def _ensure_connected(self) -> SerialLike:
        if self._connection is None:
            self._connection = self._connect()
        return self._connection

    def _reconnect(self) -> SerialLike:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001 - closing an already-dead connection must never mask the real error
                pass
            self._connection = None
        self._connection = self._connect()
        return self._connection

    def write(self, data: bytes) -> object:
        connection = self._ensure_connected()
        try:
            return connection.write(data)
        except OSError:
            connection = self._reconnect()
            return connection.write(data)

    def readline(self) -> bytes:
        connection = self._ensure_connected()
        try:
            return connection.readline()
        except OSError:
            connection = self._reconnect()
            return connection.readline()

    def close(self) -> object:
        if self._connection is None:
            return None
        result = self._connection.close()
        self._connection = None
        return result


class GrblSerialProbe:
    """Query real GRBL status over an already-open serial-like connection."""

    def query_status(
        self,
        connection: SerialLike,
        *,
        estop: Callable[[], object],
        door_closed: Callable[[], object],
    ) -> CncSnapshot:
        # `estop`/
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
        # queries (this project's own already-fixed gap).
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
        # a real
        # pyserial connection's write() returns the real number of bytes
        # actually written - a `0`-byte (or short) write raises no
        # exception at all, so it used to be reported as `executed=True`
        # just because nothing crashed.
        #
        # (P2):
        # this project's own fix only caught a reported SHORT count -
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
