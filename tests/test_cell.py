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


if __name__ == "__main__": unittest.main()
