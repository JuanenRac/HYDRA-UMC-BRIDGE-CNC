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
        # V07-019 (found in an independent revalidation audit, P2): a real
        # pyserial connection's write() returns the real byte count
        # actually written - GrblRealtimeControl._write() now requires
        # exactly that (see its own comment) instead of silently trusting
        # an unreported/non-conforming return value, so this fake must
        # report one too rather than relying on production to relax its
        # own real contract for it.
        return len(data)

    def readline(self) -> bytes:
        return self.response_line

    def close(self):
        pass


class LiveSignals:
    """A real, mutable stand-in for the estop/door_closed/cell_state
    callables CncMqttBridge reads live - lets a test flip the physical
    signal BETWEEN an earlier query and a later command, exactly the real
    race CNC-01 is about."""

    def __init__(self, estop=False, door_closed=True, cell_state=CellState.READY):
        self.estop = estop
        self.door_closed = door_closed
        self.cell_state = cell_state


def bridge(response_line=b"<Idle|MPos:0,0,0>\n", estop=False, door_closed=True, cell_state=CellState.READY):
    connection = FakeSerial(response_line)
    signals = LiveSignals(estop, door_closed, cell_state)
    b = CncMqttBridge(connection, lambda: signals.estop, lambda: signals.door_closed, lambda: signals.cell_state)
    return b, connection, signals


def job(phase=JobPhase.LOAD, machine_state=MachineState.IDLE):
    return BridgeJob("job-1", "key-1", "orchestrator", phase, machine_state, {})


class TopicRoutingTests(unittest.TestCase):
    def test_unknown_prefix_is_ignored(self):
        b, _, _signals = bridge()
        self.assertEqual(b.handle_message("some/other/topic", b""), [])

    def test_unrecognised_cmd_topic_is_ignored_not_an_error(self):
        b, _, _signals = bridge()
        self.assertEqual(b.handle_message(f"{TOPIC_PREFIX}cmd/does_not_exist", b""), [])


