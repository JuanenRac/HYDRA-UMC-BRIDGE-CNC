# Changelog

All notable changes to this project are documented here.

## [Unreleased]

- **A result keeps its QoS when a `request_id` is added to it:** the copy that carries the id used to fall back to QoS 0, which would have silently lowered any result a bridge publishes at QoS 1 or 2.

- **The MQTT transport is tested against a real broker:** `tests/test_broker_integration.py` starts a `HYDRA-UMC-MQTT-BROKER` on a free local port and runs the bridge through the shared client: the retained availability, a command answered at QoS 0, 1 and 2, a retained command that is only a replay, messages published at each QoS, the last will after an unclean loss of the link and the way back after the broker went down and up. It skips itself when node, `paho-mqtt` or the installed broker (`HYDRA_UMC_TEST_BROKER_DIR`, default the folder next to this one) are missing.

- **The bridge runs as a service.** `python -m hydra_umc_bridge_cnc --config <file>` (or the `hydra-umc-bridge-cnc` command) starts it from one TOML file: the controller (a GRBL board on a serial port, CNCjs or LinuxCNC), the sources of the E-STOP and of the door (a GPIO line with its declared unsafe level, or a small file), the sharing of the serial port with a G-code sender, the spindle gate, MTConnect, the MQTT broker and where the state of the cell comes from. A key it does not know is refused, a signal source has to declare its wiring, and everything fails closed: a signal with no source is E-STOP pressed or door open, and a signal or cell-state file that is missing, unknown or stale gives the unsafe answer. `--check` validates the file. `deploy/hydra-umc-bridge-cnc.service` is the systemd unit (`Restart=on-failure`, a closed device policy, memory limits) and `deploy/bridge-cnc.example.toml` the example file. Covered by `tests/test_service.py` against the GRBL emulator.

- **A stopped machine is no longer read as idle:** an MTConnect `STOPPED` execution (stopped until an operator resets it) used to be read as `IDLE`, so a consumer could treat the machine as free for work after a stop. It is now its own `STOPPED` machine state: no work is allowed on it, it is not resumed like a feed hold, and the MTConnect view publishes it as `STOPPED`. The state comes with the SDK release that adds it; until the pin moves the bridge reports `FAULT` for it, which refuses the same things.

- **The serial port of a GRBL board can be shared with a G-code sender:** `multiplexer.py` owns the port and serves one sender at a time over TCP (loopback only by default) while the bridge keeps its status queries and its three real-time bytes; the bridge cannot write anything else through it. It is off by default and is switched on with `HYDRA_UMC_CNC_PORT_SHARING=multiplexer`. Tested against the GRBL emulator; not run on a real board.

## [0.2.1] - Jobs through the shared intake, answers matched to their requests and a test of the real serial port

- **Jobs go through the shared intake of the SDK:** the check of a malformed job and of a repeated `idempotency_key` is the SDK's `JobIntake` instead of code of this bridge (the keys are no longer prefixed with `job:`).

- **A caller can match each answer to its request:** a command payload that is a JSON object may carry a `request_id` (a plain token of up to 64 characters: letters, digits, `.`, `_`, `:`, `-`); every result the bridge publishes for that message carries it back, also the one stored for a repeated idempotency key. The state topics are not changed. 6 tests.
- **The real serial port is tested over a pseudo-terminal:** a new test opens the port with pyserial against the GRBL emulator behind an `os.openpty()` pair and covers the status query, a real-time feed hold and the reopening of a port that went away; it runs where POSIX and pyserial are available (the CI installs pyserial) and is skipped elsewhere.

- **README in the universal layout:** the README and its seven translations follow the layout that the four families share (22 sections in a fixed order), with an architecture picture and a repository-structure picture drawn from `docs/architecture.json` and `docs/structure.json`; the CI validator checks both pictures and the file tree. Where the repository has a service or an MQTT transport, the README now says how to start it and which environment variables it reads.

- **The layout of the README is checked in the CI:** the validator runs `tools/universal_readme_check.py` and lists, as a warning, what differs from the universal layout of the README.

## [0.2.0] - Controller links and spindle gate

