# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Tests of the gated reset in alarm
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""`reset_in_alarm`: with `separate` (the default) the soft reset only stops and `cmd/reset_rearm` clears an alarm; `allow` and `gated` keep one topic for both."""

import json
import unittest

from hydra_umc_sdk.bridge_contract import CellState
from hydra_umc_sdk.global_stop import GlobalStopSettings

from hydra_umc_bridge_cnc import CncMqttBridge
from hydra_umc_bridge_cnc.mqtt_transport import TOPIC_PREFIX
from hydra_umc_bridge_cnc.service import ConfigError, parse_config

RESET = b"\x18"
SAFE_STOP = json.dumps({"schema_version": "1.0", "state": "SAFE_STOP", "source": "hydra-umc-server"}).encode()


class Serial:
    def __init__(self, status: bytes) -> None:
        self.status = status
        self.sent: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.sent.append(data)
        return len(data)

    def readline(self) -> bytes:
        return self.status

    def close(self) -> None:
        pass


def reset(status: bytes, cell: CellState = CellState.READY, mode: str = "gated", stop: bool = False):
    serial = Serial(status)
    bridge = CncMqttBridge(serial, lambda: False, lambda: True, lambda: cell, global_stop=GlobalStopSettings(stop), reset_in_alarm=mode)
    if stop:
        bridge.global_stop.observe(SAFE_STOP)
    out = bridge.handle_message(f"{TOPIC_PREFIX}cmd/soft_reset", b"")
    answer = json.loads(next(p.payload for p in out if p.topic.endswith("cmd/soft_reset/result")))
    return answer, serial


ALARM = b"<Alarm|MPos:0.000,0.000,0.000|FS:0,0>\n"
RUN = b"<Run|MPos:1.000,0.000,0.000|FS:500,0>\n"


class ResetInAlarmTests(unittest.TestCase):
    def test_allow_sends_the_reset_even_in_alarm(self) -> None:
        answer, serial = reset(ALARM, cell=CellState.INHIBITED, mode="allow")
        self.assertTrue(answer["executed"])
        self.assertIn(RESET, serial.sent)

    def test_gated_holds_the_reset_of_an_alarm_while_the_cell_is_not_ready(self) -> None:
        answer, serial = reset(ALARM, cell=CellState.INHIBITED)
        self.assertFalse(answer["executed"])
        self.assertIn("INHIBITED", answer["reason"])
        self.assertNotIn(RESET, serial.sent)

    def test_gated_lets_the_reset_of_an_alarm_through_when_the_cell_is_ready(self) -> None:
        answer, serial = reset(ALARM, cell=CellState.READY)
        self.assertTrue(answer["executed"])
        self.assertIn(RESET, serial.sent)

    def test_gated_holds_the_reset_of_an_alarm_during_a_global_stop(self) -> None:
        answer, serial = reset(ALARM, cell=CellState.READY, stop=True)
        self.assertFalse(answer["executed"])
        self.assertIn("global stop", answer["reason"])
        self.assertNotIn(RESET, serial.sent)

    def test_a_reset_while_running_is_a_stop_and_is_never_held(self) -> None:
        answer, serial = reset(RUN, cell=CellState.INHIBITED)
        self.assertTrue(answer["executed"])
        self.assertIn(RESET, serial.sent)

    def test_a_status_that_cannot_be_read_does_not_delay_the_reset(self) -> None:
        answer, serial = reset(b"", cell=CellState.INHIBITED)
        self.assertTrue(answer["executed"])

    def test_the_setting_is_checked_in_the_configuration(self) -> None:
        self.assertEqual(parse_config({"controller": {"kind": "linuxcnc"}}).reset_in_alarm, "separate")
        self.assertEqual(parse_config({"controller": {"kind": "linuxcnc"}, "gate": {"reset_in_alarm": "gated"}}).reset_in_alarm, "gated")
        with self.assertRaisesRegex(ConfigError, "reset_in_alarm"):
            parse_config({"controller": {"kind": "linuxcnc"}, "gate": {"reset_in_alarm": "maybe"}})
        with self.assertRaises(ValueError):
            CncMqttBridge(Serial(RUN), lambda: False, lambda: True, lambda: CellState.READY, reset_in_alarm="maybe")


def rearm(status: bytes, payload: bytes = b'{"confirm": "REARM"}', cell: CellState = CellState.READY, estop: bool = False, door: bool = True, stop: bool = False):
    serial = Serial(status)
    bridge = CncMqttBridge(serial, lambda: estop, lambda: door, lambda: cell, global_stop=GlobalStopSettings(stop))
    if stop:
        bridge.global_stop.observe(SAFE_STOP)
    (out,) = bridge.handle_message(f"{TOPIC_PREFIX}cmd/reset_rearm", payload)
    return json.loads(out.payload), serial


DOOR = b"<Door:0|MPos:0.000,0.000,0.000|FS:0,0>\n"


class SeparateResetTests(unittest.TestCase):
    def test_the_soft_reset_never_clears_an_alarm_or_a_door(self) -> None:
        for status in (ALARM, DOOR):
            with self.subTest(status=status):
                answer, serial = reset(status, mode="separate")
                self.assertFalse(answer["executed"])
                self.assertTrue(answer["already_stopped"])
                self.assertIn("cmd/reset_rearm", answer["reason"])
                self.assertNotIn(RESET, serial.sent)

    def test_the_soft_reset_of_a_running_or_unreadable_machine_is_sent_whatever_the_cell_says(self) -> None:
        for status in (RUN, b""):
            with self.subTest(status=status):
                answer, serial = reset(status, cell=CellState.INHIBITED, mode="separate", stop=True)
                self.assertTrue(answer["executed"])
                self.assertEqual(serial.sent[0], b"!")
                self.assertIn(RESET, serial.sent)

    def test_the_rearm_clears_a_latched_state_when_everything_allows_it(self) -> None:
        for status in (ALARM, DOOR):
            with self.subTest(status=status):
                answer, serial = rearm(status)
                self.assertTrue(answer["executed"], answer)
                self.assertIn(RESET, serial.sent)

    def test_the_rearm_is_refused_when_anything_is_missing(self) -> None:
        cases = {
            "no payload": {"payload": b""},
            "wrong word": {"payload": b'{"confirm": "yes"}'},
            "not an object": {"payload": b'["REARM"]'},
            "cell": {"cell": CellState.INHIBITED},
            "estop": {"estop": True},
            "door": {"door": False},
            "global stop": {"stop": True},
        }
        for name, case in cases.items():
            with self.subTest(case=name):
                answer, serial = rearm(ALARM, **case)
                self.assertFalse(answer["executed"], answer)
                self.assertNotIn(RESET, serial.sent)

    def test_there_is_nothing_to_rearm_on_a_machine_that_runs_or_cannot_be_read(self) -> None:
        for status in (RUN, b""):
            with self.subTest(status=status):
                answer, serial = rearm(status)
                self.assertFalse(answer["executed"])
                self.assertNotIn(RESET, serial.sent)


if __name__ == "__main__":
    unittest.main()
