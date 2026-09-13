# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Bridge <-> realistic GRBL v1.1 emulator tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Exercise this bridge's REAL serial_transport code (GrblSerialProbe,
GrblRealtimeControl) end to end against a protocol-faithful GRBL v1.1
emulator, driving the *physical* side (alarm, safety door, running job)
the way real hardware would - not a canned one-line ``FakeSerial``.
"""
from __future__ import annotations

import unittest

from hydra_umc_sdk.bridge_contract import CellState, MachineState

from hydra_umc_bridge_cnc.serial_transport import GrblRealtimeControl, GrblSerialProbe

from grbl_emulator import GrblEmulator


def _drain_welcome(grbl: GrblEmulator) -> bytes:
    """A real client reads GRBL's welcome banner before its first query."""
    return grbl.readline()


class GrblEmulatorProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.grbl = GrblEmulator()
        self.probe = GrblSerialProbe()
        self.control = GrblRealtimeControl()

    def test_emits_a_real_welcome_banner_first(self) -> None:
        self.assertEqual(_drain_welcome(self.grbl), b"Grbl 1.1h ['$' for help]\r\n")

    def test_status_query_returns_a_real_frame_the_bridge_parses_as_idle(self) -> None:
        _drain_welcome(self.grbl)
        snapshot = self.probe.query_status(self.grbl, estop=lambda: False, door_closed=lambda: True)
        # The bridge only trusts the leading "<State|" token of a real frame.
        self.assertEqual(snapshot.controller_state, "Idle")
        self.assertIs(snapshot.machine_state(), MachineState.IDLE)

    def test_feed_hold_then_resume_moves_through_real_run_hold_run(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.start_program()  # native controller authority: job is running
        running = self.probe.query_status(self.grbl, estop=lambda: False, door_closed=lambda: True)
        self.assertIs(running.machine_state(), MachineState.RUNNING)

        hold = self.control.feed_hold(self.grbl)
        self.assertTrue(hold.allowed and hold.executed)
        held = self.probe.query_status(self.grbl, estop=lambda: False, door_closed=lambda: True)
        self.assertIs(held.machine_state(), MachineState.HOLDING)

        # Resume is gated on a genuinely HOLDING machine beside a READY cell.
        resumed = self.control.cycle_start_resume(self.grbl, CellState.READY, held)
        self.assertTrue(resumed.allowed and resumed.executed, resumed.reason)
        back = self.probe.query_status(self.grbl, estop=lambda: False, door_closed=lambda: True)
        self.assertIs(back.machine_state(), MachineState.RUNNING)

    def test_resume_is_refused_when_the_machine_is_not_actually_holding(self) -> None:
        _drain_welcome(self.grbl)
        idle = self.probe.query_status(self.grbl, estop=lambda: False, door_closed=lambda: True)
        result = self.control.cycle_start_resume(self.grbl, CellState.READY, idle)
        self.assertFalse(result.executed)
        self.assertIn("not HOLDING", result.reason)

    def test_a_latched_alarm_reads_as_FAULT_and_survives_a_soft_reset(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.trigger_alarm(1)  # simulated hard-limit / E-STOP
        self.grbl.readline()        # the async "ALARM:1" line GRBL pushes

        alarmed = self.probe.query_status(self.grbl, estop=lambda: False, door_closed=lambda: True)
        self.assertIs(alarmed.machine_state(), MachineState.FAULT)

        # Soft reset (always allowed) does NOT clear a latched alarm on real GRBL.
        self.assertTrue(self.control.soft_reset(self.grbl).executed)
        self.grbl.readline()  # re-emitted welcome
        still_alarmed = self.probe.query_status(self.grbl, estop=lambda: False, door_closed=lambda: True)
        self.assertIs(still_alarmed.machine_state(), MachineState.FAULT)

    def test_open_safety_door_blocks_resume_until_it_is_closed(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.start_program()
        self.control.feed_hold(self.grbl)
        self.grbl.open_door()

        held_door_open = self.probe.query_status(self.grbl, estop=lambda: False, door_closed=lambda: False)
        # The bridge's own door_closed=False alone forces SAFE_STOP...
        self.assertIs(held_door_open.machine_state(), MachineState.SAFE_STOP)
        # ...and the real-time resume byte the bridge would send is a no-op
        # on the emulator while the door is physically open.
        self.grbl.write(b"~")
        self.assertEqual(self.grbl.state, "Hold")

        self.grbl.close_door()
        self.grbl.write(b"~")
        self.assertEqual(self.grbl.state, "Run")

    def test_line_channel_answers_ok_and_error_on_its_own_frames(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.write(b"$G\n")
        self.assertEqual(self.grbl.readline(), b"[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n")
        self.assertEqual(self.grbl.readline(), b"ok\r\n")

        self.grbl.write(b"BOGUS\n")
        self.assertEqual(self.grbl.readline(), b"error:20\r\n")

        # Everything but $X / real-time is locked out while ALARM is latched.
        self.grbl.trigger_alarm(2)
        self.grbl.readline()  # ALARM:2
        self.grbl.write(b"G0 X10\n")
        self.assertEqual(self.grbl.readline(), b"error:9\r\n")
        self.grbl.write(b"$X\n")
        self.assertEqual(self.grbl.readline(), b"[MSG:Caution: Unlocked]\r\n")
        self.assertEqual(self.grbl.readline(), b"ok\r\n")

    def test_g10_sets_a_real_work_offset_reflected_in_the_status_frame(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.write(b"?")
        self.assertIn(b"WCO:0.000,0.000,0.000", self.grbl.readline())

        self.grbl.write(b"G10 L2 P1 X10 Y5 Z0\n")
        self.assertEqual(self.grbl.readline(), b"ok\r\n")

        self.grbl.write(b"?")
        self.assertIn(b"WCO:10.000,5.000,0.000", self.grbl.readline())

    def test_g92_derives_its_offset_from_the_real_machine_position(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.set_machine_position(50.0, 20.0, 0.0)

        # "The point I'm at right now IS work X0 Y0" - offset must be
        # exactly the current machine position for the axes given.
        self.grbl.write(b"G92 X0 Y0\n")
        self.assertEqual(self.grbl.readline(), b"ok\r\n")
        self.grbl.write(b"?")
        self.assertIn(b"WCO:50.000,20.000,0.000", self.grbl.readline())

        # G92.1 clears it back to a real zero offset, not just the display.
        self.grbl.write(b"G92.1\n")
        self.assertEqual(self.grbl.readline(), b"ok\r\n")
        self.grbl.write(b"?")
        self.assertIn(b"WCO:0.000,0.000,0.000", self.grbl.readline())

    def test_switching_work_coordinate_system_switches_the_reported_offset(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.write(b"G10 L2 P1 X10 Y0 Z0\n")
        self.grbl.readline()
        self.grbl.write(b"G10 L2 P2 X-10 Y0 Z0\n")
        self.grbl.readline()

        self.grbl.write(b"?")
        self.assertIn(b"WCO:10.000,0.000,0.000", self.grbl.readline())  # still G54

        self.grbl.write(b"G55\n")
        self.assertEqual(self.grbl.readline(), b"ok\r\n")
        self.grbl.write(b"?")
        self.assertIn(b"WCO:-10.000,0.000,0.000", self.grbl.readline())

    def test_motion_target_is_computed_through_the_active_work_offset(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.write(b"G10 L2 P1 X100 Y0 Z0\n")
        self.grbl.readline()

        # Commanding work-space X0 Y0 with a +100 work offset must land the
        # MACHINE at X100, not X0 - the exact offset-aware chain a bridge
        # (or any future consumer of MPos/WCO) needs to get right.
        self.grbl.write(b"G1 X0 Y0\n")
        self.assertEqual(self.grbl.readline(), b"ok\r\n")
        self.grbl.write(b"?")
        self.assertIn(b"MPos:0.000,0.000,0.000", self.grbl.readline())  # accepted, not yet executed

        self.grbl.complete_next_block()
        self.assertEqual(self.grbl.machine_position, (100.0, 0.0, 0.0))

    def test_a_full_planner_buffer_withholds_ok_until_a_block_completes(self) -> None:
        _drain_welcome(self.grbl)
        for i in range(1, self.grbl.planner_buffer_size + 1):
            self.grbl.write(f"G1 X{i}\n".encode("ascii"))
            self.assertEqual(
                self.grbl.readline(), b"ok\r\n",
                f"block {i} should have been accepted immediately - buffer not full yet",
            )

        # The buffer is now exactly full - one more block queues but its
        # `ok` is real backpressure, withheld until a slot frees up.
        self.grbl.write(b"G1 X999\n")
        self.assertEqual(
            self.grbl.readline(), b"",
            "a block accepted while the planner buffer is full must not get an ok yet",
        )

        self.grbl.complete_next_block()  # drains the oldest queued block (X1)
        self.assertEqual(self.grbl.machine_position, (1.0, 0.0, 0.0))
        # Freeing that one slot must release exactly the deferred ok - no more.
        self.assertEqual(self.grbl.readline(), b"ok\r\n")
        self.assertEqual(self.grbl.readline(), b"")

    def test_complete_next_block_refuses_to_run_with_nothing_queued(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.start_program()  # Run, but via the OTHER (non-planner) path
        with self.assertRaises(RuntimeError):
            self.grbl.complete_next_block()

    def test_complete_next_block_refuses_to_advance_while_not_running(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.write(b"G1 X10\n")
        self.grbl.readline()  # ok
        self.control.feed_hold(self.grbl)
        self.assertEqual(self.grbl.state, "Hold")

        # Real GRBL never advances the planner during a hold - a test (or a
        # bridge) that tried to fake progress here must fail loudly instead.
        with self.assertRaises(RuntimeError):
            self.grbl.complete_next_block()

    def test_soft_reset_discards_the_whole_planner_queue(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.write(b"G1 X10\n")
        self.grbl.readline()
        self.grbl.write(b"G1 X20\n")
        self.grbl.readline()

        self.control.soft_reset(self.grbl)
        self.grbl.readline()  # re-emitted welcome

        # Nothing survives a reset to complete - real GRBL requires the
        # sender to restart its stream from scratch.
        self.grbl.start_program()
        with self.assertRaises(RuntimeError):
            self.grbl.complete_next_block()

    def test_state_returns_to_idle_once_the_queue_fully_drains(self) -> None:
        _drain_welcome(self.grbl)
        self.grbl.write(b"G1 X10\n")
        self.grbl.readline()
        self.assertEqual(self.grbl.state, "Run")

        self.grbl.complete_next_block()
        self.assertEqual(self.grbl.state, "Idle")
        self.assertEqual(self.grbl.feed_rate, 0)

    def test_bridge_fails_closed_when_the_port_returns_nothing(self) -> None:
        # A real pyserial read timeout returns b"" - drain the welcome so
        # the next readline() is genuinely empty.
        _drain_welcome(self.grbl)
        while self.grbl.readline():
            pass
        # No "?" is answered because the probe writes then reads one line;
        # with the queue empty the emulator's own "?" reply IS produced,
        # so instead simulate a dead port explicitly.
        self.grbl.close()
        snapshot = self.probe.query_status(self.grbl, estop=lambda: True, door_closed=lambda: True)
        self.assertEqual(snapshot.controller_state, "")
        self.assertIs(snapshot.machine_state(), MachineState.SAFE_STOP)


if __name__ == "__main__":
    unittest.main()
