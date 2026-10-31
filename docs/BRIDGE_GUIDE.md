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

`mqtt_transport.py` reaches the exact same real serial commands over `HYDRA-UMC-MQTT-BROKER`, this bridge's second real transport. `CncMqttBridge.handle_message()` routes `hydra/bridges/cnc/cmd/{status,feed_hold,soft_reset,cycle_start_resume,job}` to the same `GrblRealtimeControl`/`CncCellBridge` calls above, publishing `hydra/bridges/cnc/state` (retained) and one `.../cmd/<verb>/result` per command, published at QoS 1 (the state topics stay at QoS 0) - no new physical authority, only a new way to reach what already existed. `job` accepts a `BridgeJob` in the shared SDK's `job_to_dict()` wire shape and answers with a `GateDecision`, marked `"mode": "gate-only"`: it never acts, so an `ABORT` through it is answered with `"forwarded": false` and the reason says that nothing was stopped (use `cmd/feed_hold`). `cmd/feed_hold` asks the controller for its status right after the byte is written and reports `controller_state_after` and `confirmed` (the controller reports a hold): "a byte was written" is not "the machine stopped". A job goes through the shared SDK intake (`JobIntake`): a malformed one is refused, and a repeated `idempotency_key` is answered with the stored answer instead of being evaluated again. Every message is handled without ever raising: a payload over 16 KiB, deeply nested or unreadable JSON and any unexpected failure get a refusal on the result topic. In `run_forever()` the messages are handled off the network thread, one at a time and in order, except `feed_hold` and `soft_reset`, which have a lane of their own so that a status query waiting on the serial port never delays them; one status query at a time uses the port. `state` carries `observed_at` (epoch milliseconds), and the bridge publishes `hydra/bridges/cnc/availability` (`online` when connected; the broker publishes `offline`, the MQTT Last Will, when the connection is lost). The client uses paho-mqtt's version 2 callback API (`paho-mqtt>=2.1,<3`). After a door event GRBL sits in `Door:0` ("ready to resume") and the bridge keeps reporting `SAFE_STOP` for every `Door` state, so `cycle_start_resume` is refused: resuming after a door event is left to the operator at the sender. Dispatch is pure enough to unit-test with the same in-memory fake connection, no real broker required - `paho-mqtt` (optional `[mqtt]` extra) is only imported, lazily, inside `run_forever()`.

## Controller links

`CncMqttBridge` talks to its machine through `ControllerLink` (`controller.py`): `read_snapshot` and the three requests `feed_hold`, `soft_reset` and `cycle_start_resume`. Three links exist and all of them keep the rule of the bridge (nothing is ever streamed; a request that did not happen is reported as such; a resume needs a READY cell and a machine that is really holding):

- `GrblController` - GRBL on a serial port the bridge holds open (the default; `CncMqttBridge(connection, ...)`).
- `LinuxcncController` (`linuxcnc_link.py`) - a LinuxCNC installation on the same host. It reads `linuxcnc.stat` (task state, interpreter state, emergency stop, spindle, velocity, position) and sends only `auto(AUTO_PAUSE)` (feed hold), `abort()` (reset) and `auto(AUTO_RESUME)` (resume) through `linuxcnc.command`; it never opens or runs a program, never changes the task state and never sets a spindle. LinuxCNC has no door signal: `door_closed` comes from the cell (a HAL pin the integrator exposes), as for GRBL, and the emergency stop is the OR of LinuxCNC's own and the cell's. `open_linuxcnc()` imports the `linuxcnc` module lazily; `SimulatedLinuxcnc` is the in-memory machine the tests run against.
- `CncjsController` (`cncjs_link.py`) - a CNCjs server that owns the serial port. The bridge connects to the CNCjs socket.io API, listens to the `controller:state` it already broadcasts and sends only the commands `feedhold`, `reset` and `cyclestart` on the open port; it never sends `gcode`, `unlock`, `homing` or `sleep`. CNCjs stays the sender that runs the programs, so the bridge no longer takes the single serial port away from it. A reading older than `max_age_seconds` (default 3) is not trusted: an old or missing state is never an idle machine and the door counts as open, like a failed serial query. `open_cncjs()` imports a socket.io client lazily (install one compatible with your CNCjs); `SimulatedCncjs` is the in-memory server the tests run against.

Pass `controller=` to `CncMqttBridge` to use one of the last two instead of a serial connection.

### Sharing the serial port (`multiplexer.py`)

A GRBL board has one serial port. With `HYDRA_UMC_CNC_PORT_SHARING=multiplexer` the bridge can keep the port and still leave the machine to a G-code sender: `share_serial_port(port)` returns the connection the bridge should use (`GrblController(connection)`) and a `SerialMultiplexer` that owns the real port and serves **one** sender at a time over TCP (`HYDRA_UMC_CNC_PORT_SHARING_LISTEN`, default `127.0.0.1:5700`; point UGS, bCNC, CNCjs or LaserGRBL at that address as a raw TCP / `socket://` connection instead of the serial device).

