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
  * Real work coordinate offsets (`G54`-`G59`, `G10 L2 P<n>`, `G92`/
    `G92.1`) that actually change the status frame's `WCO` field, instead
    of a hardcoded zero - a motion command's target is computed in work
    space and converted through the active offset, same as real GRBL.
  * A real, bounded planner buffer (`planner_buffer_size`, default 16
    blocks like GRBL's own `BLOCK_BUFFER_SIZE`): motion lines queue and
    `ok` immediately once there's room, but a block accepted while the
    buffer is already full withholds its `ok` until `complete_next_block()`
    frees a slot - the real backpressure a G-code streamer must respect,
    and a soft reset discards the whole queue rather than resuming it.

`trigger_alarm()`, `open_door()`, `close_door()`, `set_machine_position()`
and `complete_next_block()` let a test drive the *physical* side the
bridge is meant to observe and fail closed on, without any real hardware,
OS pty or `pyserial` install.
"""

from __future__ import annotations

import re
from collections import deque

_WELCOME = b"Grbl 1.1h ['$' for help]\r\n"

# A curated G-code word tokenizer - real GRBL parses letter+number pairs
# with or without spaces between them ("G1 X10 Y20" and "G1X10Y20" are both
# valid); this fixture only needs enough of that to recognize the words it
# actually acts on below (motion targets, G92/G10 offsets, L/P selectors).
_WORD_RE = re.compile(r"([A-Za-z])\s*(-?\d+\.?\d*)")


def _tokenize_gcode(line: str) -> list[tuple[str, float]]:
    words: list[tuple[str, float]] = []
    for letter, number in _WORD_RE.findall(line):
        try:
            words.append((letter.upper(), float(number)))
        except ValueError:
            continue
    return words


# GRBL's real motion planner block-buffer capacity (BLOCK_BUFFER_SIZE in
# grbl/planner.h, modern GRBL v1.1 default).
_BLOCK_BUFFER_SIZE = 16

_WCS_NAMES = ("G54", "G55", "G56", "G57", "G58", "G59")
_MOTION_G_VALUES = (0.0, 1.0, 2.0, 3.0)

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
        # Work coordinate systems (G54-G59, real GRBL default is 6 slots)
        # plus the separate G92 offset that stacks on top of whichever one
        # is active - together these are what a real status frame's WCO
        # field reports (`MPos - WCO = WPos`), never a hardcoded zero.
        self._work_coordinate_offsets: dict[str, tuple[float, float, float]] = {
            name: (0.0, 0.0, 0.0) for name in _WCS_NAMES
        }
        self._active_wcs = "G54"
        self._g92_offset = (0.0, 0.0, 0.0)
        # Real GRBL's planner: motion blocks are accepted (and `ok`'d) up to
        # `planner_buffer_size` blocks ahead of what has physically executed.
        # A block accepted while the buffer is already full still queues,
        # but its `ok` is withheld until complete_next_block() frees a slot -
        # the real backpressure a G-code streamer must respect. Only used by
        # tests that explicitly exercise it; start_program()'s own Run/idle
        # toggle for "some other real controller/UI already streamed a job"
        # observation scenarios never touches this queue at all.
        self.planner_buffer_size = _BLOCK_BUFFER_SIZE
        self._planner_queue: deque[tuple[float, float, float]] = deque()
        self._pending_ok_count = 0
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

    def complete_next_block(self) -> None:
        """Simulate the oldest queued planner block physically finishing:
        the machine reaches its target and, if the buffer was full when
        that block was accepted, its withheld `ok` is now released. Real
        GRBL never advances the planner outside Run - raises loudly on
        misuse instead of silently faking progress during Hold/Alarm/Door,
        the same fail-closed spirit as the rest of this fixture."""
        if self.state != "Run":
            raise RuntimeError(
                f"cannot complete a motion block while state={self.state!r} - "
                "real GRBL never advances the planner outside Run"
            )
        if not self._planner_queue:
            raise RuntimeError("no queued motion block to complete")
        self.machine_position = self._planner_queue.popleft()
        if self._pending_ok_count > 0:
            self._pending_ok_count -= 1
            self._out.append(b"ok\r\n")
        if not self._planner_queue and self._pending_ok_count == 0:
            self.state = "Idle"
            self.feed_rate = 0
            self.spindle_speed = 0

    def _current_wco(self) -> tuple[float, float, float]:
        wx, wy, wz = self._work_coordinate_offsets[self._active_wcs]
        gx, gy, gz = self._g92_offset
        return (wx + gx, wy + gy, wz + gz)

    def _current_work_position(self) -> tuple[float, float, float]:
        mx, my, mz = self.machine_position
        wox, woy, woz = self._current_wco()
        return (mx - wox, my - woy, mz - woz)

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
            # so keep Alarm if one is set. Real GRBL also discards its
            # entire planner buffer on reset - any motion still queued is
            # simply gone, not resumed; a real streamer must restart it.
            self._in.clear()
            self._planner_queue.clear()
            self._pending_ok_count = 0
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
        elif line.strip() in _WCS_NAMES:
            self._active_wcs = line.strip()
            self._out.append(b"ok\r\n")
        elif line.startswith("G92.1"):
            self._g92_offset = (0.0, 0.0, 0.0)
            self._out.append(b"ok\r\n")
        elif line.startswith("G92"):
            self._apply_g92(_tokenize_gcode(line))
            self._out.append(b"ok\r\n")
        elif line.startswith("G10"):
            self._apply_g10(_tokenize_gcode(line))
            self._out.append(b"ok\r\n")
        elif self._is_motion_line(line):
            self._enqueue_motion(_tokenize_gcode(line))
        elif line[0] in ("G", "M", "F", "S", "T", "X", "Y", "Z", "N"):
            self._out.append(b"ok\r\n")
        elif line.startswith("$"):
            self._out.append(b"ok\r\n")
        else:
            self._out.append(f"error:{20}\r\n".encode("ascii"))  # 20 = unsupported command

    def _is_motion_line(self, line: str) -> bool:
        words = _tokenize_gcode(line)
        if not words or words[0][0] != "G" or words[0][1] not in _MOTION_G_VALUES:
            return False
        return any(letter in ("X", "Y", "Z") for letter, _ in words[1:])

    def _apply_g92(self, words: list[tuple[str, float]]) -> None:
        # G92: the commanded axis values become the CURRENT machine
        # position's work coordinate - i.e. offset = machine - commanded.
        # An axis not mentioned keeps its existing work coordinate, so its
        # offset must not change either (real GRBL semantics).
        mx, my, mz = self.machine_position
        ox, oy, oz = self._g92_offset
        for letter, value in words:
            if letter == "X":
                ox = mx - value
            elif letter == "Y":
                oy = my - value
            elif letter == "Z":
                oz = mz - value
        self._g92_offset = (ox, oy, oz)

    def _apply_g10(self, words: list[tuple[str, float]]) -> None:
        # Curated subset: only `G10 L2 P<1-6> [X..] [Y..] [Z..]` - setting a
        # work coordinate system's own stored offset directly. Anything else
        # (L20, no P, P out of range) is a real-GRBL-recognized command this
        # fixture doesn't model further; still `ok`, matching the same
        # curated-but-real spirit as `$$`'s settings dump above.
        l_value = next((v for w, v in words if w == "L"), None)
        p_value = next((v for w, v in words if w == "P"), None)
        if l_value != 2.0 or p_value is None or not 1.0 <= p_value <= 6.0:
            return
        wcs = _WCS_NAMES[int(p_value) - 1]
        x, y, z = self._work_coordinate_offsets[wcs]
        for letter, value in words:
            if letter == "X":
                x = value
            elif letter == "Y":
                y = value
            elif letter == "Z":
                z = value
        self._work_coordinate_offsets[wcs] = (x, y, z)

    def _enqueue_motion(self, words: list[tuple[str, float]]) -> None:
        target = self._motion_target_machine_position(words)
        buffer_was_full = len(self._planner_queue) >= self.planner_buffer_size
        self._planner_queue.append(target)
        if self.state == "Idle":
            self.state = "Run"
        if buffer_was_full:
            # Real backpressure: this block is queued, but its `ok` is
            # withheld until complete_next_block() frees a slot - a
            # streamer that ignores this and keeps writing anyway would
            # overflow a real controller's serial RX buffer.
            self._pending_ok_count += 1
        else:
            self._out.append(b"ok\r\n")

    def _motion_target_machine_position(self, words: list[tuple[str, float]]) -> tuple[float, float, float]:
        # G-code axis words command WORK coordinates; an axis not
        # mentioned keeps its current work coordinate (real GRBL modal
        # semantics), then the active WCO converts back to machine space.
        wx, wy, wz = self._current_work_position()
        for letter, value in words:
            if letter == "X":
                wx = value
            elif letter == "Y":
                wy = value
            elif letter == "Z":
                wz = value
        wox, woy, woz = self._current_wco()
        return (wx + wox, wy + woy, wz + woz)

    def _status_frame(self) -> bytes:
        x, y, z = self.machine_position
        wcx, wcy, wcz = self._current_wco()
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
            f"|WCO:{wcx:.3f},{wcy:.3f},{wcz:.3f}>\r\n"
        )
        return frame.encode("ascii")
