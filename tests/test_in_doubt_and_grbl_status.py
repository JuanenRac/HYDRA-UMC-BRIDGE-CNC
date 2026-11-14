# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - A job in doubt is looked up, and the GRBL state words are read by the shared table
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A repeat whose first attempt left no answer is answered with what the controller reports (switch off by default) and never evaluated again; the state word of a GRBL report is read by the table of the SDK. Simulated controller, evidence=simulator."""

import json
import unittest

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState, job_to_dict
from hydra_umc_sdk.grbl_status import GRBL_STATES, grbl_machine_state
from hydra_umc_sdk.job_intake import IdempotencyStore

from hydra_umc_bridge_cnc import CncMqttBridge
from hydra_umc_bridge_cnc.cell import CncSnapshot
from hydra_umc_bridge_cnc.mqtt_transport import IN_DOUBT_CONSULT_ENVIRONMENT, TOPIC_PREFIX

JOB_TOPIC = f"{TOPIC_PREFIX}cmd/job"


class ScriptedSerial:
    """A GRBL port that answers every status query with the next scripted line (the last one repeats); `fail` makes it raise."""

    def __init__(self, *lines, fail=False):
        self.lines = list(lines) or [b"<Idle|MPos:0,0,0>\n"]
        self.written = []
        self.fail = fail

    def write(self, data):
        self.written.append(data)
        return len(data)

    def readline(self):
        if self.fail:
            raise OSError("port lost")
        return self.lines.pop(0) if len(self.lines) > 1 else self.lines[0]

    def close(self):
        pass


def job_message(key="key-1"):
    return json.dumps(job_to_dict(BridgeJob("job-1", key, "orchestrator", JobPhase.LOAD, MachineState.IDLE, {}))).encode("utf-8")


def make(serial, store, **options):
    return CncMqttBridge(serial, lambda: False, lambda: True, lambda: CellState.READY, store, **options)


def in_doubt_store(key="key-1"):
    store = IdempotencyStore()
    assert store.claim(key)  # the first attempt claimed the key and never stored an answer
    return store


class InDoubtOffByDefaultTests(unittest.TestCase):
    def test_off_by_default_a_job_in_doubt_is_refused_as_before(self):
        serial = ScriptedSerial()
        out = make(serial, in_doubt_store()).handle_message(JOB_TOPIC, job_message())
        body = json.loads(out[0].payload)
        self.assertFalse(body["allowed"])
        self.assertIn("already handled", body["reason"])
        self.assertNotIn("in_doubt", body)
        self.assertEqual(serial.written, [], "the controller is not even asked while the switch is off")

    def test_the_environment_switch_turns_it_on(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {IN_DOUBT_CONSULT_ENVIRONMENT: "1"}):
            out = make(ScriptedSerial(), in_doubt_store()).handle_message(JOB_TOPIC, job_message())
        self.assertTrue(json.loads(out[0].payload)["in_doubt"])


class InDoubtConsultTests(unittest.TestCase):
    def test_the_repeat_is_answered_with_the_state_the_controller_reports_and_is_not_evaluated_again(self):
        serial = ScriptedSerial(b"<Run|MPos:1,2,3|FS:500,8000>\n")
        out = make(serial, in_doubt_store(), in_doubt_consult=True).handle_message(JOB_TOPIC, job_message())
        body = json.loads(out[0].payload)
        self.assertEqual((body["allowed"], body["in_doubt"], body["forwarded"], body["machine_state"]), (False, True, False, "RUNNING"))
        self.assertEqual(body["controller_state"], "Run")
        self.assertEqual(out[0].qos, 1)
        self.assertIn("RUNNING", body["reason"])

    def test_the_answer_is_stored_so_a_third_repeat_gets_the_same_one_without_asking_again(self):
        serial = ScriptedSerial(b"<Hold:0|MPos:0,0,0>\n", b"<Idle|MPos:0,0,0>\n")
        bridge = make(serial, in_doubt_store(), in_doubt_consult=True)
        first = bridge.handle_message(JOB_TOPIC, job_message())[0]
        asked = len(serial.written)
        second = bridge.handle_message(JOB_TOPIC, job_message())[0]
        self.assertEqual(second.payload, first.payload)
        self.assertEqual(json.loads(first.payload)["machine_state"], "HOLDING")
        self.assertEqual(len(serial.written), asked)

    def test_a_controller_that_cannot_be_read_leaves_the_repeat_refused(self):
        out = make(ScriptedSerial(fail=True), in_doubt_store(), in_doubt_consult=True).handle_message(JOB_TOPIC, job_message())
        body = json.loads(out[0].payload)
        self.assertFalse(body["allowed"])
        self.assertNotIn("in_doubt", body)

    def test_the_gate_is_never_run_for_a_repeat_even_when_the_machine_is_idle_and_the_cell_ready(self):
        out = make(ScriptedSerial(b"<Idle|MPos:0,0,0>\n"), in_doubt_store(), in_doubt_consult=True).handle_message(JOB_TOPIC, job_message())
        body = json.loads(out[0].payload)
        self.assertFalse(body["allowed"], "an idle machine does not turn a repeat into a new job")

    def test_a_key_with_a_stored_answer_still_gets_that_answer_not_a_reading(self):
        store = IdempotencyStore()
        bridge = make(ScriptedSerial(), store, in_doubt_consult=True)
        first = bridge.handle_message(JOB_TOPIC, job_message())[0]
        second = bridge.handle_message(JOB_TOPIC, job_message())[0]
        self.assertEqual(second.payload, first.payload)
        self.assertNotIn("in_doubt", json.loads(second.payload))

    def test_a_new_key_is_evaluated_normally_with_the_switch_on(self):
        out = make(ScriptedSerial(), in_doubt_store("other"), in_doubt_consult=True).handle_message(JOB_TOPIC, job_message("key-2"))
        self.assertTrue(json.loads(out[0].payload)["allowed"])

    def test_without_a_store_nothing_is_in_doubt(self):
        out = make(ScriptedSerial(), None, in_doubt_consult=True).handle_message(JOB_TOPIC, job_message())
        self.assertTrue(json.loads(out[0].payload)["allowed"])


class SharedGrblTableTests(unittest.TestCase):
    def snapshot(self, word, **flags):
        return CncSnapshot(word, flags.get("estop", False), flags.get("door_closed", True))

    def test_every_word_of_the_shared_table_is_read_the_same_way_and_with_any_substate_or_case(self):
        for word, expected in GRBL_STATES.items():
            for spelled in (word, word.title(), word.lower(), f"{word.title()}:0", f"{word}:3"):
                self.assertEqual(self.snapshot(spelled).machine_state(), expected, spelled)
                self.assertEqual(self.snapshot(spelled).machine_state(), grbl_machine_state(spelled, {"OFF": MachineState.FAULT}), spelled)

    def test_the_words_this_bridge_always_read_keep_their_state(self):
        expected = {"Idle": MachineState.IDLE, "Run": MachineState.RUNNING, "Jog": MachineState.RUNNING, "Home": MachineState.RUNNING, "Hold:0": MachineState.HOLDING,
                    "Hold:1": MachineState.HOLDING, "Alarm": MachineState.FAULT, "Alarm:1": MachineState.FAULT, "Door:0": MachineState.SAFE_STOP, "Door:3": MachineState.SAFE_STOP,
                    "OFF": MachineState.FAULT, "off": MachineState.FAULT, "Stopped": MachineState.STOPPED}
        for word, state in expected.items():
            self.assertEqual(self.snapshot(word).machine_state(), state, word)

    def test_a_word_nobody_knows_and_a_value_that_is_not_text_are_offline_never_idle(self):
        for word in ("Check", "Sleep", "Idle2", "", "Bogus:1", None, 5, b"Idle"):
            self.assertEqual(self.snapshot(word).machine_state(), MachineState.OFFLINE, repr(word))

    def test_the_safety_inputs_still_win_over_the_word(self):
        self.assertEqual(self.snapshot("Idle", estop=True).machine_state(), MachineState.SAFE_STOP)
        self.assertEqual(self.snapshot("Idle", door_closed=False).machine_state(), MachineState.SAFE_STOP)

    def test_only_idle_allows_work(self):
        allowing = [word for word in list(GRBL_STATES) + ["OFF", "Bogus"] if self.snapshot(word).machine_state() is MachineState.IDLE]
        self.assertEqual(allowing, ["IDLE"])


if __name__ == "__main__":
    unittest.main()
