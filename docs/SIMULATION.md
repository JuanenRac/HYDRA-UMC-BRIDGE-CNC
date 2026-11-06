# Simulation bench

The CNC bridge runs against a GRBL emulator instead of a machine: status reports, the banner after a reset, `ok` and `error:N` lines, holds, doors and alarms, with fault injection (silent controller, short writes, corrupt lines).

The simulation bench replaces the hardware with a protocol-faithful GRBL v1.1 controller (`tests/grbl_emulator.py`) behind the serial seam, a pseudo-terminal serial pair, the simulated LinuxCNC and CNCjs links and the MQTT dispatch. It proves the parsing, the gating, the transports and the failure paths of the bridge; it says nothing about
a real machine, real timing or real safety functions.

## Run it

```
python -m unittest -v <modules>
```

from the `tests` folder, with `src` of this repository and `clients/python/src` of the SDK on `PYTHONPATH` (the same layout `python tools/build_test.py` uses). The
workflow named "Simulation bench" does exactly this on a clean Linux runner on every push and pull request, by hand and once a week, with nothing but Python.

The example settings in `deploy/` show the configuration the service reads; the emulator does not need it.

## What it runs

| File | What it exercises |
|------|-------------------|
| `tests/test_grbl_emulator.py` | the emulator and the bridge against it: status vocabulary, real-time bytes and injected faults |
| `tests/test_serial_pty.py` | the real serial code over a pseudo-terminal pair (needs `pyserial`) |
| `tests/test_serial_transport.py` | status query and real-time control bytes over the serial seam; fail-closed on every failure |
| `tests/test_controllers.py` | the GRBL, LinuxCNC and CNCjs links against their in-memory simulations |
| `tests/test_cell.py` | the cell with simulated E-STOP and door evidence |
| `tests/test_mqtt_transport.py` | the topic dispatch, QoS, idempotency and limits without a broker |
| `tests/test_broker_integration.py` | the same dispatch against a real MQTT broker; skipped unless the broker checkout is present |
| `tests/test_global_stop_broker.py` | the global stop over a real broker; skipped unless the broker checkout is present |

The broker scenarios start a real MQTT broker from a checkout of HYDRA-UMC-MQTT-BROKER (`HYDRA_UMC_TEST_BROKER_DIR`); the simulation job does not install it, so
those cases skip themselves there and run in the normal test run on a machine that has the broker.

## What it cannot show

A real GRBL board and its USB serial adapter, real timing and electrical noise on the line, a real E-STOP or door switch, LinuxCNC and CNCjs installations. The hardware row of the status table therefore stays at "simulated" until a person has run the bridge on a real setup.
