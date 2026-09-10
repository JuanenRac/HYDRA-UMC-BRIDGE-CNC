# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Realistic GRBL v1.1 serial emulator (test fixture)
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A genuinely protocol-faithful GRBL v1.1 controller, over the exact
``SerialLike`` seam (`write`/`readline`/`close`) this bridge's real
``serial_transport.py`` talks to.

Until now the only serial test double in this repo was ``FakeSerial`` -
one canned status line, no state, no framing. That proves the bridge's
own parsing/gating in isolation but never that it survives a controller
that behaves like the real thing:

  * A welcome banner (`Grbl 1.1h ['$' for help]`) emitted before anything
    else, that a soft reset re-emits.
  * Real-time bytes (`?`, `!`, `~`, Ctrl-X) processed immediately and
    out-of-band - `!`/`~` produce NO `ok`, exactly like real GRBL.
  * `?` returns a real status report frame:
    `<State|MPos:x,y,z|FS:f,s|WCO:x,y,z>` - `snapshot_from_grbl_status()`
    only trusts the leading `<State|` token, so this fixture is what
    actually exercises that "starts with `<`, contains `|`" contract
    against a real frame rather than a hand-written string.
  * A real state machine: Idle -> Run, Run <-> Hold (`!`/`~`), any ->
    Alarm (a simulated hard-limit / E-STOP) which only `$X` or Ctrl-X
    clears, and a Door:1 hold that blocks resume until the door closes.
  * Line-buffered `$`/G-code channel answering `ok` / `error:N` /
    `[GC:...]` / `ALARM:n` on its own frames.

