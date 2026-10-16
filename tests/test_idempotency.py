# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - A repeated idempotency key is answered once
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""docs/BRIDGE_CONTRACT.md: the idempotency key of a job is handled once; a repeat gets the answer given the first time and is not evaluated again."""

import json
import unittest

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState, job_to_dict
from hydra_umc_sdk.job_intake import IdempotencyStore

from hydra_umc_bridge_cnc import CncMqttBridge
from hydra_umc_bridge_cnc.mqtt_transport import TOPIC_PREFIX


class FakeSerial:
    def write(self, data):
        return len(data)

    def readline(self):
        return b"<Idle|MPos:0,0,0>\n"

    def close(self):
        pass


def message(key="key-1"):
    job = BridgeJob("job-1", key, "orchestrator", JobPhase.LOAD, MachineState.IDLE, {})
    return json.dumps(job_to_dict(job)).encode("utf-8")


class IdempotencyTests(unittest.TestCase):
    def bridge(self, store, cell):
        return CncMqttBridge(FakeSerial(), lambda: False, lambda: True, lambda: cell[0], store)

    def test_a_repeated_key_gets_the_first_answer_even_if_the_cell_changed_since(self):
        cell = [CellState.READY]
        bridge = self.bridge(IdempotencyStore(), cell)
        first = bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", message())[0]
        cell[0] = CellState.FAULT
        second = bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", message())[0]
        self.assertTrue(json.loads(first.payload)["allowed"])
        self.assertEqual(second.payload, first.payload)

    def test_a_new_key_is_evaluated_again(self):
        cell = [CellState.READY]
        bridge = self.bridge(IdempotencyStore(), cell)
        bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", message("a"))
        cell[0] = CellState.FAULT
        self.assertFalse(json.loads(bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", message("b"))[0].payload)["allowed"])

    def test_without_a_store_nothing_is_deduplicated(self):
        cell = [CellState.READY]
        bridge = self.bridge(None, cell)
        bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", message())
        cell[0] = CellState.FAULT
        self.assertFalse(json.loads(bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", message())[0].payload)["allowed"])


if __name__ == "__main__":
    unittest.main()