- **Controller links.** The bridge talks to its machine through `ControllerLink`: GRBL on a serial port (the default), a LinuxCNC installation (`linuxcnc_link.py`: reads `linuxcnc.stat`, sends only pause, abort and resume through `linuxcnc.command`, never runs a program) and a CNCjs server that owns the port (`cncjs_link.py`: reads its `controller:state` broadcast and sends only `feedhold`, `reset` and `cyclestart`, so the bridge works beside the sender that runs the programs). A resume needs a READY cell and a machine that is really holding on every link, and an old or missing reading is never an idle machine. Tested against `SimulatedLinuxcnc` and `SimulatedCncjs`; neither has touched a real LinuxCNC or CNCjs.
- **Spindle gate.** A load or an unload is refused when the controller is idle but reports a turning spindle or a moving feed. `HYDRA_UMC_CNC_SPINDLE_GATE`: `off`, `when_known` (default) or `required`. The snapshot now carries the spindle speed, the feed rate, the machine and work positions, the work offset and the overrides of the status report; a field the controller did not send is `None`, never zero.
- **MTConnect view.** `mtconnect_publish.py` turns the snapshot into MTConnect observations and serves them as SHDR over TCP (`ShdrAdapter`, read only), and, with `HYDRA_UMC_CNC_MTCONNECT=1`, publishes them retained on `hydra/bridges/cnc/mtconnect`. A controller that is not reporting is `UNAVAILABLE`, never `READY`.
- The serial port, the reconnecting connection, the status query and the real-time byte writer are the shared `hydra_umc_sdk.serial_link` instead of a copy of their own; the laser bridge reads GRBL with the same code. Needs an SDK newer than 0.3.6.
- Building no longer changes the version; raising it is the explicit `bump` command (`python tools/hydra_project_tool.py bump`). The build scripts only report the version they built, and CI fails if a build script runs a bump.
- **The MQTT transport comes from the SDK.** Connecting with retries, the publish record and the message dispatcher are the shared `hydra_umc_sdk.bridge_transport` of SDK 0.3.6 instead of a copy of their own; what is specific to this bridge (its topics and what it does with a message) stays here, and its tests are unchanged.

- **Pinned to SDK 0.3.6** (dependency and contract checkout in CI). From this version the shared bridge gate fails closed: a productive job is refused when the bridge did not pass the state it read from its machine. The bridge passes what the controller reported.
- The banner follows the animated style of the ecosystem banners, with the name, the kind and the technologies of the project.
- Added `tools/hydra_project_tool.py`: the same `validate`, `test`, `version`, `status` and `bump` commands in every repository. The checks run with `HYDRA_UMC_CI=1` and fail if the version changes; only `bump` changes it.
- The SDK is now fetched at a release tag instead of the main branch, so a change of the SDK cannot break this repository by itself.
- Successful incremental build: synchronized package metadata and `hydra-umc.project.json`.

## [0.1.9]

- Successful incremental build: synchronized package metadata and `hydra-umc.project.json`.
- The licence is written as GPL-3.0-or-later in every header and in the package metadata.

## [0.1.8] - Maturity lowered to functional

- Maturity is functional: the code, tests and documentation are in place, but nothing here has been verified on real hardware yet, so the manifest no longer claims the established level.

## [0.1.7] - The status reply that is read is the one that was asked for, and a missing cable fails closed

