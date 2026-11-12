# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - the run loop of the service and the GPIO reader of the safety lines
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The exit codes of the service (a bridge that cannot be built, a loop that ends, closers that fail) and the real GPIO path against a fake gpiod:
the bias makes a cut wire read as unsafe, and a missing library stops the start with a clear message."""

import contextlib
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from hydra_umc_bridge_cnc import service
from hydra_umc_bridge_cnc.service import main, open_gpio_level

EXAMPLE = Path(__file__).resolve().parents[1] / "deploy" / "bridge-cnc.example.toml"


class FakeGpiod:
    def __init__(self, value):
        self.value, self.requests = value, []

    def modules(self):
        gpiod = types.ModuleType("gpiod")
        line = types.ModuleType("gpiod.line")
        line.Bias = types.SimpleNamespace(PULL_UP="up", PULL_DOWN="down")
        line.Direction = types.SimpleNamespace(INPUT="input")
        line.Value = types.SimpleNamespace(ACTIVE="active", INACTIVE="inactive")
        gpiod.line = line
        gpiod.LineSettings = lambda **settings: types.SimpleNamespace(**settings)

        def request_lines(chip, consumer, config):
            self.requests.append((chip, consumer, config))
            return types.SimpleNamespace(get_value=lambda number: self.value)

        gpiod.request_lines = request_lines
        return {"gpiod": gpiod, "gpiod.line": line}


class GpioLevelTests(unittest.TestCase):
    def test_an_unsafe_high_line_uses_a_pull_up_and_reads_unsafe_when_active(self):
        fake = FakeGpiod("active")
        with mock.patch.dict(sys.modules, fake.modules()):
            read = open_gpio_level("/dev/gpiochip0", 5, "high", 20.0)
        chip, consumer, config = fake.requests[0]
        self.assertEqual((chip, consumer), ("/dev/gpiochip0", "hydra-umc-bridge-cnc"))
        self.assertEqual((config[5].direction, config[5].bias), ("input", "up"))
        self.assertEqual(config[5].debounce_period.total_seconds(), 0.02)
        self.assertTrue(read())
        fake.value = "inactive"
        self.assertFalse(read())

    def test_an_unsafe_low_line_uses_a_pull_down_and_reads_unsafe_when_inactive(self):
        fake = FakeGpiod("inactive")
        with mock.patch.dict(sys.modules, fake.modules()):
            read = open_gpio_level("chip", 9, "low", 0.0)
        self.assertEqual(fake.requests[0][2][9].bias, "down")
        self.assertTrue(read())
        fake.value = "active"
        self.assertFalse(read())

    def test_a_missing_library_stops_the_start_with_a_clear_message(self):
        with mock.patch.dict(sys.modules, {"gpiod": None, "gpiod.line": None}):
            with self.assertRaises(RuntimeError) as caught:
                open_gpio_level("chip", 1, "high", 10.0)
        self.assertIn("gpiod is not installed", str(caught.exception))


class RunLoopTests(unittest.TestCase):
    def _main(self, build, run, *extra):
        out, err = io.StringIO(), io.StringIO()
        environment = {"HYDRA_UMC_CNC_HARDENED": ""}
        with mock.patch.dict("os.environ", environment), mock.patch.object(service, "build_bridge", build), mock.patch(
            "hydra_umc_bridge_cnc.mqtt_transport.run_forever", run
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", str(EXAMPLE), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_a_bridge_that_cannot_be_built_is_exit_3_and_names_the_cause(self):
        def build(config):
            raise RuntimeError("gpiod is not installed - install it")

        code, _, err = self._main(build, lambda *a, **k: self.fail("the loop must not start"))
        self.assertEqual(code, 3)
        self.assertIn("gpiod is not installed", err)

    def test_a_port_that_cannot_be_reached_is_exit_3_too(self):
        def build(config):
            raise OSError("no such port")

        self.assertEqual(self._main(build, lambda *a, **k: None)[0], 3)

    def test_a_loop_that_ends_is_exit_1_so_the_service_manager_restarts_it(self):
        closed = []
        code, _, _ = self._main(lambda config: (object(), [lambda: closed.append("a"), lambda: closed.append("b")]), lambda *a, **k: None)
        self.assertEqual(code, 1)
        self.assertEqual(closed, ["a", "b"])

    def test_a_loop_that_raises_is_exit_1_and_the_closers_still_run(self):
        closed = []

        def run(*args, **kwargs):
            raise OSError("broker gone")

        code, _, _ = self._main(lambda config: (object(), [lambda: closed.append("serial")]), run)
        self.assertEqual(code, 1)
        self.assertEqual(closed, ["serial"])

    def test_a_closer_that_fails_does_not_stop_the_others(self):
        closed = []

        def broken():
            raise OSError("already closed")

        code, _, _ = self._main(lambda config: (object(), [broken, lambda: closed.append("second")]), lambda *a, **k: None)
        self.assertEqual(code, 1)
        self.assertEqual(closed, ["second"])

    def test_the_loop_gets_the_broker_settings_of_the_file(self):
        seen = {}

        def run(bridge, host, port, client_id, **options):
            seen.update(host=host, port=port, client_id=client_id, **options)

        self._main(lambda config: (object(), []), run)
        self.assertTrue(seen["host"])
        self.assertIsInstance(seen["port"], int)
        self.assertIn("max_connect_attempts", seen)
        self.assertIn("hardening", seen)

    def test_a_file_that_is_not_there_is_exit_2(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stderr(err):
            code = main(["--config", str(Path(folder) / "missing.toml")])
        self.assertEqual(code, 2)
        self.assertIn("configuration error", err.getvalue())


if __name__ == "__main__":
    unittest.main()