class StatusCommandTests(unittest.TestCase):
    def test_status_publishes_retained_state_with_derived_machine_state(self):
        b, connection, _signals = bridge(b"<Idle|MPos:0,0,0>\n")
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
        b, connection, _signals = bridge()
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", b"")
        self.assertEqual(connection.written, [b"!"])
        self.assertEqual(publishes[0].topic, f"{TOPIC_PREFIX}cmd/feed_hold/result")
        self.assertTrue(json.loads(publishes[0].payload)["executed"])

    def test_soft_reset_sends_the_real_grbl_byte(self):
        b, connection, _signals = bridge()
        b.handle_message(f"{TOPIC_PREFIX}cmd/soft_reset", b"")
        self.assertEqual(connection.written, [b"\x18"])

    def test_cycle_start_resume_always_queries_fresh_status_even_if_one_was_cached(self):
        # CNC-01 regression (found in an ecosystem-wide software-improvements
        # audit): this used to reuse whatever refresh_status() last saw
        # instead of re-checking live - even after an explicit earlier
        # refresh_status() call, handle_message() must still issue its OWN
        # real status query right before deciding.
        b, connection, _signals = bridge(b"<Hold:0|MPos:0,0,0>\n")
        b.refresh_status()
        connection.written.clear()
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/cycle_start_resume", b"")
        self.assertEqual(connection.written, [b"?", b"~"])  # real fresh query, THEN the real resume byte
        self.assertTrue(json.loads(publishes[0].payload)["allowed"])

    def test_cycle_start_resume_queries_status_if_none_cached_yet(self):
        b, connection, _signals = bridge(b"<Hold:0|MPos:0,0,0>\n")
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/cycle_start_resume", b"")
        # First byte written is the real status query, second is cycle start/resume.
        self.assertEqual(connection.written, [b"?", b"~"])
        self.assertTrue(json.loads(publishes[0].payload)["allowed"])

    def test_cycle_start_resume_rejects_an_idle_machine(self):
        b, connection, _signals = bridge(b"<Idle|MPos:0,0,0>\n")
        b.refresh_status()
        connection.written.clear()
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/cycle_start_resume", b"")
        self.assertFalse(json.loads(publishes[0].payload)["allowed"])
        self.assertEqual(connection.written, [b"?"])  # the fresh re-check itself, no resume byte

    def test_cycle_start_resume_refuses_a_real_door_open_between_refresh_and_order_regression_for_cnc_01(self):
        # The exact real reproduction from the audit: door closed + no
        # E-STOP at an earlier refresh, then the door opens (and E-STOP
        # activates) before cycle_start_resume actually arrives. The real
        # b"~" byte must never reach the serial port for this sequence.
        b, connection, signals = bridge(b"<Hold:0|MPos:0,0,0>\n", estop=False, door_closed=True)
        b.refresh_status()  # an earlier, now-stale poll while everything was safe
        connection.written.clear()
        signals.door_closed = False
        signals.estop = True
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/cycle_start_resume", b"")
        decision = json.loads(publishes[0].payload)
        self.assertFalse(decision["allowed"])
        self.assertNotIn(b"~", connection.written)

    def test_cycle_start_resume_refuses_a_real_door_open_during_the_blocking_status_read_regression_for_rev_005(self):
        # REV-005 (found in an independent revalidation audit) - a deeper
        # version of CNC-01 above: the real event happens WHILE
        # refresh_status()'s own readline() is blocked, not merely between
        # two separate messages. FakeSerial's readline() here flips the
        # live signals as a side effect, modelling the physical event
        # landing during that one blocking call - the real b"~" byte must
        # still never reach the serial port.
        connection = FakeSerial(b"<Hold:0|MPos:0,0,0>\n")
        signals = LiveSignals(estop=False, door_closed=True, cell_state=CellState.READY)
        original_readline = connection.readline

        def _readline_that_flips_signals():
            signals.door_closed = False
            signals.estop = True
            return original_readline()

        connection.readline = _readline_that_flips_signals
        b = CncMqttBridge(connection, lambda: signals.estop, lambda: signals.door_closed, lambda: signals.cell_state)

        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/cycle_start_resume", b"")
        decision = json.loads(publishes[0].payload)
        self.assertFalse(decision["allowed"])
        self.assertNotIn(b"~", connection.written)


