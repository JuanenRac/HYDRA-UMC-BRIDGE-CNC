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

import os
import unittest

from hydra_umc_sdk.bridge_contract import CellState, MachineState
from hydra_umc_bridge_cnc import CncSnapshot, GrblRealtimeControl, GrblSerialProbe
from hydra_umc_bridge_cnc.serial_transport import ReconnectingSerialConnection, resolve_serial_device_path


class FakeSerial:
    """A minimal, deterministic stand-in for a real pyserial connection."""

    def __init__(self, response_line: bytes = b"<Idle|MPos:0,0,0>\n"):
        self.response_line = response_line
        self.written: list[bytes] = []
        self.closed = False
        self.raise_on_write: OSError | None = None
        # (P2):
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

    # regression: the interlocks were read BEFORE the
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

    # regression: a real 0-byte write (no
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

    # 's
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


class FakePortInfo:
    """Minimal stand-in for pyserial's ListPortInfo - only the attributes
    resolve_serial_device_path() actually reads."""

    def __init__(self, device: str, serial_number: str | None = None, hwid: str = ""):
        self.device = device
        self.serial_number = serial_number
        self.hwid = hwid


class ResolveSerialDevicePathTests(unittest.TestCase):
    def test_an_already_existing_path_is_returned_as_is(self):
        # Covers the real Linux /dev/serial/by-id/* case: the OS/udev
        # symlink itself is already the stable identifier, no enumeration
        # needed - this test uses this very test file's own path (always
        # exists) rather than a real serial device, since only the
        # "path exists" branch is under test here.
        this_file = os.path.abspath(__file__)
        self.assertEqual(resolve_serial_device_path(this_file), this_file)

    def test_a_serial_number_match_resolves_to_the_current_device_path(self):
        ports = [
            FakePortInfo("/dev/ttyUSB0", serial_number="AB123"),
            FakePortInfo("/dev/ttyUSB1", serial_number="ZZ999"),
        ]
        resolved = resolve_serial_device_path("AB123", list_ports=lambda: ports)
        self.assertEqual(resolved, "/dev/ttyUSB0")

    def test_a_replug_that_changes_the_port_name_still_resolves_by_id(self):
        # The real scenario this whole feature exists for: the same
        # physical device (same serial number) now enumerates under a
        # different OS-assigned port after a replug.
        before = [FakePortInfo("COM3", serial_number="AB123")]
        after = [FakePortInfo("COM5", serial_number="AB123")]
        self.assertEqual(resolve_serial_device_path("AB123", list_ports=lambda: before), "COM3")
        self.assertEqual(resolve_serial_device_path("AB123", list_ports=lambda: after), "COM5")

    def test_a_hwid_substring_match_also_resolves(self):
        ports = [FakePortInfo("COM4", serial_number=None, hwid="USB VID:PID=1A86:7523 SER=AB123")]
        resolved = resolve_serial_device_path("VID:PID=1A86:7523", list_ports=lambda: ports)
        self.assertEqual(resolved, "COM4")

    def test_a_device_that_is_not_currently_connected_raises_a_clear_error(self):
        with self.assertRaises(RuntimeError) as context:
            resolve_serial_device_path("not-plugged-in", list_ports=lambda: [])
        self.assertIn("not-plugged-in", str(context.exception))


class ReconnectingSerialConnectionTests(unittest.TestCase):
    def test_lazily_opens_the_connection_on_first_use(self):
        opened: list[FakeSerial] = []

        def connect() -> FakeSerial:
            fake = FakeSerial()
            opened.append(fake)
            return fake

        reconnecting = ReconnectingSerialConnection(connect)
        self.assertEqual(opened, [])
        reconnecting.write(b"?")
        self.assertEqual(len(opened), 1)

    def test_a_write_failure_triggers_exactly_one_reconnect_and_the_write_is_retried(self):
        first = FakeSerial()
        first.raise_on_write = OSError("device disconnected")
        second = FakeSerial()
        connections = [first, second]

        reconnecting = ReconnectingSerialConnection(lambda: connections.pop(0))
        written = reconnecting.write(b"?")

        self.assertTrue(first.closed)
        self.assertEqual(second.written, [b"?"])
        self.assertEqual(written, 1)

    def test_a_readline_failure_triggers_exactly_one_reconnect_and_the_read_is_retried(self):
        class FlakyThenGood(FakeSerial):
            def __init__(self, fail: bool):
                super().__init__(b"<Idle|MPos:0,0,0>\n")
                self._fail = fail

            def readline(self) -> bytes:
                if self._fail:
                    raise OSError("device disconnected")
                return super().readline()

        connections = [FlakyThenGood(fail=True), FlakyThenGood(fail=False)]
        reconnecting = ReconnectingSerialConnection(lambda: connections.pop(0))
        line = reconnecting.readline()
        self.assertEqual(line, b"<Idle|MPos:0,0,0>\n")

    def test_a_failed_reconnect_propagates_the_original_oserror(self):
        # The device genuinely is not back yet - this must fail closed
        # (an OSError, same as GrblSerialProbe/GrblRealtimeControl already
        # handle), not raise something callers don't already expect, and
        # not silently swallow the failure.
        first = FakeSerial()
        first.raise_on_write = OSError("device disconnected")

        def connect_always_fails() -> FakeSerial:
            fake = FakeSerial()
            fake.raise_on_write = OSError("still not plugged in")
            return fake

        calls = {"n": 0}

        def connect() -> FakeSerial:
            calls["n"] += 1
            return first if calls["n"] == 1 else connect_always_fails()

        reconnecting = ReconnectingSerialConnection(connect)
        with self.assertRaises(OSError):
            reconnecting.write(b"?")

    def test_query_status_survives_a_replug_via_the_reconnecting_connection(self):
        # End-to-end proof this composes with the existing, untouched
        # GrblSerialProbe: a real interlock query must keep working across
        # a simulated cable replug, not just the raw write()/readline().
        first = FakeSerial()
        first.raise_on_write = OSError("unplugged")
        second = FakeSerial(b"<Idle|MPos:0,0,0>\n")
        connections = [first, second]

        reconnecting = ReconnectingSerialConnection(lambda: connections.pop(0))
        # query_status() calls write() once, then readline() - the write
        # transparently reconnects onto `second` (proving the replug is
        # invisible to GrblSerialProbe, which is never touched by this
        # feature), and the query completes normally on the healed
        # connection instead of falling back to a fail-closed snapshot.
        snapshot = GrblSerialProbe().query_status(reconnecting, estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)
        self.assertTrue(first.closed)


if __name__ == "__main__":
    unittest.main()