`trigger_alarm()`, `open_door()`, `close_door()` and `set_machine_position()`
let a test drive the *physical* side the bridge is meant to observe and
fail closed on, without any real hardware, OS pty or `pyserial` install.
"""

from __future__ import annotations

from collections import deque

_WELCOME = b"Grbl 1.1h ['$' for help]\r\n"

# Real-time command bytes (github.com/gnea/grbl/wiki/Grbl-v1.1-Interface).
_RT_STATUS = 0x3F  # ?
_RT_FEED_HOLD = 0x21  # !
_RT_CYCLE_START = 0x7E  # ~
_RT_SOFT_RESET = 0x18  # Ctrl-X

# A curated, real subset of `$$` output (github.com/gnea/grbl/wiki/
# Grbl-v1.1-Configuration) - enough to be recognisably real without
# pretending this fixture is a full firmware.
_SETTINGS_DUMP = (
    "$0=10", "$1=25", "$10=1", "$11=0.010", "$20=0", "$21=0", "$22=1",
    "$100=250.000", "$101=250.000", "$102=250.000",
    "$110=500.000", "$111=500.000", "$112=500.000",
    "$130=200.000", "$131=200.000", "$132=200.000",
)


class GrblEmulator:
    """A ``SerialLike`` GRBL v1.1 controller. Not thread-safe by design -
    a real single serial line is not either."""

    def __init__(self) -> None:
        self._in = bytearray()            # unparsed inbound line bytes
        self._out: deque[bytes] = deque()  # framed outbound lines, oldest first
        self.state = "Idle"              # Idle|Run|Hold|Alarm|Door|Home|Jog|Check
        self.closed = False
        self._door_open = False
        self._alarm_code: int | None = None
        self.machine_position = (0.0, 0.0, 0.0)
        self.feed_rate = 0
        self.spindle_speed = 0
        self.line_log: list[str] = []     # every complete `$`/G-code line received
        self._out.append(_WELCOME)

    # ---- physical-side controls a test drives -----------------------------
    def trigger_alarm(self, code: int = 1) -> None:
        """Simulate a hard limit / E-STOP: GRBL latches ALARM and refuses
        motion until `$X` (unlock) or Ctrl-X (soft reset)."""
        self._alarm_code = code
        self.state = "Alarm"
        self._out.append(f"ALARM:{code}\r\n".encode("ascii"))

    def open_door(self) -> None:
        self._door_open = True
        if self.state in ("Idle", "Run"):
            self.state = "Door"

    def close_door(self) -> None:
        self._door_open = False
        if self.state == "Door":
            self.state = "Idle"

    def set_machine_position(self, x: float, y: float, z: float) -> None:
        self.machine_position = (x, y, z)

    def start_program(self) -> None:
        """Simulate a job actually running (native controller authority) so
        the bridge sees `Run` and gates resume/abort against it."""
        if self.state == "Idle":
            self.state = "Run"
            self.feed_rate = 500
            self.spindle_speed = 12000

    # ---- SerialLike ------------------------------------------------------
    def write(self, data: bytes) -> int:
        if self.closed:
            raise OSError("write on closed port")
        for byte in data:
            if byte in (_RT_STATUS, _RT_FEED_HOLD, _RT_CYCLE_START, _RT_SOFT_RESET):
                self._handle_realtime(byte)
            elif byte in (0x0A, 0x0D):  # \n or \r terminates a line
                if self._in:
                    self._handle_line(self._in.decode("ascii", errors="replace").strip())
                    self._in.clear()
            else:
                self._in.append(byte)
        return len(data)

    def readline(self) -> bytes:
        """One framed line, oldest first. Empty bytes when nothing is
        queued - a real `pyserial` connection returns `b""` on its read
        timeout, which `GrblSerialProbe.query_status()` already treats as
        an unparseable/empty status (fail closed)."""
        if self.closed:
            raise OSError("readline on closed port")
        return self._out.popleft() if self._out else b""

    def close(self) -> object:
        self.closed = True
        return None

    # ---- protocol internals -------------------------------------------------
    def _handle_realtime(self, byte: int) -> None:
        if byte == _RT_STATUS:
            self._out.append(self._status_frame())
        elif byte == _RT_FEED_HOLD:
            # Real GRBL: Run -> Hold. No `ok`. Ignored in Alarm/Door.
            if self.state == "Run":
                self.state = "Hold"
        elif byte == _RT_CYCLE_START:
            # Real GRBL: resume only from a real Hold, and never while the
            # safety door is still open.
            if self.state == "Hold" and not self._door_open:
                self.state = "Run"
        elif byte == _RT_SOFT_RESET:
            # Ctrl-X: re-emit the welcome, clear a soft state. A latched
            # ALARM survives a soft reset in real GRBL until `$X`/homing,
            # so keep Alarm if one is set.
            self._in.clear()
            self._out.append(_WELCOME)
            if self._alarm_code is None:
                self.state = "Door" if self._door_open else "Idle"
            else:
                self.state = "Alarm"

    def _handle_line(self, line: str) -> None:
        self.line_log.append(line)
        if line in ("", "\r"):
            self._out.append(b"ok\r\n")
        elif line == "$$":
            for entry in _SETTINGS_DUMP:
                self._out.append(f"{entry}\r\n".encode("ascii"))
            self._out.append(b"ok\r\n")
        elif line == "$G":
            self._out.append(b"[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n")
            self._out.append(b"ok\r\n")
        elif line == "$I":
            self._out.append(b"[VER:1.1h.20190825:]\r\n[OPT:VNMPS,15,128]\r\n")
            self._out.append(b"ok\r\n")
        elif line == "$X":
            if self._alarm_code is not None:
                self._alarm_code = None
                self.state = "Door" if self._door_open else "Idle"
                self._out.append(b"[MSG:Caution: Unlocked]\r\n")
            self._out.append(b"ok\r\n")
        elif line in ("$H",):
            self.state = "Home"
            self.machine_position = (0.0, 0.0, 0.0)
            self.state = "Idle"
            self._out.append(b"ok\r\n")
        elif self._alarm_code is not None:
            # Real GRBL rejects everything but `$X`/`$H`/real-time while
            # ALARM is latched.
            self._out.append(f"error:{9}\r\n".encode("ascii"))  # 9 = G-code locked out during alarm
        elif line[0] in ("G", "M", "F", "S", "T", "X", "Y", "Z", "N"):
            self._out.append(b"ok\r\n")
        elif line.startswith("$"):
            self._out.append(b"ok\r\n")
        else:
            self._out.append(f"error:{20}\r\n".encode("ascii"))  # 20 = unsupported command

    def _status_frame(self) -> bytes:
        x, y, z = self.machine_position
        state = self.state
        if state == "Hold":
            state = "Hold:0"
        elif state == "Door":
            state = "Door:1" if self._door_open else "Door:0"
        elif state == "Alarm":
            state = "Alarm"
        frame = (
            f"<{state}|MPos:{x:.3f},{y:.3f},{z:.3f}"
            f"|FS:{self.feed_rate},{self.spindle_speed}"
            f"|WCO:0.000,0.000,0.000>\r\n"
        )
        return frame.encode("ascii")