class JobCommandTests(unittest.TestCase):
    def test_a_valid_job_against_a_ready_idle_cell_is_allowed(self):
        b, _, _signals = bridge(b"<Idle|MPos:0,0,0>\n", cell_state=CellState.READY)
        payload = json.dumps(job_to_dict(job())).encode("utf-8")
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/job", payload)
        self.assertEqual(publishes[0].topic, f"{TOPIC_PREFIX}cmd/job/result")
        self.assertTrue(json.loads(publishes[0].payload)["allowed"])

    def test_a_job_against_a_running_cnc_is_rejected(self):
        b, _, _signals = bridge(b"<Run|MPos:0,0,0>\n", cell_state=CellState.READY)
        payload = json.dumps(job_to_dict(job())).encode("utf-8")
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/job", payload)
        decision = json.loads(publishes[0].payload)
        self.assertFalse(decision["allowed"])
        self.assertIn("RUNNING", decision["reason"])

    def test_malformed_json_fails_closed_with_a_real_result_not_a_crash(self):
        b, _, _signals = bridge()
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/job", b"{not valid json")
        self.assertEqual(len(publishes), 1)
        decision = json.loads(publishes[0].payload)
        self.assertFalse(decision["allowed"])
        self.assertIn("malformed job payload", decision["reason"])

    def test_missing_field_fails_closed_with_a_real_result_not_a_crash(self):
        b, _, _signals = bridge()
        payload = job_to_dict(job())
        del payload["source"]
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/job", json.dumps(payload).encode("utf-8"))
        decision = json.loads(publishes[0].payload)
        self.assertFalse(decision["allowed"])
        self.assertIn("malformed job payload", decision["reason"])

    def test_abort_is_always_allowed_even_with_a_faulted_cnc(self):
        b, _, _signals = bridge(b"<Alarm|MPos:0,0,0>\n", cell_state=CellState.FAULT)
        payload = json.dumps(job_to_dict(job(phase=JobPhase.ABORT, machine_state=MachineState.FAULT))).encode("utf-8")
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/job", payload)
        self.assertTrue(json.loads(publishes[0].payload)["allowed"])

    def test_job_refuses_a_real_door_open_between_refresh_and_job_regression_for_cnc_01(self):
        # CNC-01 regression (found in an ecosystem-wide software-improvements
        # audit): _handle_job() had the exact same stale-snapshot gap as
        # cycle_start_resume - door closed + no E-STOP at an earlier poll,
        # then the door opens and E-STOP activates before a new cmd/job
        # arrives. The gate must see the CURRENT machine state, not the one
        # from the earlier poll.
        b, _, signals = bridge(b"<Idle|MPos:0,0,0>\n", cell_state=CellState.READY)
        b.refresh_status()  # an earlier, now-stale poll while everything was safe
        signals.door_closed = False
        signals.estop = True
        payload = json.dumps(job_to_dict(job())).encode("utf-8")
        publishes = b.handle_message(f"{TOPIC_PREFIX}cmd/job", payload)
        decision = json.loads(publishes[0].payload)
        self.assertFalse(decision["allowed"])


class RunForeverTests(unittest.TestCase):
    def test_missing_paho_mqtt_raises_a_clear_runtime_error_not_an_import_error(self):
        try:
            import paho.mqtt.client  # noqa: F401

            self.skipTest("paho-mqtt is installed in this environment - nothing to prove here")
        except ImportError:
            pass
        from hydra_umc_bridge_cnc import run_forever

        b, _, _signals = bridge()
        with self.assertRaises(RuntimeError) as context:
            run_forever(b, "127.0.0.1")
        self.assertIn("paho-mqtt is not installed", str(context.exception))


class ConnectWithRetryTests(unittest.TestCase):
    """connect_with_retry() is pure - no real paho-mqtt/broker needed to
    prove the real startup-race tolerance an ecosystem-wide software
    audit found missing here (this bridge's process used to die outright
    if it started before HYDRA-UMC-MQTT-BROKER was listening yet)."""

    def test_succeeds_on_the_first_try_without_sleeping(self):
        from hydra_umc_bridge_cnc import connect_with_retry

        sleeps: list[float] = []
        connect_with_retry(lambda: None, sleep=sleeps.append)
        self.assertEqual(sleeps, [])

    def test_retries_a_transient_connection_failure_then_succeeds(self):
        from hydra_umc_bridge_cnc import connect_with_retry

        attempts = {"n": 0}
        sleeps: list[float] = []

        def flaky_connect() -> None:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionRefusedError("broker not up yet")

        connect_with_retry(flaky_connect, max_attempts=5, retry_delay_seconds=1.5, sleep=sleeps.append)
        self.assertEqual(attempts["n"], 3)
        # 2 real failures -> 2 waits before the 3rd, successful attempt.
        self.assertEqual(sleeps, [1.5, 1.5])

    def test_gives_up_after_max_attempts_with_a_clear_error(self):
        from hydra_umc_bridge_cnc import connect_with_retry

        def always_fails() -> None:
            raise ConnectionRefusedError("broker still not up")

        with self.assertRaises(RuntimeError) as context:
            connect_with_retry(always_fails, max_attempts=3, retry_delay_seconds=0.01, sleep=lambda _: None)
        self.assertIn("after 3 attempts", str(context.exception))
        self.assertIn("broker still not up", str(context.exception))

    def test_a_non_os_error_is_never_retried(self):
        # Not every failure is a transient startup race - a real bug in
        # caller-supplied connect logic must surface immediately, not be
        # silently retried and masked as "broker unreachable".
        from hydra_umc_bridge_cnc import connect_with_retry

        def broken_connect() -> None:
            raise ValueError("not an OSError - a real bug, not a broker being down")

        with self.assertRaises(ValueError):
            connect_with_retry(broken_connect, sleep=lambda _: None)


if __name__ == "__main__":
    unittest.main()
