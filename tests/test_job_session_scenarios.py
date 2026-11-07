# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - A job session over MQTT topics against the GRBL emulator
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The real `CncMqttBridge` answers the topics of a whole job session while the GRBL emulator is driven through the faults of a real run (evidence: simulator).

Every message goes through `handle_message`, the way the broker delivers it: a status poll, a feed hold, a resume, the safety
door opening and closing, a latched alarm that survives a soft reset, and a new job after the recovery. The controller is the
protocol-faithful emulator of `grbl_emulator.py`, not a real GRBL board.
"""
from __future__ import annotations

import json
import unittest

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState, job_to_dict
from hydra_umc_bridge_cnc import CncMqttBridge
from hydra_umc_bridge_cnc.mqtt_transport import TOPIC_PREFIX

from grbl_emulator import GrblEmulator
from test_mqtt_transport import LiveSignals


def body(publishes, suffix: str) -> dict:
    for publish in publishes:
        if publish.topic == f"{TOPIC_PREFIX}{suffix}":
            return json.loads(publish.payload)
    raise AssertionError(f"no message on {suffix}: {[p.topic for p in publishes]}")


class JobSession:
    def __init__(self) -> None:
        self.grbl = GrblEmulator()
        self.grbl.readline()  # the welcome banner a real client reads first
        self.signals = LiveSignals()
        self.bridge = CncMqttBridge(self.grbl, lambda: self.signals.estop, lambda: self.signals.door_closed, lambda: self.signals.cell_state)
        self.number = 0

    def send(self, verb: str, payload: bytes = b""):
        return self.bridge.handle_message(f"{TOPIC_PREFIX}cmd/{verb}", payload)

    def state(self) -> str:
        return body(self.send("status"), "state")["machine_state"]

    def job(self, phase: JobPhase = JobPhase.LOAD) -> dict:
        self.number += 1
        job = BridgeJob(f"job-{self.number}", f"key-{self.number}", "orchestrator", phase, MachineState.IDLE, {})
        return body(self.send("job", json.dumps(job_to_dict(job)).encode()), "cmd/job/result")


class JobSessionTests(unittest.TestCase):
    def test_an_idle_machine_takes_a_job_and_a_running_one_does_not(self) -> None:
        session = JobSession()
        self.assertEqual(session.state(), "IDLE")
        self.assertTrue(session.job()["allowed"])
        session.grbl.start_program()
        self.assertEqual(session.state(), "RUNNING")
        self.assertFalse(session.job()["allowed"])

    def test_hold_then_resume_goes_run_hold_run_through_the_topics(self) -> None:
        session = JobSession()
        session.grbl.start_program()
        self.assertTrue(body(session.send("feed_hold"), "cmd/feed_hold/result")["executed"])
        self.assertEqual(session.state(), "HOLDING")
        self.assertTrue(body(session.send("cycle_start_resume"), "cmd/cycle_start_resume/result")["allowed"])
        self.assertEqual(session.state(), "RUNNING")

    def test_the_door_opening_during_a_hold_refuses_the_resume_until_it_closes(self) -> None:
        session = JobSession()
        session.grbl.start_program()
        session.send("feed_hold")
        session.grbl.open_door()
        session.signals.door_closed = False
        refused = body(session.send("cycle_start_resume"), "cmd/cycle_start_resume/result")
        self.assertFalse(refused["allowed"])
        self.assertEqual(session.state(), "SAFE_STOP")
        session.grbl.close_door()
        session.signals.door_closed = True
        self.assertEqual(session.state(), "HOLDING")
        resumed = body(session.send("cycle_start_resume"), "cmd/cycle_start_resume/result")
        self.assertTrue(resumed["allowed"] and resumed["executed"])
        self.assertEqual(session.state(), "RUNNING")

    def test_a_latched_alarm_reads_as_a_fault_refuses_jobs_and_survives_a_soft_reset(self) -> None:
        session = JobSession()
        session.grbl.start_program()
        session.grbl.trigger_alarm(1)
        self.assertEqual(session.state(), "FAULT")
        self.assertFalse(session.job()["allowed"])
        session.send("soft_reset")
        self.assertEqual(session.state(), "FAULT")  # a real GRBL keeps the alarm until it is unlocked

    def test_after_the_unlock_the_machine_reads_idle_and_takes_a_job_again(self) -> None:
        session = JobSession()
        session.grbl.trigger_alarm(1)
        self.assertEqual(session.state(), "FAULT")
        session.grbl.write(b"$X\n")
        while session.grbl.readline():
            pass
        self.assertEqual(session.state(), "IDLE")
        self.assertTrue(session.job()["allowed"])

    def test_the_estop_input_wins_over_an_idle_controller(self) -> None:
        session = JobSession()
        session.signals.estop = True
        self.assertEqual(session.state(), "SAFE_STOP")
        self.assertFalse(session.job()["allowed"])
        session.signals.estop = False
        self.assertEqual(session.state(), "IDLE")

    def test_a_cell_that_is_not_ready_refuses_an_idle_machine(self) -> None:
        session = JobSession()
        session.signals.cell_state = CellState.SAFE_STOP
        self.assertFalse(session.job()["allowed"])

    def test_a_repeated_job_key_is_answered_from_the_store_and_run_once(self) -> None:
        session = JobSession()
        first = session.job()
        session.number -= 1  # the same idempotency key again
        again = session.job()
        self.assertTrue(first["allowed"])
        self.assertEqual(again.get("allowed"), first["allowed"])

    def test_a_retained_command_is_never_executed_on_a_reconnect(self) -> None:
        session = JobSession()
        session.grbl.start_program()
        self.assertEqual(session.bridge.handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", b"", retained=True), [])
        self.assertEqual(session.state(), "RUNNING")


if __name__ == "__main__":
    unittest.main()
