# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Status replies that line up, a serial link that fails closed, honest answers and the stop lane
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""GRBL talks unasked, a USB cable can disappear, and a client of the broker can send anything: none of it may leave this bridge believing an old state or end its loop."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState, job_to_dict
from hydra_umc_sdk.job_intake import IdempotencyStore

from hydra_umc_bridge_cnc import (
    CncMqttBridge,
    GrblRealtimeControl,
    GrblSerialProbe,
    ReconnectingSerialConnection,
    open_serial_port_by_id,
)
from hydra_umc_bridge_cnc.mqtt_transport import AVAILABILITY_TOPIC, MAX_PAYLOAD_BYTES, TOPIC_PREFIX, MessageDispatcher
from hydra_umc_bridge_cnc.serial_transport import open_serial_port


class ScriptedSerial:
    """A GRBL that talks when it likes: `lines` is what is waiting on the wire, oldest first."""

    def __init__(self, lines=()):
        self.lines = deque(lines)
        self.written = []
        self.input_resets = 0

    def write(self, data):
        self.written.append(data)
        return len(data)

    def readline(self):
        return self.lines.popleft() if self.lines else b""

    def reset_input_buffer(self):
        self.input_resets += 1
        self.lines.clear()

    @property
    def in_waiting(self):
        return len(self.lines)

    def close(self):
        return None


def query(connection):
    return GrblSerialProbe().query_status(connection, estop=lambda: False, door_closed=lambda: True)


class QueryAlignmentTests(unittest.TestCase):
    def test_the_banner_and_the_unasked_lines_are_not_taken_for_the_answer(self):
        # the lines already waiting are thrown away by a real port; here they sit between the "?" and the answer
        class Chatty(ScriptedSerial):
            def write(self, data):
                super().write(data)
                self.lines.extend([b"Grbl 1.1h ['$' for help]\r\n", b"ok\r\n", b"[MSG:Caution: Unlocked]\r\n", b"ALARM:1\r\n", b"<Run|MPos:0,0,0|FS:500,0>\r\n"])
                return len(data)

            def reset_input_buffer(self):
                super().reset_input_buffer()

        snapshot = query(Chatty())
        self.assertEqual(snapshot.controller_state, "Run")
        self.assertEqual(snapshot.machine_state(), MachineState.RUNNING)

    def test_a_status_that_was_already_waiting_does_not_make_every_later_query_one_reply_late(self):
        # the late answer to an earlier "?" arrives after the buffer was cleared: two reports are waiting, the newest is the truth
        class LateAnswer(ScriptedSerial):
            def write(self, data):
                super().write(data)
                self.lines.extend([b"<Idle|MPos:0,0,0>\r\n", b"<Run|MPos:1,0,0>\r\n"])
                return len(data)

        self.assertEqual(query(LateAnswer()).controller_state, "Run")

    def test_the_pending_input_is_discarded_before_the_question_is_asked(self):
        connection = ScriptedSerial([b"<Idle|MPos:0,0,0>\r\n"])  # stale: sent in answer to a "?" that timed out
        snapshot = query(connection)
        self.assertEqual(connection.input_resets, 1)
        self.assertEqual(snapshot.controller_state, "", "the stale report was thrown away and nothing answered this query: the machine is not known")
        self.assertEqual(snapshot.machine_state(), MachineState.OFFLINE)

    def test_silence_is_offline_and_does_not_spin(self):
        connection = ScriptedSerial()
        self.assertEqual(query(connection).machine_state(), MachineState.OFFLINE)
        self.assertEqual(connection.written, [b"?"])

    def test_lines_that_never_become_a_report_give_up(self):
        class Noise(ScriptedSerial):
            def readline(self):
                return b"ok\r\n"

        self.assertEqual(query(Noise()).machine_state(), MachineState.OFFLINE)


