# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - CNC cell boundary tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
import unittest
from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState
from hydra_umc_bridge_cnc import CncCellBridge, CncSnapshot


def job(phase=JobPhase.LOAD):
    return BridgeJob("cnc-1", "cnc-key-1", "cnc", phase, MachineState.IDLE, {})


class CncCellTests(unittest.TestCase):
    def test_idle_safeguarded_cnc_permits_prepared_load(self):
        decision = CncCellBridge().plan(job(), CellState.READY, CncSnapshot("IDLE", False, True))
        self.assertTrue(decision.allowed)

    def test_open_door_blocks_productive_auxiliary_work(self):
        decision = CncCellBridge().plan(job(), CellState.READY, CncSnapshot("IDLE", False, False))
        self.assertFalse(decision.allowed)

    def test_abort_is_forwarded_during_estop(self):
        decision = CncCellBridge().plan(job(JobPhase.ABORT), CellState.SAFE_STOP, CncSnapshot("RUN", True, True))
        self.assertTrue(decision.allowed)

    def test_non_text_controller_state_fails_safe_instead_of_crashing(self):
        state = CncSnapshot(None, False, True).machine_state()  # type: ignore[arg-type]
        self.assertEqual(state, MachineState.OFFLINE)

    def test_real_grbl_jog_and_home_states_are_recognized_as_active_motion(self):
        # Real GRBL v1.1 status-report tokens (github.com/gnea/grbl/wiki/
        # Grbl-v1.1-Interface) - a machine jogging or homing is exactly as
        # busy as one running a program; both were previously swallowed by
        # the OFFLINE fallback, indistinguishable from "not reporting".
        for token in ("Jog", "Home"):
            with self.subTest(token=token):
                self.assertEqual(CncSnapshot(token, False, True).machine_state(), MachineState.RUNNING)

    def test_real_grbl_hold_state_maps_to_holding_not_running(self):
        # GRBL's real "Hold" means execution is intentionally paused (feed
        # hold), a distinct condition from actively running - matching the
        # real print_stats.state=paused -> HOLDING fix already made in the
        # sibling PRINTER3D bridge.
        self.assertEqual(CncSnapshot("Hold", False, True).machine_state(), MachineState.HOLDING)

    def test_real_grbl_alarm_state_is_a_fault_not_a_generic_offline_fallback(self):
        # GRBL's real "Alarm" state (limit trip, lost position, unresolved
        # E-STOP) is its most safety-critical report - it must be
        # distinguishable from "controller not reporting at all".
        self.assertEqual(CncSnapshot("Alarm", False, True).machine_state(), MachineState.FAULT)

    def test_real_grbl_door_state_is_a_safe_stop(self):
        # GRBL's own real safety-interlock state; this bridge's separate
        # door_closed input already forces SAFE_STOP, so this is a
        # defensive second signal confirming the same real condition.
        self.assertEqual(CncSnapshot("Door", False, True).machine_state(), MachineState.SAFE_STOP)

    def test_holding_cnc_does_not_permit_new_productive_work(self):
        decision = CncCellBridge().plan(job(), CellState.READY, CncSnapshot("Hold", False, True))
        self.assertFalse(decision.allowed)


if __name__ == "__main__": unittest.main()
