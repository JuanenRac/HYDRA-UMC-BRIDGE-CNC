# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Read-only controller observation tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hydra_umc_sdk.bridge_contract import MachineState
from hydra_umc_bridge_cnc.cell import STOPPED_STATE
from hydra_umc_bridge_cnc.observation import snapshot_from_grbl_status, snapshot_from_mapping, snapshot_from_mtconnect_execution


class CncObservationTests(unittest.TestCase):
    def test_complete_idle_evidence_maps_to_idle(self):
        snapshot = snapshot_from_mapping({"state": "idle", "estop": False, "door_closed": True})
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)

    def test_missing_or_non_boolean_safeguards_fail_closed(self):
        self.assertEqual(snapshot_from_mapping({"state": "IDLE"}).machine_state(), MachineState.SAFE_STOP)
        self.assertEqual(snapshot_from_mapping({"state": "IDLE", "estop": "false", "door_closed": 1}).machine_state(), MachineState.SAFE_STOP)

    def test_grbl_status_is_read_only_and_needs_independent_safeguards(self):
        self.assertEqual(snapshot_from_grbl_status("<Idle|MPos:0,0,0>", estop=False, door_closed=True).machine_state(), MachineState.IDLE)
        self.assertEqual(snapshot_from_grbl_status("<Idle|MPos:0,0,0>", estop=False, door_closed=None).machine_state(), MachineState.SAFE_STOP)

    def test_malformed_status_fails_offline_when_safeguards_are_present(self):
        self.assertEqual(snapshot_from_grbl_status("Idle", estop=False, door_closed=True).machine_state(), MachineState.OFFLINE)

    def test_mtconnect_execution_is_normalized_read_only_and_fails_closed(self):
        self.assertEqual(snapshot_from_mtconnect_execution("EXECUTING", estop=False, door_closed=True).machine_state(), MachineState.RUNNING)
        self.assertEqual(snapshot_from_mtconnect_execution("READY", estop=False, door_closed=True).machine_state(), MachineState.IDLE)
        self.assertEqual(snapshot_from_mtconnect_execution("UNKNOWN", estop=False, door_closed=True).machine_state(), MachineState.OFFLINE)
        self.assertEqual(snapshot_from_mtconnect_execution("READY", estop=False, door_closed="true").machine_state(), MachineState.SAFE_STOP)

    def test_mtconnect_stopped_is_not_idle_and_cannot_be_resumed(self):
        # STOPPED in MTConnect means stopped until the operator resets it, not ready for work.
        snapshot = snapshot_from_mtconnect_execution("STOPPED", estop=False, door_closed=True)
        self.assertNotEqual(snapshot.machine_state(), MachineState.IDLE)
        self.assertNotEqual(snapshot.machine_state(), MachineState.HOLDING)
        self.assertEqual(snapshot.machine_state(), STOPPED_STATE)

    def test_a_stopped_machine_gets_no_work(self):
        from hydra_umc_bridge_cnc.cell import CncCellBridge
        from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase
        snapshot = snapshot_from_mtconnect_execution("STOPPED", estop=False, door_closed=True)
        job = BridgeJob("j1", "k1", "test", JobPhase.LOAD, MachineState.IDLE, {})
        self.assertFalse(CncCellBridge().plan(job, CellState.READY, snapshot).allowed)

    @unittest.skipUnless(hasattr(MachineState, "STOPPED"), "the SDK of this pin has no STOPPED state yet")
    def test_stopped_is_its_own_machine_state_when_the_sdk_has_it(self):
        self.assertIs(STOPPED_STATE, MachineState.STOPPED)

    def test_mtconnect_feed_hold_and_interrupted_map_to_holding_not_running(self):
        # MTConnect's real FEED_HOLD/INTERRUPTED executions mean the
        # program is intentionally paused, not actively running - matching
        # the same real GRBL Hold -> HOLDING fix in cell.py.
        for execution in ("FEED_HOLD", "INTERRUPTED"):
            with self.subTest(execution=execution):
                snapshot = snapshot_from_mtconnect_execution(execution, estop=False, door_closed=True)
                self.assertEqual(snapshot.machine_state(), MachineState.HOLDING)

    def test_offline_cli_reads_saved_evidence_without_controller_connection(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.json"
            evidence.write_text(json.dumps({"grbl_status": "<Idle|MPos:0,0,0>", "estop": False, "door_closed": True}), encoding="utf-8")
            completed = subprocess.run([sys.executable, str(root / "tools" / "inspect_controller_evidence.py"), str(evidence)], text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["machine_state"], "IDLE")


if __name__ == "__main__":
    unittest.main()