class ReconnectFailureTests(unittest.TestCase):
    def dead_link(self):
        class Dead:
            def write(self, data):
                raise OSError("device disconnected")

            def readline(self):
                raise OSError("device disconnected")

            def close(self):
                return None

        def connect():
            if connect.calls == 0:
                connect.calls += 1
                return Dead()
            return open_serial_port_by_id("usb-XYZ", list_ports=lambda: [])  # raises RuntimeError: nothing matches

        connect.calls = 0
        return ReconnectingSerialConnection(connect)

    def test_a_missing_device_surfaces_as_an_oserror_not_a_runtimeerror(self):
        with self.assertRaises(OSError):
            self.dead_link().write(b"!")

    def test_a_status_query_on_a_dead_link_fails_closed(self):
        snapshot = query(self.dead_link())
        self.assertEqual(snapshot.machine_state(), MachineState.SAFE_STOP)

    def test_a_feed_hold_on_a_dead_link_is_a_result_not_a_crash(self):
        result = GrblRealtimeControl().feed_hold(self.dead_link())
        self.assertFalse(result.executed)
        bridge = CncMqttBridge(self.dead_link(), lambda: False, lambda: True, lambda: CellState.READY)
        publishes = bridge.handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", b"")
        self.assertFalse(json.loads(publishes[0].payload)["executed"])

    def test_two_threads_that_see_the_same_dead_port_reopen_it_once(self):
        opened = []

        class Port:
            def __init__(self, working):
                self.working = working

            def write(self, data):
                if not self.working:
                    raise OSError("gone")
                return len(data)

            def readline(self):
                return b""

            def close(self):
                return None

        def connect():
            opened.append(1)
            return Port(working=len(opened) > 1)

        link = ReconnectingSerialConnection(connect)
        results = []
        threads = [threading.Thread(target=lambda: results.append(link.write(b"!"))) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [1, 1, 1, 1])
        self.assertLessEqual(len(opened), 2 + 3)  # the first open, then one reopen per thread that raced before the swap at most


class OpenPortTests(unittest.TestCase):
    def test_the_port_is_opened_with_the_control_lines_already_declared(self):
        events = []

        class FakeSerialPort:
            def __init__(self, *args, **kwargs):
                events.append(("init", args, kwargs))

            def __setattr__(self, name, value):
                events.append(("set", name, value))
                object.__setattr__(self, name, value)

            def open(self):
                events.append(("open",))

        fake = types.ModuleType("serial")
        fake.Serial = FakeSerialPort
        with mock.patch.dict(sys.modules, {"serial": fake}):
            open_serial_port("/dev/ttyACM0", baud=115200, timeout_seconds=0.5)
        names = [event[1] if event[0] == "set" else event[0] for event in events]
        self.assertEqual(names[0], "init")
        self.assertEqual(names[-1], "open")
        self.assertIn(("set", "dtr", False), events)
        self.assertLess(events.index(("set", "dtr", False)), events.index(("open",)))
        self.assertIn(("set", "port", "/dev/ttyACM0"), events)