- What the controller writes goes to the sender as it comes, and every complete line is also kept for the bridge's status queries.
- What the sender writes goes to the controller as it comes: the multiplexer does not rewrite, delay or inspect a program. The optional `sender_allowed` gate holds a line of the sender while it answers no (the sender gets `error:9`); real-time bytes of the sender are never held.
- The bridge writes only GRBL's real-time bytes (`?`, `!`, `~`, Ctrl-X); anything else it tries to write raises an error, so it cannot stream a program through the multiplexer either. A real-time byte goes out between two bytes of the sender.
- The default is conservative: sharing is **off** (the bridge holds the port alone, as before), the front listens on the loopback address only (any other address needs `HYDRA_UMC_CNC_PORT_SHARING_ALLOW_REMOTE=1`), and a second sender is refused while one is connected. A word the configuration does not know means off.
- A status report that the bridge asked for also reaches the sender, which is harmless to the senders that poll the status themselves. A soft reset sent by the bridge resets the controller under the sender, exactly as it would at the machine.

It is tested against the GRBL emulator of the test suite and a real TCP socket; it has not run against a real GRBL board. Both links have only been exercised against their simulations.

## The spindle gate

For the phases in which an arm reaches into the machine (`LOAD`, `UNLOAD`) an idle controller is not enough: the snapshot also carries the spindle speed and the feed rate the controller reported (GRBL `FS:`, LinuxCNC spindle and velocity, CNCjs `spindle` and `feedrate`). `HYDRA_UMC_CNC_SPINDLE_GATE` (or `spindle_gate=` of `CncMqttBridge` / `CncCellBridge`) selects the mode: `when_known` (the default) refuses when a turning spindle or a moving feed is reported and says nothing about what the controller does not report, `required` also refuses when it does not report the spindle speed, and `off` is the old behaviour. `ABORT` is always forwarded.

## MTConnect view

`mtconnect_publish.py` states what the CNC is doing in the words of MTConnect: `avail`, `execution` (`READY`, `ACTIVE`, `FEED_HOLD`, `STOPPED`, `INTERRUPTED`; `STOPPED` is a machine that waits for its operator to reset it, so it is neither `READY` nor a pause that can be resumed), `estop`, `door`, the samples `spindle_speed` and `path_feedrate`, and a `system` condition (a `FAULT` with the native code `ESTOP`, `DOOR_OPEN` or the controller state). A controller that is not reporting is `UNAVAILABLE`, never `READY`, and a value the controller did not report is `UNAVAILABLE`, never zero. `ShdrAdapter` serves the observations as SHDR lines over TCP (127.0.0.1 unless told otherwise, at most 16 clients, read only: the only line it honours is `* PING`), which is what an off-the-shelf MTConnect agent reads. With `HYDRA_UMC_CNC_MTCONNECT=1` the bridge also publishes the same observations, retained, on `hydra/bridges/cnc/mtconnect` after each status refresh.

## Matching answers to requests

A command whose payload is a JSON object may carry a `request_id` (a plain token of up to 64 characters: letters, digits, `.`, `_`, `:`, `-`). Every result the bridge publishes for that message, on `cmd/<verb>/result`, carries the same `request_id`, so a caller with several commands in flight can tell the answers apart. A repeated idempotency key still gets the answer that was stored for it, with the `request_id` of the new request. The state topics do not carry one.

## One serial port

A GRBL controller has ONE serial port. While this bridge holds it open, no other sender (UGS, bCNC, CNCjs, LaserGRBL) can use it, and this bridge does not send programs. That means the machine can run a job only if something that shares the port - a multiplexer such as CNCjs or ser2net - sits between the bridge, the sender and the controller, or if the programs are started some other way while the bridge only observes. With the CNCjs link above, CNCjs keeps the port and the bridge observes and pauses beside it; without it, the bridge is not deployable as it stands on a machine that is also driven by a G-code sender.

## Compatible software

The current parser accepts generic controller-state evidence, the leading state token of a supplied GRBL status line, and saved MTConnect `execution` values (`READY`, `STOPPED` (its own `STOPPED` state: no work starts on it and it is not resumed), `ACTIVE`/`EXECUTING`, `FEED_HOLD`/`INTERRUPTED`, `FAULT`). MTConnect is read-only evidence only: it neither opens a controller connection nor offers a command path. LinuxCNC (through its Python module) and CNCjs have read-and-pause links (see Controller links); HAL pins are not used. Other controllers can be supported only through a documented adapter that preserves independent guard and E-STOP signals. Other controllers can be supported only through a documented read-only adapter that preserves independent guard and E-STOP signals.

