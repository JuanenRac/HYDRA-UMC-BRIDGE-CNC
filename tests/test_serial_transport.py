# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Real serial transport tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Tests the real GRBL serial transport against an in-memory fake connection.

No OS pty, socat or real hardware is needed: the transport is written
against a small SerialLike protocol (write/readline/close), so a plain fake
object proves the logic (framing, gating, fail-closed behavior) is correct
independent of pyserial or a real port - only open_serial_port() itself
needs pyserial, and it isn't exercised here (see its own docstring).
"""

import unittest

from hydra_umc_sdk.bridge_contract import CellState, MachineState
from hydra_umc_bridge_cnc import CncSnapshot, GrblRealtimeControl, GrblSerialProbe


class FakeSerial:
    """A minimal, deterministic stand-in for a real pyserial connection."""

    def __init__(self, response_line: bytes = b"<Idle|MPos:0,0,0>\n"):
        self.response_line = response_line
        self.written: list[bytes] = []
        self.closed = False
        self.raise_on_write: OSError | None = None

    def write(self, data: bytes):
        if self.raise_on_write:
            raise self.raise_on_write
        self.written.append(data)

    def readline(self) -> bytes:
        return self.response_line

    def close(self):
        self.closed = True


class GrblSerialProbeTests(unittest.TestCase):
    def test_status_query_sends_the_real_grbl_realtime_byte(self):
        connection = FakeSerial(b"<Idle|MPos:0,0,0>\n")
        GrblSerialProbe().query_status(connection, estop=False, door_closed=True)
        self.assertEqual(connection.written, [b"?"])

    def test_idle_response_with_safeguards_present_is_reported_idle(self):
        connection = FakeSerial(b"<Idle|MPos:0,0,0>\n")
        snapshot = GrblSerialProbe().query_status(connection, estop=False, door_closed=True)
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)

    def test_hold_response_is_reported_holding_not_running(self):
        connection = FakeSerial(b"<Hold:0|MPos:1,2,3>\n")
        snapshot = GrblSerialProbe().query_status(connection, estop=False, door_closed=True)
        self.assertEqual(snapshot.machine_state(), MachineState.HOLDING)

    def test_missing_safeguards_still_fail_closed_over_a_real_connection(self):
        connection = FakeSerial(b"<Idle|MPos:0,0,0>\n")
        snapshot = GrblSerialProbe().query_status(connection, estop=None, door_closed=None)
        self.assertEqual(snapshot.machine_state(), MachineState.SAFE_STOP)

    def test_a_transport_failure_fails_closed_instead_of_crashing(self):
        connection = FakeSerial()
        connection.raise_on_write = OSError("device disconnected")
        snapshot = GrblSerialProbe().query_status(connection, estop=False, door_closed=True)
        self.assertEqual(snapshot.machine_state(), MachineState.SAFE_STOP)


class GrblRealtimeControlTests(unittest.TestCase):
    def test_feed_hold_is_always_allowed_and_sends_the_real_byte(self):
        connection = FakeSerial()
        result = GrblRealtimeControl().feed_hold(connection)
        self.assertTrue(result.allowed)
        self.assertTrue(result.executed)
        self.assertEqual(connection.written, [b"!"])

    def test_soft_reset_is_always_allowed_and_sends_the_real_byte(self):
        connection = FakeSerial()
        result = GrblRealtimeControl().soft_reset(connection)
        self.assertTrue(result.allowed)
        self.assertEqual(connection.written, [b"\x18"])

    def test_resume_requires_a_genuinely_holding_cnc_not_idle(self):
        connection = FakeSerial()
        idle = CncSnapshot("IDLE", False, True)
        rejected = GrblRealtimeControl().cycle_start_resume(connection, CellState.READY, idle)
        self.assertFalse(rejected.allowed)
        self.assertEqual(connection.written, [])

        holding = CncSnapshot("Hold", False, True)
        accepted = GrblRealtimeControl().cycle_start_resume(connection, CellState.READY, holding)
        self.assertTrue(accepted.allowed)
        self.assertTrue(accepted.executed)
        self.assertEqual(connection.written, [b"~"])

    def test_resume_requires_a_ready_cell(self):
        connection = FakeSerial()
        holding = CncSnapshot("Hold", False, True)
        result = GrblRealtimeControl().cycle_start_resume(connection, CellState.FAULT, holding)
        self.assertFalse(result.allowed)
        self.assertEqual(connection.written, [])

    def test_a_write_failure_reports_not_executed_instead_of_crashing(self):
        connection = FakeSerial()
        connection.raise_on_write = OSError("device disconnected")
        result = GrblRealtimeControl().feed_hold(connection)
        self.assertTrue(result.allowed)
        self.assertFalse(result.executed)
        self.assertIn("serial write failed", result.reason)


class OpenSerialPortTests(unittest.TestCase):
    def test_missing_pyserial_raises_a_clear_runtime_error_not_an_import_error(self):
        # Proves the lazy-import degrades cleanly - this test only checks
        # the failure path is a clean, documented RuntimeError; it does not
        # require pyserial to be installed or absent either way, since the
        # real assertion only fires when the import genuinely fails.
        from hydra_umc_bridge_cnc import open_serial_port

        try:
            import serial  # noqa: F401

            self.skipTest("pyserial is installed in this environment - nothing to prove here")
        except ImportError:
            pass
        with self.assertRaises(RuntimeError) as context:
            open_serial_port("COM3")
        self.assertIn("pyserial is not installed", str(context.exception))


if __name__ == "__main__":
    unittest.main()
