<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - Technical bridge guide
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-CNC Technical Guide

## Scope and operating model

`CncSnapshot` makes a CNC state usable by the shared SDK gate only after independent E-STOP and door evidence are present. `observation.py` normalizes an already-saved mapping or supplied GRBL status line. Missing or non-Boolean E-STOP/door values fail closed. The bridge coordinates auxiliary loading/unloading; it does not control trajectory, spindle, axes or G-code.

`CncSnapshot.machine_state()` recognizes GRBL v1.1's real full status vocabulary (github.com/gnea/grbl/wiki/Grbl-v1.1-Interface), including the real numeric substate GRBL always appends to `Hold`/`Door` (`Hold:0`/`Hold:1`, `Door:0`..`Door:3` - stripped before matching): `Idle` -> `IDLE`; `Run`/`Jog`/`Home` -> `RUNNING` (all three are real active-motion states); `Hold` -> `HOLDING` (a paused program, distinct from actively running); `Alarm`/`Fault`/`Error`/`Off` -> `FAULT`; `Door` -> `SAFE_STOP` (a defensive second signal alongside the bridge's own independent `door_closed` input). Any other token, including GRBL's `Check`/`Sleep`, stays `OFFLINE` - the same conservative fail-safe default used for every unrecognized signal in this bridge.

`serial_transport.py` is this bridge's first real transport: `GrblSerialProbe.query_status()` opens a real serial connection and sends GRBL's real-time status query byte (`?`); `GrblRealtimeControl` sends only GRBL's own real-time single-byte control characters - `feed_hold()`/`soft_reset()` (always allowed) and `cycle_start_resume()` (gated on a genuinely `HOLDING` machine). It never streams a G-code program; LinuxCNC or the native controller keeps all real-time trajectory, limits, spindle and safety authority. The GRBL-facing logic is written against a small `SerialLike` protocol so it is unit-testable with an in-memory fake connection - `pyserial` (optional `[serial]` extra) is only imported, lazily, inside `open_serial_port()`.

`mqtt_transport.py` reaches the exact same real serial commands over `HYDRA-UMC-MQTT-BROKER`, this bridge's second real transport. `CncMqttBridge.handle_message()` routes `hydra/bridges/cnc/cmd/{status,feed_hold,soft_reset,cycle_start_resume,job}` to the same `GrblRealtimeControl`/`CncCellBridge` calls above, publishing `hydra/bridges/cnc/state` (retained) and one `.../cmd/<verb>/result` per command - no new physical authority, only a new way to reach what already existed. `job` accepts a `BridgeJob` in the shared SDK's `job_to_dict()` wire shape and answers with a `GateDecision`. Dispatch is pure enough to unit-test with the same in-memory fake connection, no real broker required - `paho-mqtt` (optional `[mqtt]` extra) is only imported, lazily, inside `run_forever()`.

## Compatible software

The current parser accepts generic controller-state evidence, the leading state token of a supplied GRBL status line, and saved MTConnect `execution` values (`READY`/`STOPPED`, `ACTIVE`/`EXECUTING`, `FEED_HOLD`/`INTERRUPTED`, `FAULT`). MTConnect is read-only evidence only: it neither opens a controller connection nor offers a command path. LinuxCNC/HAL is an intended future controller integration, but is not connected today. Other controllers can be supported only through a documented read-only adapter that preserves independent guard and E-STOP signals.

## Scripts and verification

| Script | Purpose | Changes version/CHANGELOG? |
|---|---|---|
| `build-test.bat` / `build-test.sh` | Compile and run local safety tests | No |
| `build.bat` / `build.sh` | Validate, then increment version and CHANGELOG | Yes, after success |
| `tools/inspect_controller_evidence.py` | Normalize a saved JSON capture only | No |

## Adding a new script

Add the standard header, explain the safe scope, number console steps and use `pause` in `.bat`. Place parsing logic under test, compile `tools/` in the build check and document every command. No script may open a controller, emit G-code or change HAL state as a side effect.

## Hardware acceptance gate

Identify the controller and interface, bind evidence to that controller, validate E-STOP/door signals independently, test stale/offline behavior, conduct dry auxiliary-cell trials and complete HIL before any command path exists. The native CNC controller remains authoritative.