## Scripts and verification

| Script | Purpose | Changes version/CHANGELOG? |
|---|---|---|
| `build-test.bat` / `build-test.sh` | Compile and run local safety tests | No |
| `build.bat` / `build.sh` | Validate, then build (never changes the version or the CHANGELOG) | No |
| `tools/inspect_controller_evidence.py` | Normalize a saved JSON capture only | No |

## Adding a new script

Add the standard header, explain the safe scope, number console steps and use `pause` in `.bat`. Place parsing logic under test, compile `tools/` in the build check and document every command. No script may emit G-code or change HAL state as a side effect; the serial link is opened only by the bridge's own transport, never by a helper script.

## Following the global stop

The server can announce a global stop on `<mqtt prefix>/safety/estop` (a retained `SafetyState` message: `SAFE_STOP`, later `READY`; the contract is `docs/GLOBAL_STOP.md` of HYDRA-UMC-SERVER). This bridge can follow it. It is off by default and then behaves exactly as before: it does not even subscribe to the topic.

| Switch | Default | Meaning |
|---|---|---|
| `HYDRA_UMC_CNC_GLOBAL_STOP` | off | `1` makes the bridge subscribe to the announcement (QoS 1, again after every reconnect) |
| `HYDRA_UMC_CNC_GLOBAL_STOP_TOPIC` | `hydra/swarm/safety/estop` | the topic, when the MQTT prefix of the server is not the default one |
| `HYDRA_UMC_CNC_GLOBAL_STOP_STRICT` | off | `1` also refuses before the first announcement has been heard |

In the configuration file of the service the same three switches are the table `[global_stop]` (`enabled`, `topic`, `strict`; the example file shows it switched off).

While the last announcement says `SAFE_STOP`, says anything but `READY`, or cannot be read, the command handler of the bridge refuses `cmd/cycle_start_resume` and `cmd/job` (an abort job passes) with an answer that gives the reason (`global stop active (SAFE_STOP) - nothing moves until the server announces READY`); `cmd/feed_hold`, `cmd/soft_reset` and `cmd/status` keep working. Only a `READY` message ends it. The announcement is read although it is retained: it is the state, not a replayed command. When the broker is lost the bridge keeps the last state it heard; a bridge that has not heard the topic yet is left as it was, unless `HYDRA_UMC_CNC_GLOBAL_STOP_STRICT` is on. This is a refusal in software: the controller and the machine keep the last word, and the contract adds a stop, it never removes one. Tested against a real broker (`tests/test_global_stop_broker.py`); never run with the real server.

## Hardware acceptance gate

Identify the controller and interface, bind evidence to that controller, validate E-STOP/door signals independently, test stale/offline behavior, conduct dry auxiliary-cell trials and complete HIL before relying on the command path (feed hold, soft reset, cycle start/resume) that already exists. The native CNC controller remains authoritative.

## Running as a service

`python -m hydra_umc_bridge_cnc --config /etc/hydra-umc/bridge-cnc.toml` (the path can also come from `HYDRA_UMC_BRIDGE_CNC_CONFIG`) starts the bridge. Nothing that depends on the machine is in the code: the file `deploy/bridge-cnc.example.toml` shows every key.

- `[controller]`: `kind` is `grbl` (a board on a serial port: `device`, `baud`, `timeout_seconds`), `cncjs` (`url` and the port CNCjs has open as `device`; the token comes from `HYDRA_UMC_CNC_CNCJS_TOKEN`) or `linuxcnc`.
- `[port_sharing]`: the multiplexer for a G-code sender, off unless `enabled`; it listens on the loopback address unless `allow_remote` is set.
- `[estop]` and `[door]`: `source` is `gpio` (`chip`, `line`, `debounce_ms`) or `file` (`path`, `max_age_seconds`), with `unsafe_level` (`high` or `low`) declaring what the signal reads when the cell is NOT safe. The bias of a GPIO line makes a cut wire read as unsafe. A signal with no source is E-STOP pressed or door open.
- `[cell_state]`: `fixed` (the default, `INHIBITED`) or `file`; a file that is missing, unknown or stale is `SAFE_STOP`.
- `[gate]` (`spindle`: `off`, `when_known` or `required`), `[mtconnect]`, `[mqtt]` and `[idempotency]`. The MQTT user, password and TLS come from `HYDRA_MQTT_*` in the environment of the service, not from the file.

A key the service does not know is refused and `--check` validates the file without starting anything. `deploy/hydra-umc-bridge-cnc.service` is the systemd unit: it checks the file before starting, restarts on failure and allows only the GPIO chip and the serial devices. The service has run against the GRBL emulator and simulated signals; it has not run on a real machine.