class FeedHoldConfirmationTests(unittest.TestCase):
    def hold(self, status):
        connection = ScriptedSerial()

        class Answering(ScriptedSerial):
            def write(self, data):
                super().write(data)
                if data == b"?":
                    self.lines.append(status)
                return len(data)

        connection = Answering()
        bridge = CncMqttBridge(connection, lambda: False, lambda: True, lambda: CellState.READY)
        return json.loads(bridge.handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", b"")[0].payload)

    def test_a_hold_the_controller_reports_is_confirmed(self):
        result = self.hold(b"<Hold:0|MPos:0,0,0>\r\n")
        self.assertTrue(result["executed"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["controller_state_after"], "Hold:0")

    def test_a_byte_that_did_nothing_is_not_confirmed(self):
        result = self.hold(b"<Run|MPos:0,0,0>\r\n")
        self.assertTrue(result["executed"])
        self.assertFalse(result["confirmed"])


def job_payload(key="k1", phase=JobPhase.LOAD):
    return json.dumps(job_to_dict(BridgeJob("job-1", key, "orchestrator", phase, MachineState.IDLE, {}))).encode("utf-8")


def idle_bridge(store=None, clock=lambda: 50.0):
    class Idle(ScriptedSerial):
        def write(self, data):
            super().write(data)
            if data == b"?":
                self.lines.append(b"<Idle|MPos:0,0,0>\r\n")
            return len(data)

    return CncMqttBridge(Idle(), lambda: False, lambda: True, lambda: CellState.READY, store, clock=clock)


class HostilePayloadTests(unittest.TestCase):
    def test_a_deeply_nested_json_is_a_refusal_not_an_exception(self):
        publishes = idle_bridge().handle_message(f"{TOPIC_PREFIX}cmd/job", b"[" * 100_000)
        self.assertEqual(len(publishes), 1)
        self.assertFalse(json.loads(publishes[0].payload)["allowed"])

    def test_a_payload_over_the_limit_is_refused(self):
        publishes = idle_bridge().handle_message(f"{TOPIC_PREFIX}cmd/job", b" " * (MAX_PAYLOAD_BYTES + 1))
        self.assertIn("larger than", json.loads(publishes[0].payload)["reason"])

    def test_a_failure_while_gating_is_a_refusal_and_leaves_an_honest_answer_for_the_key(self):
        def broken():
            raise RuntimeError("cell state source down")

        store = IdempotencyStore()
        bridge = CncMqttBridge(ScriptedSerial(), lambda: False, lambda: True, broken, store)
        first = bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", job_payload())[0]
        again = bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", job_payload())[0]
        self.assertFalse(json.loads(first.payload)["allowed"])
        self.assertEqual(again.payload, first.payload)

    def test_a_failure_in_a_stop_command_is_a_refusal_on_its_result_topic(self):
        class Broken(ScriptedSerial):
            def write(self, data):
                raise ValueError("odd failure")

        publishes = CncMqttBridge(Broken(), lambda: False, lambda: True, lambda: CellState.READY).handle_message(f"{TOPIC_PREFIX}cmd/soft_reset", b"")
        self.assertFalse(json.loads(publishes[0].payload)["executed"])


class AbortAnswerTests(unittest.TestCase):
    def test_an_abort_says_that_nothing_was_stopped(self):
        result = json.loads(idle_bridge().handle_message(f"{TOPIC_PREFIX}cmd/job", job_payload(phase=JobPhase.ABORT))[0].payload)
        self.assertIs(result["forwarded"], False)
        self.assertEqual(result["mode"], "gate-only")
        self.assertIn("nothing was stopped", result["reason"])


class StateTests(unittest.TestCase):
    def test_the_state_carries_the_moment_it_was_observed(self):
        publish = idle_bridge(clock=lambda: 1234.5).handle_message(f"{TOPIC_PREFIX}cmd/status", b"")[0]
        self.assertEqual(json.loads(publish.payload)["observed_at"], 1234500)


class OneReaderAtATimeTests(unittest.TestCase):
    def test_two_status_queries_never_share_the_port(self):
        inside = {"now": 0, "max": 0}
        guard = threading.Lock()

        class Slow(ScriptedSerial):
            def write(self, data):
                with guard:
                    inside["now"] += 1
                    inside["max"] = max(inside["max"], inside["now"])
                return super().write(data)

            def readline(self):
                threading.Event().wait(0.002)
                with guard:
                    inside["now"] -= 1
                return b"<Idle|MPos:0,0,0>\r\n"

        bridge = CncMqttBridge(Slow(), lambda: False, lambda: True, lambda: CellState.READY)
        threads = [threading.Thread(target=lambda: [bridge.refresh_status() for _ in range(5)]) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(inside["max"], 1)


class DispatcherTests(unittest.TestCase):
    def test_a_stop_is_not_held_up_by_a_slow_status_query(self):
        release = threading.Event()
        started = threading.Event()
        answered = threading.Event()

        class Bridge:
            def handle_message(self, topic, payload, *, retained=False):
                if topic.endswith("cmd/status"):
                    started.set()
                    release.wait(5)
                else:
                    answered.set()
                return []

        dispatcher = MessageDispatcher(Bridge())
        try:
            dispatcher.on_message(mock.MagicMock(), None, mock.MagicMock(topic=f"{TOPIC_PREFIX}cmd/status", payload=b"", retain=False))
            self.assertTrue(started.wait(2))
            dispatcher.on_message(mock.MagicMock(), None, mock.MagicMock(topic=f"{TOPIC_PREFIX}cmd/feed_hold", payload=b"", retain=False))
            self.assertTrue(answered.wait(2), "the feed hold waited behind the slow status query")
        finally:
            release.set()
            dispatcher.close()

    def test_the_answers_are_published_and_a_retained_message_is_never_queued(self):
        client = mock.MagicMock()
        dispatcher = MessageDispatcher(idle_bridge())
        dispatcher.on_message(client, None, mock.MagicMock(topic=f"{TOPIC_PREFIX}cmd/status", payload=b"", retain=False))
        dispatcher.on_message(client, None, mock.MagicMock(topic=f"{TOPIC_PREFIX}cmd/status", payload=b"", retain=True))
        dispatcher.close()
        self.assertEqual(client.publish.call_count, 1)


class InspectToolTests(unittest.TestCase):
    def test_a_failed_inspection_reports_the_canonical_state_value(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "evidence.json"
            evidence.write_text("{not json", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(root / "tools" / "inspect_controller_evidence.py"), str(evidence)],
                capture_output=True, text=True, env=os.environ.copy(),
            )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["machine_state"], "OFFLINE")


class FakeClient:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.will = None
        self.published = []
        self.subscribed = []
        self.reconnect = None
        FakeClient.instances.append(self)

    def username_pw_set(self, *args, **kwargs):
        pass

    def tls_set(self, *args, **kwargs):
        pass

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.reconnect = (min_delay, max_delay)

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def connect(self, *args, **kwargs):
        pass

    def subscribe(self, topic, *args, **kwargs):
        self.subscribed.append(topic)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def loop_forever(self):
        return None


class RunForeverTests(unittest.TestCase):
    def run_it(self):
        paho = types.ModuleType("paho")
        mqtt_package = types.ModuleType("paho.mqtt")
        client_module = types.ModuleType("paho.mqtt.client")
        client_module.Client = FakeClient
        client_module.CallbackAPIVersion = types.SimpleNamespace(VERSION2="v2")
        paho.mqtt = mqtt_package
        mqtt_package.client = client_module
        modules = {"paho": paho, "paho.mqtt": mqtt_package, "paho.mqtt.client": client_module}
        from hydra_umc_bridge_cnc import run_forever

        FakeClient.instances.clear()
        with mock.patch.dict(sys.modules, modules):
            run_forever(idle_bridge(), "127.0.0.1")
        return FakeClient.instances[-1]

    def test_the_client_uses_the_version_2_callbacks(self):
        self.assertEqual(self.run_it().args[0], "v2")

    def test_a_last_will_marks_the_bridge_offline_and_connecting_marks_it_online(self):
        client = self.run_it()
        self.assertEqual(client.will, (AVAILABILITY_TOPIC, "offline", 1, True))
        client.on_connect(client, None, {}, types.SimpleNamespace(is_failure=False), None)
        self.assertIn((AVAILABILITY_TOPIC, "online", 1, True), client.published)
        self.assertEqual(client.subscribed, [f"{TOPIC_PREFIX}cmd/#"])
        self.assertEqual(client.reconnect, (1, 30))

    def test_a_refused_connection_does_not_announce_the_bridge_online(self):
        client = self.run_it()
        client.on_connect(client, None, {}, types.SimpleNamespace(is_failure=True), None)
        self.assertEqual(client.published, [])


if __name__ == "__main__":
    unittest.main()
