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
        # V07-019 (P2):
        # defaults to the real pyserial contract - write() reports the
        # real byte count actually written. A test overrides this to a
        # specific (short, or otherwise wrong) value to simulate that
        # real failure mode; `report_byte_count = False` below simulates
        # a serial-like implementation that does not conform to that
        # contract AT ALL (returns None) - production no longer trusts
        # that silently either way, see GrblRealtimeControl._write's own
        # comment. Fixing this fake to report a real count by default
        # (instead of relaxing production for a fake that doesn't) is
        # an explicit correction.
        self.write_return_value: int | None = None
        self.report_byte_count = True

    def write(self, data: bytes):
        if self.raise_on_write:
            raise self.raise_on_write
        self.written.append(data)
        if not self.report_byte_count:
            return None
        if self.write_return_value is not None:
            return self.write_return_value
        return len(data)

    def readline(self) -> bytes:
        return self.response_line

    def close(self):
        self.closed = True


class GrblSerialProbeTests(unittest.TestCase):
    def test_status_query_sends_the_real_grbl_realtime_byte(self):
        connection = FakeSerial(b"<Idle|MPos:0,0,0>\n")
        GrblSerialProbe().query_status(connection, estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(connection.written, [b"?"])

    def test_idle_response_with_safeguards_present_is_reported_idle(self):
        connection = FakeSerial(b"<Idle|MPos:0,0,0>\n")
        snapshot = GrblSerialProbe().query_status(connection, estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)

    def test_hold_response_is_reported_holding_not_running(self):
        connection = FakeSerial(b"<Hold:0|MPos:1,2,3>\n")
        snapshot = GrblSerialProbe().query_status(connection, estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.HOLDING)

    def test_missing_safeguards_still_fail_closed_over_a_real_connection(self):
        connection = FakeSerial(b"<Idle|MPos:0,0,0>\n")
        snapshot = GrblSerialProbe().query_status(connection, estop=lambda: None, door_closed=lambda: None)
        self.assertEqual(snapshot.machine_state(), MachineState.SAFE_STOP)

    def test_a_getter_that_raises_fails_closed_instead_of_crashing(self):
        connection = FakeSerial(b"<Idle|MPos:0,0,0>\n")

        def _broken_sensor():
            raise RuntimeError("real sensor read failed")

        snapshot = GrblSerialProbe().query_status(connection, estop=_broken_sensor, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.SAFE_STOP)

    def test_a_transport_failure_fails_closed_instead_of_crashing(self):
        connection = FakeSerial()
        connection.raise_on_write = OSError("device disconnected")
        snapshot = GrblSerialProbe().query_status(connection, estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.SAFE_STOP)

    # REV-005 regression: the interlocks were read BEFORE the
    # blocking write()/readline() below, so a real E-STOP hit or door
    # opened DURING that block went unnoticed by the snapshot this call
    # returns. `FakeSerial` here flips both signals as a side effect of
    # `readline()` itself - exactly modelling the physical event landing
    # while the real call is blocked waiting on GRBL's reply.
    def test_a_signal_that_changes_during_the_blocking_read_is_still_caught(self):
        class FlippingSerial(FakeSerial):
            def __init__(self):
                super().__init__(b"<Hold:0|MPos:0,0,0>\n")
                self.estop_active = False
                self.door_open = False

            def readline(self) -> bytes:
                # Simulate the real physical event happening WHILE this
                # blocking call is in flight, before it returns.
                self.estop_active = True
                self.door_open = True
                return super().readline()

        connection = FlippingSerial()
        snapshot = GrblSerialProbe().query_status(
            connection,
            estop=lambda: connection.estop_active,
            door_closed=lambda: not connection.door_open,
        )
        # The GRBL status line itself still says "Hold", but the real,
        # current interlock state (read AFTER the blocking call) must win.
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

    # REV-006 regression: a real 0-byte write (no
    # exception raised at all - the connection is fine, nothing actually
    # reached the wire) reported as `executed=True`. For a real stop/reset
    # command this makes the safety evidence itself misleading.
    def test_a_zero_byte_write_reports_not_executed_instead_of_a_false_success(self):
        connection = FakeSerial()
        connection.write_return_value = 0
        result = GrblRealtimeControl().feed_hold(connection)
        self.assertTrue(result.allowed)
        self.assertFalse(result.executed)
        self.assertIn("serial write incomplete", result.reason)

    def test_a_partial_write_reports_not_executed(self):
        connection = FakeSerial()
        connection.write_return_value = 0  # _FEED_HOLD is a single byte - 0 < 1 is already "short"
        result = GrblRealtimeControl().soft_reset(connection)
        self.assertFalse(result.executed)

    # V07-019 (P2): REV-006's
    # own fix only caught a reported SHORT count - `None`/a bool/a
    # mismatched-but-real int were all still silently trusted as a
    # genuine confirmed write, just because nothing crashed. A real
    # pyserial connection's write() always returns the real int byte
    # count - now required exactly, not just "if it happens to look like
    # one". Every real fake in this suite defaults to reporting a real
    # byte count instead (see FakeSerial's own comment) - an
    # explicit correction, fixing the fakes rather than relaxing
    # production for them.
    def test_a_fake_that_does_not_report_a_real_byte_count_is_no_longer_trusted(self):
        connection = FakeSerial()
        connection.report_byte_count = False
        result = GrblRealtimeControl().feed_hold(connection)
        self.assertTrue(result.allowed)
        self.assertFalse(result.executed)
        self.assertIn("did not report a real byte count", result.reason)

    def test_a_bool_return_value_is_never_trusted_as_a_real_byte_count(self):
        # bool is technically `isinstance(x, int)` in Python (True == 1) -
        # a serial-like implementation returning one is not reporting a
        # real byte count regardless.
        connection = FakeSerial()
        connection.write_return_value = True
        result = GrblRealtimeControl().feed_hold(connection)
        self.assertFalse(result.executed)

    def test_a_string_return_value_is_never_trusted_as_a_real_byte_count(self):
        connection = FakeSerial()
        connection.write_return_value = "1"
        result = GrblRealtimeControl().feed_hold(connection)
        self.assertFalse(result.executed)

    def test_a_reported_byte_count_greater_than_the_command_length_is_also_not_trusted(self):
        # Exact equality with len(command) is required, not just "not
        # less than" - a real single-byte GRBL realtime command reporting
        # 99 bytes written is nonsensical and must not be trusted either.
        connection = FakeSerial()
        connection.write_return_value = 99
        result = GrblRealtimeControl().feed_hold(connection)
        self.assertFalse(result.executed)


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
