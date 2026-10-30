# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Service configuration and wiring tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The configuration file, the signal and cell-state sources and the wiring of the service, against the GRBL emulator."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState, job_to_dict
from hydra_umc_bridge_cnc.mqtt_transport import TOPIC_PREFIX
from hydra_umc_bridge_cnc.service import (
    CellStateConfig,
    ConfigError,
    SignalConfig,
    build_bridge,
    cell_state_reader,
    load_config,
    main,
    parse_config,
    unsafe_reader,
)

from grbl_emulator import GrblEmulator

EXAMPLE = Path(__file__).resolve().parents[1] / "deploy" / "bridge-cnc.example.toml"
MINIMAL = {"controller": {"device": "/dev/ttyUSB0"}}


def with_(**changes):
    data = json.loads(json.dumps(MINIMAL))
    data.update(changes)
    return data


class ParseTests(unittest.TestCase):
    def test_defaults_are_the_conservative_ones(self):
        config = parse_config(MINIMAL)
        self.assertEqual(config.estop.source, "none")
        self.assertEqual(config.door.source, "none")
        self.assertEqual(config.cell_state.value, CellState.INHIBITED)
        self.assertFalse(config.port_sharing.enabled)
        self.assertEqual(config.spindle_gate, "when_known")
        self.assertFalse(config.mtconnect)

    def test_the_example_file_is_valid(self):
        config = load_config(EXAMPLE)
        self.assertEqual(config.estop.line, 23)
        self.assertEqual(config.door.unsafe_level, "high")
        self.assertEqual(config.cell_state.source, "file")

    def test_a_signal_with_a_source_must_declare_its_wiring(self):
        with self.assertRaisesRegex(ConfigError, "estop.unsafe_level must be high or low"):
            parse_config(with_(estop={"source": "gpio", "chip": "/dev/gpiochip0", "line": 23}))
        with self.assertRaisesRegex(ConfigError, "needs chip and line"):
            parse_config(with_(estop={"source": "gpio", "unsafe_level": "high"}))
        with self.assertRaisesRegex(ConfigError, "needs path"):
            parse_config(with_(door={"source": "file", "unsafe_level": "low"}))
        with self.assertRaisesRegex(ConfigError, "none, gpio or file"):
            parse_config(with_(door={"source": "magic"}))

    def test_unknown_keys_and_wrong_values_are_refused(self):
        with self.assertRaisesRegex(ConfigError, "unknown key in the file: estp"):
            parse_config(with_(estp={}))
        with self.assertRaisesRegex(ConfigError, "controller.kind"):
            parse_config({"controller": {"kind": "mach3"}})
        with self.assertRaisesRegex(ConfigError, "controller.device is required"):
            parse_config({})
        with self.assertRaisesRegex(ConfigError, "controller.url is required"):
            parse_config({"controller": {"kind": "cncjs", "device": "/dev/ttyUSB0"}})
        with self.assertRaisesRegex(ConfigError, "gate.spindle"):
            parse_config(with_(gate={"spindle": "maybe"}))
        with self.assertRaisesRegex(ConfigError, "must be a int"):
            parse_config(with_(mqtt={"port": "1883"}))

    def test_port_sharing_rules(self):
        with self.assertRaisesRegex(ConfigError, "not the loopback"):
            parse_config(with_(port_sharing={"enabled": True, "host": "0.0.0.0"}))
        parse_config(with_(port_sharing={"enabled": True, "host": "0.0.0.0", "allow_remote": True}))
        with self.assertRaisesRegex(ConfigError, "needs a grbl controller"):
            parse_config({"controller": {"kind": "linuxcnc"}, "port_sharing": {"enabled": True}})
        parse_config({"controller": {"kind": "linuxcnc"}})

    def test_bad_toml_and_missing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / "bad.toml"
            bad.write_text("[estop\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "not valid TOML"):
                load_config(bad)
            self.assertEqual(main(["--config", str(Path(folder) / "none.toml"), "--check"]), 2)
        self.assertEqual(main(["--config", str(EXAMPLE), "--check"]), 0)


class SignalTests(unittest.TestCase):
    def test_no_source_is_unsafe(self):
        self.assertTrue(unsafe_reader(SignalConfig())())

    def test_file_signal_levels_and_staleness(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "door"
            read = unsafe_reader(SignalConfig("file", path=str(path), unsafe_level="low", max_age_seconds=30))
            self.assertTrue(read())  # missing
            path.write_text("1", encoding="utf-8")
            self.assertFalse(read())  # 1 is safe when the unsafe level is 0
            path.write_text("0", encoding="utf-8")
            self.assertTrue(read())
            path.write_text("maybe", encoding="utf-8")
            self.assertTrue(read())
            path.write_text("1", encoding="utf-8")
            old = time.time() - 120
            os.utime(path, (old, old))
            self.assertTrue(read())  # a stale "safe" is not safe

    def test_gpio_signal_and_a_failing_read(self):
        values = {"level": False}
        seen = {}

        def open_gpio(chip, line, unsafe_level, debounce_ms):
            seen.update(chip=chip, line=line, unsafe=unsafe_level, debounce=debounce_ms)

            def read():
                if values["level"] is None:
                    raise OSError("chip gone")
                return values["level"]

            return read

        read = unsafe_reader(SignalConfig("gpio", chip="/dev/gpiochip0", line=23, unsafe_level="high", debounce_ms=5.0), open_gpio=open_gpio)
        self.assertEqual(seen, {"chip": "/dev/gpiochip0", "line": 23, "unsafe": "high", "debounce": 5.0})
        self.assertFalse(read())
        values["level"] = True
        self.assertTrue(read())
        values["level"] = None
        self.assertTrue(read())  # unreadable is unsafe


class CellStateTests(unittest.TestCase):
    def test_file_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cell_state"
            read = cell_state_reader(CellStateConfig("file", path=str(path)))
            self.assertEqual(read(), CellState.SAFE_STOP)
            path.write_text("READY", encoding="utf-8")
            self.assertEqual(read(), CellState.READY)
            path.write_text("nonsense", encoding="utf-8")
            self.assertEqual(read(), CellState.SAFE_STOP)
            path.write_text("READY", encoding="utf-8")
            old = time.time() - 120
            os.utime(path, (old, old))
            self.assertEqual(read(), CellState.SAFE_STOP)
        self.assertEqual(cell_state_reader(CellStateConfig())(), CellState.INHIBITED)


class WiringTests(unittest.TestCase):
    def build(self, data, **kwargs):
        emulator = GrblEmulator()
        opened = {}

        def open_serial(device, baud, timeout):
            opened.update(device=device, baud=baud, timeout=timeout)
            return emulator

        bridge, closers = build_bridge(parse_config(data), open_serial=open_serial, **kwargs)
        return bridge, emulator, opened, closers

    def ask(self, bridge, suffix, payload=b""):
        return [json.loads(item.payload) for item in bridge.handle_message(f"{TOPIC_PREFIX}{suffix}", payload)]

    def test_serial_settings_reach_the_opener_and_state_is_published(self):
        with tempfile.TemporaryDirectory() as folder:
            estop, door = Path(folder) / "estop", Path(folder) / "door"
            data = with_(
                controller={"device": "/dev/ttyUSB0", "baud": 57600},
                estop={"source": "file", "path": str(estop), "unsafe_level": "high"},
                door={"source": "file", "path": str(door), "unsafe_level": "low"},
            )
            bridge, emulator, opened, _ = self.build(data)
            estop.write_text("0", encoding="utf-8")
            door.write_text("1", encoding="utf-8")
            state = self.ask(bridge, "cmd/status")[0]
            self.assertEqual(opened["baud"], 57600)
            self.assertFalse(state["estop"])
            self.assertTrue(state["door_closed"])
            estop.write_text("1", encoding="utf-8")
            self.assertTrue(self.ask(bridge, "cmd/status")[0]["estop"])
            estop.write_text("0", encoding="utf-8")
            door.write_text("0", encoding="utf-8")
            self.assertFalse(self.ask(bridge, "cmd/status")[0]["door_closed"])

    def test_without_signal_sources_the_machine_is_not_safe(self):
        bridge, _, _, _ = self.build(MINIMAL)
        state = self.ask(bridge, "cmd/status")[0]
        self.assertTrue(state["estop"])
        self.assertFalse(state["door_closed"])

    def test_cell_state_hook_reaches_the_job_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            cell, estop, door = Path(folder) / "cell", Path(folder) / "estop", Path(folder) / "door"
            estop.write_text("0", encoding="utf-8")
            door.write_text("1", encoding="utf-8")
            data = with_(
                estop={"source": "file", "path": str(estop), "unsafe_level": "high"},
                door={"source": "file", "path": str(door), "unsafe_level": "low"},
                cell_state={"source": "file", "path": str(cell)},
            )
            bridge, _, _, _ = self.build(data)

            def job(number):
                return json.dumps(job_to_dict(BridgeJob(f"j{number}", f"k{number}", "orchestrator", JobPhase.LOAD, MachineState.IDLE, {}))).encode()

            cell.write_text("SAFE_STOP", encoding="utf-8")
            self.assertFalse(self.ask(bridge, "cmd/job", job(1))[0]["allowed"])
            cell.write_text("READY", encoding="utf-8")
            self.assertTrue(self.ask(bridge, "cmd/job", job(2))[0]["allowed"])

    def test_port_sharing_starts_a_multiplexer_that_is_returned_to_close(self):
        bridge, _, _, closers = self.build(with_(port_sharing={"enabled": True, "port": 0}))
        try:
            self.assertEqual(len(closers), 1)
            self.assertIn("estop", self.ask(bridge, "cmd/status")[0])
        finally:
            for close in closers:
                close()

    def test_linuxcnc_and_cncjs_use_their_own_openers(self):
        from hydra_umc_bridge_cnc import SimulatedCncjs, SimulatedLinuxcnc

        simulation = SimulatedLinuxcnc()
        bridge, closers = build_bridge(parse_config({"controller": {"kind": "linuxcnc"}}), open_linuxcnc=lambda: simulation.controller())
        self.assertEqual(closers, [])
        self.assertIn("estop", self.ask(bridge, "cmd/status")[0])
        server = SimulatedCncjs()
        seen = {}

        def open_cncjs(url, port, token=None):
            from hydra_umc_bridge_cnc import CncjsController

            seen.update(url=url, port=port, token=token)
            return CncjsController(server, port)

        os.environ["HYDRA_UMC_CNC_CNCJS_TOKEN"] = "secret-token"
        try:
            bridge, _ = build_bridge(parse_config({"controller": {"kind": "cncjs", "device": "/dev/ttyUSB0", "url": "http://127.0.0.1:8000"}}), open_cncjs=open_cncjs)
        finally:
            del os.environ["HYDRA_UMC_CNC_CNCJS_TOKEN"]
        self.assertEqual(seen, {"url": "http://127.0.0.1:8000", "port": "/dev/ttyUSB0", "token": "secret-token"})
        self.assertIn("estop", self.ask(bridge, "cmd/status")[0])


if __name__ == "__main__":
    unittest.main()