- **The status reply lines up with the question.** GRBL talks without being asked: the banner after every reset (opening the port resets an Arduino), `ok`/`error:N`, `[MSG:...]`, `ALARM:n`, and the late answer to an earlier `?`. The bridge used to take the next line as the answer, so once one extra line was in the buffer every query returned the previous query's reply (a machine that was running could be reported idle). It now discards what is already waiting, sends `?` and reads until a line that is a status report (`<...>`) arrives, keeping the newest one when several are waiting; no report is `OFFLINE`.
- **A missing cable no longer ends the bridge.** `ReconnectingSerialConnection` turned "no such device" into a `RuntimeError` that escaped the `OSError` handling and ended the MQTT loop at the very moment a feed hold was needed. Any failure to (re)open the port is now an `OSError` that wraps the cause, and the status query and the real-time commands fail closed on any failure. Two threads that see the same dead port reopen it once.
- **No reset when the port opens.** The port is created closed, DTR/RTS and flow control are declared off, and only then is it opened (an Arduino-based GRBL board resets on DTR).
- **A feed hold says what the controller reported.** After `!` the bridge asks for the status and `cmd/feed_hold/result` carries `controller_state_after` and `confirmed`; "a byte was written" is no longer read as "the machine stopped". One status query at a time uses the serial port.
- **A stop never waits.** Messages are handled off the network thread; `cmd/feed_hold` and `cmd/soft_reset` have a lane of their own, so a status query waiting for the controller does not delay them.
- **The loop survives anything a client sends.** Deeply nested JSON, a payload larger than 16 KiB, an unreadable payload and any unexpected failure while handling a message are answered with a refusal on the result topic instead of ending `loop_forever()`.
- **`cmd/job` answers say what they are.** The result carries `"mode": "gate-only"`; an `ABORT` says `"forwarded": false` and that nothing was stopped, and points to `cmd/feed_hold`. A failure after the idempotency key was claimed leaves an explicit answer for the key.
- **A retained state can be judged.** `state` carries `observed_at` (epoch milliseconds); `availability` is `online` once connected and the broker publishes `offline` (Last Will) when the bridge dies.
- **paho-mqtt.** The client uses the version 2 callback API, which also starts with paho-mqtt 2.0; the extra now requires `paho-mqtt>=2.1,<3`.
- `tools/inspect_controller_evidence.py` reports the canonical `OFFLINE` value on failure; the unused `subprocess` import of `tools/ci_validate.py` is gone; `LICENSE` now carries the complete GPL-3.0-or-later text.
- README (all eight languages) and `docs/` updated: the state mapping (`HOLD` is `HOLDING`), test counts and test file list, the single-serial-port model explained, no LinuxCNC claim, no HYDRA-UMC-SERVER path that does not exist, and the existing command path acknowledged in the guide.
- 25 new tests (`tests/test_hardening.py`).

## [0.1.6] - Connects to a broker that demands credentials: user, password and TLS from the environment

- The MQTT client now takes its credentials and TLS from the environment of the service (`HYDRA_MQTT_USERNAME`, `HYDRA_MQTT_PASSWORD` or `HYDRA_MQTT_PASSWORD_FILE`, `HYDRA_MQTT_TLS_CA`) through `hydra_umc_sdk.mqtt_connection`, so it can reach a production HYDRA-UMC-MQTT-BROKER, which refuses anonymous clients and evaluates its ACL for the identity of the credential.
- An explicit `username`/`password` argument still wins, and a password without a user name is refused.
- The client ID must start with the `clientIdPrefix` of the bridge's credential.
- New `tests/test_mqtt_environment.py` (a fake paho client, so it runs without paho-mqtt) proves that the environment credentials and the CA reach the client, that the explicit ones win, that nothing set is an anonymous plain connection, and that a lonely password is refused.

## [0.1.5] - A repeated idempotency key is answered once

- **Idempotency keys.** `CncMqttBridge` accepts an `IdempotencyStore` from HYDRA-UMC-SDK: the first time a job key arrives it is evaluated and its answer kept; a repeat (QoS 1 delivers some twice, a client may retry) gets the same answer and nothing is evaluated again. docs/BRIDGE_CONTRACT.md asked bridges to do this and none did.
- Three new tests (tests/test_idempotency.py).

## [0.1.4] - Reconnect by real USB device id instead of a static serial port name

- `resolve_serial_device_path()` resolves a stable USB device identifier
  (a Linux `/dev/serial/by-id/*` path, or a serial number/hwid substring
  matched against currently-enumerated ports) to whatever port name the OS
  currently has it under - a cable replug (or reboot re-enumerating USB in
  a different order) reassigning `COM3` to a different device, or moving
  this one to `COM5`, no longer breaks the connection permanently.
- `open_serial_port_by_id()` opens a real serial connection resolved this
  way.
- `ReconnectingSerialConnection` wraps any `SerialLike` factory and
  transparently reopens (via the same by-id resolution) after a real
  `OSError` from `write()`/`readline()`, retrying the failed call once on
  the healed connection - composes unchanged with `GrblSerialProbe`/
  `GrblRealtimeControl`, which already fail closed on a real OSError.
