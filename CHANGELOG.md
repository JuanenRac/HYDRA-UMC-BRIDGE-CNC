<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - Change history
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# Changelog

## [0.1.1] - A protocol-faithful GRBL v1.1 emulator, not a one-line fake

Until now the only serial test double here was `FakeSerial` - one canned
status line, no state machine, no framing. It proves this bridge's own
parsing/gating in isolation but never that the real `serial_transport.py`
code survives a controller that behaves like the real thing. New
`tests/grbl_emulator.py` is a genuine `SerialLike` GRBL v1.1 controller:
a `Grbl 1.1h ['$' for help]` welcome banner that a soft reset re-emits;
real-time bytes (`?`/`!`/`~`/Ctrl-X) processed immediately and
out-of-band, with `!`/`~` producing no `ok` exactly like real GRBL; `?`
returning a real `<State|MPos:x,y,z|FS:f,s|WCO:...>` status frame; a real
Idle -> Run <-> Hold state machine; a latched `ALARM:n` that only `$X`
or homing clears; a `Door:1` hold that blocks resume until the door
closes; and a line channel answering `ok`/`error:9`/`error:20`/`[GC:...]`
on its own frames. `trigger_alarm()`/`open_door()`/`close_door()`/
`start_program()` let a test drive the physical side. New
`tests/test_grbl_emulator.py` runs this bridge's real `GrblSerialProbe`/
`GrblRealtimeControl` end to end against it (8 tests): the RUN->HOLD->RUN
feed-hold cycle, resume refused when not actually holding, a latched
alarm surfacing as `FAULT` and surviving a soft reset, the safety door
blocking resume, and the port going dead failing closed. 65 tests total.

## [0.1.0] - V07-019: a non-conforming serial write was still reported as executed

A second, closer review found REV-006's own fix only
caught a reported SHORT byte count from `GrblRealtimeControl._write()` -
`None`/a `bool`/a mismatched-but-real int (or any other non-conforming
return value) was still silently trusted as `executed=True`, just
because it wasn't a *short* int. A real pyserial connection's `write()`
always returns the real int byte count, equal to `len(command)` for a
fully-sent realtime command - both are now required exactly. For a real
feed-hold/soft-reset/resume byte this made the safety evidence itself
potentially misleading.

Fixed the real fakes in this bridge's own test suite to report a real
byte count by default (an explicit correction: fix the
fakes to comply with the production contract, don't relax production
for them), rather than keeping the old "an unreported count is trusted"
escape hatch.

## [0.0.9] - REV-005/REV-006: real regressions

Closer review reproduced 2 real regressions in this
bridge's own real interlock/serial logic (each with a real fake-connection
probe, no hardware involved). Both fixed here, each with a new regression
test:

- **REV-005 [P0]:** `refresh_status()` used to read the live
  `estop`/`door_closed` callables BEFORE calling `query_status()`, which
  then blocks on a real `write()`/`readline()` round trip for up to the
  connection's own timeout. A real E-STOP hit or door opened DURING that
  block went completely unnoticed by the snapshot the call returns -
  reproduced with a fake connection whose `readline()` flips both
  signals as a side effect (modelling the physical event landing while
  the real call is in flight): `cycle_start_resume` still reported
  `allowed=true`/`executed=true` and sent the real `b"~"` byte. Fixed:
  `GrblSerialProbe.query_status()` now takes the signals as callables and
  calls them itself, immediately AFTER the blocking I/O completes, never
  before it.
