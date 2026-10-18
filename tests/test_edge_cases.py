# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Payload edge cases, stop-lane routing and the reconnecting serial link
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""What a client of the broker can put in a payload, which lane a message takes, and how the serial link behaves when the cable goes away."""

import json
import unittest
from collections import deque

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState, job_to_dict

from hydra_umc_bridge_cnc import CncMqttBridge, GrblRealtimeControl, ReconnectingSerialConnection
from hydra_umc_bridge_cnc.mqtt_transport import MAX_PAYLOAD_BYTES, STOP_VERBS, TOPIC_PREFIX
from hydra_umc_bridge_cnc.serial_transport import resolve_serial_device_path


class Wire:
    """A GRBL that answers "?" with an idle report and records every byte written."""

    def __init__(self, status=b"<Idle|MPos:0,0,0>\r\n"):
        self.status = status
        self.written = []
        self.lines = deque()

    def write(self, data):
        self.written.append(data)
        if data == b"?":
            self.lines.append(self.status)
        return len(data)

    def readline(self):
        return self.lines.popleft() if self.lines else b""

    def close(self):
        return None


def bridge(wire=None):
    return CncMqttBridge(wire or Wire(), lambda: False, lambda: True, lambda: CellState.READY)


def reason(publishes):
    return json.loads(publishes[0].payload)["reason"]


class JobPayloadTests(unittest.TestCase):
    def test_a_payload_that_is_not_text_is_a_refusal(self):
        publishes = bridge().handle_message(f"{TOPIC_PREFIX}cmd/job", b"\xff\xfe\x00\x80")
        self.assertEqual(len(publishes), 1)
        self.assertFalse(json.loads(publishes[0].payload)["allowed"])

    def test_json_that_is_not_an_object_is_a_refusal_for_every_shape(self):
        for payload in (b"null", b"[]", b"[1,2]", b"7", b'"text"', b"true", b"", b"{"):
            publishes = bridge().handle_message(f"{TOPIC_PREFIX}cmd/job", payload)
            self.assertEqual(len(publishes), 1, payload)
            self.assertFalse(json.loads(publishes[0].payload)["allowed"], payload)

    def test_fields_of_the_wrong_type_are_a_refusal(self):
        good = job_to_dict(BridgeJob("job-1", "key-1", "orchestrator", JobPhase.LOAD, MachineState.IDLE, {}))
        for field, value in (("phase", 5), ("phase", None), ("phase", ["load"]), ("job_id", {}), ("idempotency_key", 3), ("parameters", [])):
            bad = dict(good)
            bad[field] = value
            publishes = bridge().handle_message(f"{TOPIC_PREFIX}cmd/job", json.dumps(bad).encode())
            self.assertEqual(len(publishes), 1, (field, value))
            self.assertFalse(json.loads(publishes[0].payload)["allowed"], (field, value))

    def test_the_size_limit_is_exact(self):
        at_limit = bridge().handle_message(f"{TOPIC_PREFIX}cmd/job", b" " * MAX_PAYLOAD_BYTES)
        self.assertNotIn("larger than", reason(at_limit))
        over = bridge().handle_message(f"{TOPIC_PREFIX}cmd/job", b" " * (MAX_PAYLOAD_BYTES + 1))
        self.assertIn("larger than", reason(over))

    def test_an_oversized_payload_on_a_stop_verb_is_refused_on_its_own_result_topic_without_sending_a_byte(self):
        wire = Wire()
        publishes = bridge(wire).handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", b"x" * (MAX_PAYLOAD_BYTES + 1))
        self.assertEqual(publishes[0].topic, f"{TOPIC_PREFIX}cmd/feed_hold/result")
        self.assertEqual(wire.written, [])


class TopicTests(unittest.TestCase):
    def test_topics_outside_the_prefix_or_not_a_command_get_no_answer(self):
        b = bridge()
        for topic in ("hydra/bridges/other/cmd/job", "cmd/job", "", f"{TOPIC_PREFIX}state", f"{TOPIC_PREFIX}availability"):
            self.assertEqual(b.handle_message(topic, b"{}"), [], topic)

    def test_an_unknown_verb_with_a_bad_shape_is_ignored_and_one_that_is_oversized_is_not_answered_on_a_made_up_topic(self):
        b = bridge()
        self.assertEqual(b.handle_message(f"{TOPIC_PREFIX}cmd/unknown", b"{}"), [])
        self.assertEqual(b.handle_message(f"{TOPIC_PREFIX}cmd/a/b/c", b"x" * (MAX_PAYLOAD_BYTES + 1)), [])
        self.assertEqual(b.handle_message(f"{TOPIC_PREFIX}notcmd/job", b"x" * (MAX_PAYLOAD_BYTES + 1)), [])

    def test_a_retained_message_never_sends_a_byte_for_any_verb(self):
        wire = Wire()
        b = bridge(wire)
        for verb in ("cmd/feed_hold", "cmd/soft_reset", "cmd/cycle_start_resume", "cmd/status", "cmd/job"):
            self.assertEqual(b.handle_message(f"{TOPIC_PREFIX}{verb}", b"{}", retained=True), [], verb)
        self.assertEqual(wire.written, [])

    def test_the_stop_verbs_are_the_two_that_must_never_queue_behind_a_status_query(self):
        self.assertEqual(STOP_VERBS, frozenset({"cmd/feed_hold", "cmd/soft_reset"}))


class FeedHoldConfirmationTests(unittest.TestCase):
    def test_a_hold_whose_confirmation_cannot_be_read_still_reports_that_the_byte_was_sent(self):
        class Dies(Wire):
            def readline(self):
                raise OSError("cable pulled")

        publishes = bridge(Dies()).handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", b"")
        answer = json.loads(publishes[0].payload)
        self.assertIs(answer["executed"], True)
        self.assertIs(answer["confirmed"], False)

    def test_a_machine_that_reports_hold_is_confirmed(self):
        wire = Wire(status=b"<Hold:0|MPos:0,0,0>\r\n")
        answer = json.loads(bridge(wire).handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", b"")[0].payload)
        self.assertEqual(wire.written[0], b"!")
        self.assertIs(answer["confirmed"], True)

    def test_a_machine_that_keeps_running_is_not_confirmed(self):
        wire = Wire(status=b"<Run|MPos:0,0,0>\r\n")
        answer = json.loads(bridge(wire).handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", b"")[0].payload)
        self.assertIs(answer["confirmed"], False)
        self.assertEqual(answer["controller_state_after"], "Run")


class RealtimeBytesTests(unittest.TestCase):
    def test_a_short_write_is_not_taken_for_a_stop(self):
        class Short(Wire):
            def write(self, data):
                return 0

        result = GrblRealtimeControl().feed_hold(Short())
        self.assertFalse(result.executed)


class ReconnectingLinkTests(unittest.TestCase):
    def test_a_failed_write_reopens_the_port_once_and_retries(self):
        opened = []

        class Dead(Wire):
            def write(self, data):
                raise OSError("gone")

        def connect():
            opened.append(1)
            return Dead() if len(opened) == 1 else Wire()

        link = ReconnectingSerialConnection(connect)
        self.assertEqual(link.write(b"!"), 1)
        self.assertEqual(len(opened), 2)

    def test_a_port_that_cannot_be_reopened_comes_out_as_an_oserror_even_when_the_opener_raises_something_else(self):
        def connect():
            raise RuntimeError("no such device")

        with self.assertRaises(OSError):
            ReconnectingSerialConnection(connect).write(b"!")

    def test_close_closes_once_and_reading_after_close_opens_a_new_port(self):
        closed = []

        class Port(Wire):
            def close(self):
                closed.append(1)

        link = ReconnectingSerialConnection(Port)
        link.write(b"?")
        link.close()
        link.close()
        self.assertEqual(len(closed), 1)
        link.write(b"?")

    def test_in_waiting_and_reset_input_buffer_tolerate_a_port_without_them(self):
        link = ReconnectingSerialConnection(Wire)
        self.assertEqual(link.in_waiting, 0)
        link.reset_input_buffer()
        self.assertEqual(link.in_waiting, 0)

    def test_a_failure_while_closing_a_dead_port_does_not_mask_the_reconnect(self):
        count = []

        class Dead(Wire):
            def write(self, data):
                raise OSError("gone")

            def close(self):
                raise OSError("already dead")

        def connect():
            count.append(1)
            return Dead() if len(count) == 1 else Wire()

        self.assertEqual(ReconnectingSerialConnection(connect).write(b"!"), 1)


class DeviceResolutionTests(unittest.TestCase):
    class Port:
        def __init__(self, device, hwid="", serial_number=None):
            self.device, self.hwid, self.serial_number = device, hwid, serial_number

    def test_a_serial_number_or_a_hwid_fragment_finds_the_current_port_name(self):
        ports = lambda: [self.Port("COM3", "USB VID:PID=1A86:7523 SER=ABC"), self.Port("COM9", "USB VID:PID=0403:6001", "FT123")]
        self.assertEqual(resolve_serial_device_path("FT123", list_ports=ports), "COM9")
        self.assertEqual(resolve_serial_device_path("SER=ABC", list_ports=ports), "COM3")

    def test_an_id_nobody_has_is_an_error_not_a_guess(self):
        with self.assertRaises(RuntimeError):
            resolve_serial_device_path("nothing", list_ports=lambda: [self.Port("COM3", "x")])
        with self.assertRaises(RuntimeError):
            resolve_serial_device_path("nothing", list_ports=lambda: [])


if __name__ == "__main__":
    unittest.main()
