<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - Technical bridge guide
Copyright (C) JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-CNC Technical Guide

## Scope and operating model

`CncSnapshot` makes a CNC state usable by the shared SDK gate only after independent E-STOP and door evidence are present. `observation.py` normalizes an already-saved mapping or supplied GRBL status line. Missing or non-Boolean E-STOP/door values fail closed. The bridge coordinates auxiliary loading/unloading; it does not control trajectory, spindle, axes or G-code.

`CncSnapshot.machine_state()` recognizes GRBL v1.1's real full status vocabulary (github.com/gnea/grbl/wiki/Grbl-v1.1-Interface), including the real numeric substate GRBL always appends to `Hold`/`Door` (`Hold:0`/`Hold:1`, `Door:0`..`Door:3` - stripped before matching): `Idle` -> `IDLE`; `Run`/`Jog`/`Home` -> `RUNNING` (all three are real active-motion states); `Hold` -> `HOLDING` (a paused program, distinct from actively running); `Alarm`/`Fault`/`Error`/`Off` -> `FAULT`; `Door` -> `SAFE_STOP` (a defensive second signal alongside the bridge's own independent `door_closed` input). Any other token, including GRBL's `Check`/`Sleep`, stays `OFFLINE` - the same conservative fail-safe default used for every unrecognized signal in this bridge.

`serial_transport.py` is this bridge's first real transport: `GrblSerialProbe.query_status()` works on a serial connection that is already open (`open_serial_port()` / `open_serial_port_by_id()` open it) and sends GRBL's real-time status query byte (`?`); `GrblRealtimeControl` sends only GRBL's own real-time single-byte control characters - `feed_hold()`/`soft_reset()` (always allowed) and `cycle_start_resume()` (gated on a genuinely `HOLDING` machine). It never streams a G-code program; the controller (GRBL itself) keeps all real-time trajectory, limits, spindle and safety authority. GRBL talks without being asked - the banner after every reset (opening a port resets an Arduino unless DTR is left low, which `open_serial_port()` does), `ok`/`error:N`, `[MSG:...]`, `ALARM:n` - so a status query discards what is already waiting, sends `?` and reads until a line that is a status report (`<...>`) arrives, keeping the newest one when several are waiting; no report in time is `OFFLINE`. A failure to open or reopen the port (`ReconnectingSerialConnection`) comes out as an `OSError`, and the query and the real-time commands fail closed on any failure. The GRBL-facing logic is written against a small `SerialLike` protocol so it is unit-testable with an in-memory fake connection - `pyserial` (optional `[serial]` extra) is only imported, lazily, inside `open_serial_port()`.

`mqtt_transport.py` reaches the exact same real serial commands over `HYDRA-UMC-MQTT-BROKER`, this bridge's second real transport. `CncMqttBridge.handle_message()` routes `hydra/bridges/cnc/cmd/{status,feed_hold,soft_reset,cycle_start_resume,job}` to the same `GrblRealtimeControl`/`CncCellBridge` calls above, publishing `hydra/bridges/cnc/state` (retained) and one `.../cmd/<verb>/result` per command - no new physical authority, only a new way to reach what already existed. `job` accepts a `BridgeJob` in the shared SDK's `job_to_dict()` wire shape and answers with a `GateDecision`, marked `"mode": "gate-only"`: it never acts, so an `ABORT` through it is answered with `"forwarded": false` and the reason says that nothing was stopped (use `cmd/feed_hold`). `cmd/feed_hold` asks the controller for its status right after the byte is written and reports `controller_state_after` and `confirmed` (the controller reports a hold): "a byte was written" is not "the machine stopped". Idempotency keys are namespaced (`job:<key>`). Every message is handled without ever raising: a payload over 16 KiB, deeply nested or unreadable JSON and any unexpected failure get a refusal on the result topic. In `run_forever()` the messages are handled off the network thread, one at a time and in order, except `feed_hold` and `soft_reset`, which have a lane of their own so that a status query waiting on the serial port never delays them; one status query at a time uses the port. `state` carries `observed_at` (epoch milliseconds), and the bridge publishes `hydra/bridges/cnc/availability` (`online` when connected; the broker publishes `offline`, the MQTT Last Will, when the connection is lost). The client uses paho-mqtt's version 2 callback API (`paho-mqtt>=2.1,<3`). After a door event GRBL sits in `Door:0` ("ready to resume") and the bridge keeps reporting `SAFE_STOP` for every `Door` state, so `cycle_start_resume` is refused: resuming after a door event is left to the operator at the sender. Dispatch is pure enough to unit-test with the same in-memory fake connection, no real broker required - `paho-mqtt` (optional `[mqtt]` extra) is only imported, lazily, inside `run_forever()`.

## One serial port

A GRBL controller has ONE serial port. While this bridge holds it open, no other sender (UGS, bCNC, CNCjs, LaserGRBL) can use it, and this bridge does not send programs. That means the machine can run a job only if something that shares the port - a multiplexer such as CNCjs or ser2net - sits between the bridge, the sender and the controller, or if the programs are started some other way while the bridge only observes. That architecture is not decided, so the bridge is not yet deployable as it stands on a machine that is also driven by a G-code sender.

## Compatible software

The current parser accepts generic controller-state evidence, the leading state token of a supplied GRBL status line, and saved MTConnect `execution` values (`READY`/`STOPPED`, `ACTIVE`/`EXECUTING`, `FEED_HOLD`/`INTERRUPTED`, `FAULT`). MTConnect is read-only evidence only: it neither opens a controller connection nor offers a command path. LinuxCNC/HAL is an intended future controller integration, but nothing in this bridge talks to it today: the only real transport is GRBL over a serial port. Other controllers can be supported only through a documented read-only adapter that preserves independent guard and E-STOP signals.

## Scripts and verification

| Script | Purpose | Changes version/CHANGELOG? |
|---|---|---|
| `build-test.bat` / `build-test.sh` | Compile and run local safety tests | No |
| `build.bat` / `build.sh` | Validate, then build (never changes the version or the CHANGELOG) | No |
| `tools/inspect_controller_evidence.py` | Normalize a saved JSON capture only | No |

## Adding a new script

Add the standard header, explain the safe scope, number console steps and use `pause` in `.bat`. Place parsing logic under test, compile `tools/` in the build check and document every command. No script may emit G-code or change HAL state as a side effect; the serial link is opened only by the bridge's own transport, never by a helper script.

## Hardware acceptance gate

Identify the controller and interface, bind evidence to that controller, validate E-STOP/door signals independently, test stale/offline behavior, conduct dry auxiliary-cell trials and complete HIL before relying on the command path (feed hold, soft reset, cycle start/resume) that already exists. The native CNC controller remains authoritative.