- **REV-006 [P1]:** a real `0`-byte (or short) serial write raises no
  exception at all - the connection is fine, nothing actually reached
  the wire - so it used to be reported as `executed=True` purely because
  nothing crashed. For a real feed-hold/soft-reset/resume byte, this made
  the safety evidence itself misleading. Fixed: a real, reported short
  write count now produces `executed=False` with a clear reason; a
  fake/stub that does not report a byte count (returns `None`, as every
  existing test's own fake already did) is still trusted unchanged.
- 6 new regression tests (54 total), each reproducing its own
  exact scenario before the fix and passing after it.

## [0.0.8] - CNC-01: real interlocks are re-read before every real command

- **CNC-01 (P0):**
  `cmd/cycle_start_resume` and `cmd/job` both reused
  `self._last_snapshot` - whatever door/E-STOP state was last queried,
  possibly from an unrelated `cmd/status` poll seconds or minutes
  earlier - instead of re-reading the real, current state right before
  authorizing a real command. Reproduced: door closed + no E-STOP at an
  earlier poll, then the door opens and E-STOP activates, then
  `cycle_start_resume` arrives - the stale snapshot still said HOLDING,
  so the real `b"~"` byte was sent to the serial port despite the
  CURRENT physical state being unsafe; the same gap let `cmd/job` gate a
  new job against a stale IDLE reading too. Fixed: `refresh_status()` is
  now called live, unconditionally, immediately before both decisions -
  `self._last_snapshot` itself was removed entirely (it had no other
  reader left) rather than leaving unused cached state a future edit
  could be tempted to read from again. 2 new regression tests
  (47 total, up from 45) reproduce the exact door-opens-mid-flight race
  for both commands and prove the real byte is never written / the job
  is refused.
- **LANG-05:** the English README's own
  "observation helpers are evidence normalizers" paragraph, linking
  `docs/CONTROLLER_EVIDENCE_BOUNDARY.md`, was missing from all 6
  translations even though the file was already listed in each one's own
  Directory Structure tree. Added the equivalent paragraph + link to all
  6.

- **`tools/bump_version.py`'s own auto-generated CHANGELOG heading
  embedded a literal calendar date** (`date.today().isoformat()`) into
  this public file - every real entry here is otherwise dated only by
  its position, never a literal date. The same bug, copied from the same
  template, found and fixed in the same pass across all 5 sibling
  bridges (BRIDGE-CNC/LASER/OPENPNP/PRINTER3D/ROS2) plus HYDRA-UMC-SDK
  and HYDRA-UMC-OS. Removed before it could ever actually land one (no
  prior real build in this repo's own history shows the script running
  mechanically without a hand-written entry replacing the stub first).
  Repo-hygiene fix, no runtime code changed, no version bump.
- **`run_forever()`'s initial MQTT connect now retries with backoff**
  (`connect_with_retry()`, new) - this bridge's process used to die
  outright if it started before HYDRA-UMC-MQTT-BROKER was listening yet,
  a real race between two independent systemd units with no ordering
  guarantee across a reboot. Only `OSError` (what an unreachable broker
  actually raises) is retried; anything else surfaces immediately as a
  real bug. Once connected, paho-mqtt's own `loop_forever()` already
  handles a later mid-session drop on its own - only the first connect
  needed this.

## [0.0.7] - Real MQTT transport over the real broker

- **`mqtt_transport.py`** (new) - reaches this bridge's already-real logic
  (`GrblRealtimeControl.feed_hold`/`soft_reset`/`cycle_start_resume`,
  `CncCellBridge.plan`) over `HYDRA-UMC-MQTT-BROKER`, per the ecosystem's
  own "MQTT via the real broker, real commands included" decision -
  `hydra/bridges/cnc/cmd/{status,feed_hold,soft_reset,cycle_start_resume,
  job}` in, `hydra/bridges/cnc/state` (retained) and `.../cmd/<verb>/result`
  out. `CncMqttBridge.handle_message()` is a pure(ish) topic dispatcher over
  an already-open `SerialLike` connection - fully testable with the same
  in-memory fake `test_serial_transport.py` already uses, no real broker or
  serial port required. Adds no new physical authority: every command sent
  is one `serial_transport.py`/`cell.py` already implemented, and the same
  real-time-only boundary (never streams G-code) applies unchanged.
  `run_forever()` is the thin real-I/O glue, lazily importing the new
  optional `paho-mqtt` dependency the same way `open_serial_port()` already
  lazily imports `pyserial`. 14 new tests.

## [0.0.6] - Real GRBL serial transport (pre-real: connected, not simulated)

- **`serial_transport.py`** (new) - this bridge's first real transport:
  `GrblSerialProbe.query_status()` sends GRBL's real-time status query byte
  (`?`) over an already-open serial connection and parses the real response
  through the existing `snapshot_from_grbl_status()`. `GrblRealtimeControl`
  sends only GRBL's own real-time single-byte control characters
  (researched against
  [github.com/gnea/grbl/wiki/Grbl-v1.1-Interface](https://github.com/gnea/grbl/wiki/Grbl-v1.1-Interface#real-time-commands)):
  `feed_hold()`/`soft_reset()` are always allowed (same de-escalation
  reasoning as `ABORT`/`HOLD_POSITION` elsewhere); `cycle_start_resume()`
  uses a standalone gate requiring a genuinely `HOLDING` machine (same
  reasoning as the sibling PRINTER3D bridge's `resume_job()`). This never
  streams a G-code program - LinuxCNC/the native controller keeps all
  real-time trajectory, limits, spindle and safety authority, unchanged.
  `open_serial_port()` is the one place `pyserial` (new optional
  `[serial]` extra) is imported, lazily, degrading to a clear
  `RuntimeError` instead of a bare `ImportError` when it isn't installed.
- **Real bug found while wiring this up, fixed in `cell.py`**: a real GRBL
  controller always sends a numeric substate suffix for `Hold`/`Door`
  (`Hold:0`/`Hold:1`, `Door:0`..`Door:3`), never the bare token - the
  existing exact-match state check silently missed the suffixed form
  entirely and fell through to `OFFLINE`. Fixed by splitting off the
  substate before comparison; safe for every other token too since none of
  them use a colon.
- 15 new regression tests (serial transport, against an in-memory fake
  connection - no OS pty/socat/hardware needed; plus the real Hold/Door
  substate fix) - 28/28 tests passing (1 skipped when pyserial happens to
  be installed, proving the lazy-import failure path only when it isn't).

## [0.0.5] - Real GRBL Alarm/Jog/Home/Hold/Door states

- Added a read-only MTConnect execution normalizer for saved controller
  evidence. Known execution states map into the SDK gate; unknown values and
  missing independent safeguards remain fail-safe/offline.
- **`cell.py`** - `CncSnapshot.machine_state()` now recognizes GRBL v1.1's
  full real status-report vocabulary, researched against
  [github.com/gnea/grbl/wiki/Grbl-v1.1-Interface](https://github.com/gnea/grbl/wiki/Grbl-v1.1-Interface):
  `Jog`/`Home` (real active-motion states, previously swallowed by the
  `OFFLINE` fallback) now map to `RUNNING`; `Alarm` (GRBL's own most
  safety-critical report - limit trip, lost position, unresolved E-STOP,
  also previously indistinguishable from "not reporting") now maps to
  `FAULT`; `Door` (GRBL's own real safety-interlock state) now maps to
  `SAFE_STOP` as a defensive second signal alongside this bridge's
  existing `door_closed` input.
- `Hold` (and MTConnect's `FEED_HOLD`/`INTERRUPTED`, which already
  normalized to the same `"HOLD"` token) now correctly maps to `HOLDING`
  instead of `RUNNING` - a paused program is a real, distinct condition
  from an actively running one, matching the same real
  `print_stats.state=paused` -> `HOLDING` fix already made in the sibling
  PRINTER3D bridge. This does not change any dispatch decision
  (`evaluate_job()` only permits productive work on `IDLE` either way),
  only the accuracy of the reported state.
- 6 new/updated regression tests - 16/16 tests passing.

## [0.0.4]

- Added `docs/BRIDGE_GUIDE.md`, defining evidence scope, controller
  compatibility, script conventions and the CNC hardware acceptance gate.
- Removed the duplicated terminal BUILD & RUN section from all seven README files.
- Added an offline CLI for inspecting saved controller-evidence JSON, including
  supplied GRBL state evidence, without opening a serial, HAL or network link.
- Added CLI contract coverage; the full suite now has nine tests.
- Synchronized package metadata, ecosystem manifest and all seven README files.

## [0.0.3]

- Added read-only normalization of mapping evidence and supplied GRBL status
  lines; the module opens no serial, HAL or network connection.
- Made missing or non-Boolean E-STOP and door signals fail closed before an
  observed CNC state can be trusted.
- Added four deterministic evidence-boundary tests; the suite now has eight
  tests. Package metadata, manifest and all seven README files are synchronized.

## [0.0.2]

- Made an unexpected non-text controller state fail safe as `OFFLINE` instead
  of raising while evaluating the CNC cell boundary.
- Synchronized the English README and all six translated README files with
  the current version.
- Successful incremental build: synchronized package metadata and
  `hydra-umc.project.json`.

## [0.0.1]

- Added fail-safe CNC cell snapshot and SDK safety-gate tests.
- Added non-mutating build-test scripts and CI SDK checkout.
- Standardized README (all 7 languages) and project banner to match the
  rest of the ecosystem's established-project structure.
- Promoted to `established`: manifest, docs, build-test/CI, real local
  verification and no private-doc references all confirmed - no
  functional gap found in this bridge's own small, SDK-delegated core.
