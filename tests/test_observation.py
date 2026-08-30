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
