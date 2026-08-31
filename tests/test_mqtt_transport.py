# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Real MQTT transport tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Tests CncMqttBridge's real topic dispatch against an in-memory fake
serial connection - no real MQTT broker or paho-mqtt install required,
same "small Protocol + fake" pattern test_serial_transport.py already
uses for the serial side."""

import json
import unittest

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState, job_to_dict
from hydra_umc_bridge_cnc import CncMqttBridge
from hydra_umc_bridge_cnc.mqtt_transport import TOPIC_PREFIX


class FakeSerial:
    def __init__(self, response_line: bytes = b"<Idle|MPos:0,0,0>\n"):
        self.response_line = response_line
        self.written: list[bytes] = []

    def write(self, data: bytes):
        self.written.append(data)

    def readline(self) -> bytes:
        return self.response_line

    def close(self):
        pass


def bridge(response_line=b"<Idle|MPos:0,0,0>\n", estop=False, door_closed=True, cell_state=CellState.READY):
    connection = FakeSerial(response_line)
    b = CncMqttBridge(connection, lambda: estop, lambda: door_closed, lambda: cell_state)
    return b, connection


def job(phase=JobPhase.LOAD, machine_state=MachineState.IDLE):
    return BridgeJob("job-1", "key-1", "orchestrator", phase, machine_state, {})


class TopicRoutingTests(unittest.TestCase):
    def test_unknown_prefix_is_ignored(self):
        b, _ = bridge()
        self.assertEqual(b.handle_message("some/other/topic", b""), [])

    def test_unrecognised_cmd_topic_is_ignored_not_an_error(self):
        b, _ = bridge()
        self.assertEqual(b.handle_message(f"{TOPIC_PREFIX}cmd/does_not_exist", b""), [])


class StatusCommandTests(unittest.TestCase):
    def test_status_publishes_retained_state_with_derived_machine_state(self):
        b, connection = bridge(b"<Idle|MPos:0,0,0>\n")
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/status", b"")
        self.assertEqual(len(publishes), 1)
        publish = publishes[0]
        self.assertEqual(publish.topic, f"{TOPIC_PREFIX}state")
        self.assertTrue(publish.retain)
        payload = json.loads(publish.payload)
        self.assertEqual(payload["machine_state"], "IDLE")
        self.assertEqual(connection.written, [b"?"])


class RealtimeCommandTests(unittest.TestCase):
    def test_feed_hold_sends_the_real_grbl_byte_and_publishes_a_result(self):
        b, connection = bridge()
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", b"")
        self.assertEqual(connection.written, [b"!"])
        self.assertEqual(publishes[0].topic, f"{TOPIC_PREFIX}cmd/feed_hold/result")
        self.assertTrue(json.loads(publishes[0].payload)["executed"])

    def test_soft_reset_sends_the_real_grbl_byte(self):
        b, connection = bridge()
        b.handle_message(f"{TOPIC_PREFIX}cmd/soft_reset", b"")
        self.assertEqual(connection.written, [b"\x18"])

    def test_cycle_start_resume_uses_the_last_known_snapshot_without_a_fresh_query(self):
        b, connection = bridge(b"<Hold:0|MPos:0,0,0>\n")
        b.refresh_status()
        connection.written.clear()
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/cycle_start_resume", b"")
        self.assertEqual(connection.written, [b"~"])
        self.assertTrue(json.loads(publishes[0].payload)["allowed"])

    def test_cycle_start_resume_queries_status_if_none_cached_yet(self):
        b, connection = bridge(b"<Hold:0|MPos:0,0,0>\n")
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/cycle_start_resume", b"")
        # First byte written is the real status query, second is cycle start/resume.
        self.assertEqual(connection.written, [b"?", b"~"])
        self.assertTrue(json.loads(publishes[0].payload)["allowed"])

    def test_cycle_start_resume_rejects_an_idle_machine(self):
        b, connection = bridge(b"<Idle|MPos:0,0,0>\n")
        b.refresh_status()
        connection.written.clear()
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/cycle_start_resume", b"")
        self.assertFalse(json.loads(publishes[0].payload)["allowed"])
        self.assertEqual(connection.written, [])


class JobCommandTests(unittest.TestCase):
    def test_a_valid_job_against_a_ready_idle_cell_is_allowed(self):
        b, _ = bridge(b"<Idle|MPos:0,0,0>\n", cell_state=CellState.READY)
        payload = json.dumps(job_to_dict(job())).encode("utf-8")
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/job", payload)
        self.assertEqual(publishes[0].topic, f"{TOPIC_PREFIX}cmd/job/result")
        self.assertTrue(json.loads(publishes[0].payload)["allowed"])

    def test_a_job_against_a_running_cnc_is_rejected(self):
        b, _ = bridge(b"<Run|MPos:0,0,0>\n", cell_state=CellState.READY)
        payload = json.dumps(job_to_dict(job())).encode("utf-8")
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/job", payload)
        decision = json.loads(publishes[0].payload)
        self.assertFalse(decision["allowed"])
        self.assertIn("RUNNING", decision["reason"])

    def test_malformed_json_fails_closed_with_a_real_result_not_a_crash(self):
        b, _ = bridge()
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/job", b"{not valid json")
        self.assertEqual(len(publishes), 1)
        decision = json.loads(publishes[0].payload)
        self.assertFalse(decision["allowed"])
        self.assertIn("malformed job payload", decision["reason"])

    def test_missing_field_fails_closed_with_a_real_result_not_a_crash(self):
        b, _ = bridge()
        payload = job_to_dict(job())
        del payload["source"]
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/job", json.dumps(payload).encode("utf-8"))
        decision = json.loads(publishes[0].payload)
        self.assertFalse(decision["allowed"])
        self.assertIn("malformed job payload", decision["reason"])

    def test_abort_is_always_allowed_even_with_a_faulted_cnc(self):
        b, _ = bridge(b"<Alarm|MPos:0,0,0>\n", cell_state=CellState.FAULT)
        payload = json.dumps(job_to_dict(job(phase=JobPhase.ABORT, machine_state=MachineState.FAULT))).encode("utf-8")
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/job", payload)
        self.assertTrue(json.loads(publishes[0].payload)["allowed"])


class RunForeverTests(unittest.TestCase):
    def test_missing_paho_mqtt_raises_a_clear_runtime_error_not_an_import_error(self):
        try:
            import paho.mqtt.client  # noqa: F401

            self.skipTest("paho-mqtt is installed in this environment - nothing to prove here")
        except ImportError:
            pass
        from hydra_umc_bridge_cnc import run_forever

        b, _ = bridge()
        with self.assertRaises(RuntimeError) as context:
            run_forever(b, "127.0.0.1")
        self.assertIn("paho-mqtt is not installed", str(context.exception))


if __name__ == "__main__":
    unittest.main()