- 10 new tests. 91/91 `unittest` cases pass (`python tools/build_test.py`,
  up from 81).

## [0.1.3] - configurable MQTT authentication, and a retained command can no longer replay as a live one

- **.** `run_forever` had no way to authenticate against
  HYDRA-UMC-MQTT-BROKER's own real, opt-in `MQTT_AUTH_JSON` username/
  password CONNECT authentication - a broker deployed with credentials
  required was simply unreachable from this bridge. New optional
  `username`/`password` keyword arguments call paho-mqtt's own
  `username_pw_set()`; a `password` given without a `username` is
  rejected outright rather than silently connecting unauthenticated.
- **.** `on_connect`'s `subscribe("cmd/#")` makes the broker replay
  every currently-retained message on that wildcard immediately - on
  *every* reconnect, not just once at startup. A retained `cmd/job` (or
  any other `cmd/*` topic) would re-trigger a real physical decision with
  no new operator intent behind it. `handle_message()` now takes a
  `retained` flag and ignores any retained delivery before it reaches a
  real command; `on_message()` passes the real MQTT message's own
  `.retain` flag through.
- 7 new tests, confirmed to fail against the pre-fix code (6 real
  failures) via a local revert of just `mqtt_transport.py`, tests kept.
  81/81 `unittest` cases pass (`python tools/build_test.py`, up from 74).

## [0.1.2] - The GRBL emulator fixture now tracks real work offsets and a real planner buffer

`tests/grbl_emulator.py`: closes the two remaining real
gaps in this test double's own protocol fidelity.

- **Work coordinate offsets.** The status frame's `WCO` field used to be
  hardcoded to `0.000,0.000,0.000` no matter what a test sent - `G54`-`G59`
  selection, `G10 L2 P<n>` (set a work coordinate system's offset) and
  `G92`/`G92.1` now all actually change it, and a motion command's target
  is computed in work space and converted through whichever offset is
  active, same as real GRBL (`MPos = WPos + WCO`).
- **A real, bounded planner buffer.** Motion lines used to get an instant
  `ok` no matter how many were already in flight - now they queue against
  `planner_buffer_size` (16 blocks, matching GRBL's own
  `BLOCK_BUFFER_SIZE`), and a block accepted while the buffer is already
  full withholds its `ok` until the new `complete_next_block()` frees a
  slot: the real backpressure a G-code streamer must respect. A soft
  reset discards the whole queue rather than resuming it, and
  `complete_next_block()` refuses to advance outside `Run` (fails loudly
  instead of faking progress during Hold/Alarm/Door).

10 new tests. This bridge's own production code still never streams
G-code and never reads position (`serial_transport.py`'s own documented
boundary) - both additions are fixture-only realism, useful to any test
that wants to drive the emulator's physical side more precisely.

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

## [0.1.0] - A non-conforming serial write was still reported as executed

A second, closer review found the prior short-write fix only
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

## [0.0.9] - Two real regressions in interlock/serial logic

Closer review reproduced 2 real regressions in this
bridge's own real interlock/serial logic (each with a real fake-connection
probe, no hardware involved). Both fixed here, each with a new regression
test:

- **Fixed a real interlock-check-before-blocking-I/O race:** `refresh_status()` used to read the live
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
- **Fixed misleading safety evidence on a short serial write:** a real `0`-byte (or short) serial write raises no
  exception at all - the connection is fine, nothing actually reached
  the wire - so it used to be reported as `executed=True` purely because
  nothing crashed. For a real feed-hold/soft-reset/resume byte, this made
  the safety evidence itself misleading. Fixed: a real, reported short
  write count now produces `executed=False` with a clear reason; a
  fake/stub that does not report a byte count (returns `None`, as every
  existing test's own fake already did) is still trusted unchanged.
- 6 new regression tests (54 total), each reproducing its own
  exact scenario before the fix and passing after it.

## [0.0.8] - Real interlocks are re-read before every real command

- **Fixed a real stale-interlock-snapshot bug:**
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
- **Fixed a missing translation section:** the English README's own
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
