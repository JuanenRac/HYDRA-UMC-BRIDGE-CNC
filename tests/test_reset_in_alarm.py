# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Tests of the gated reset in alarm
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""`reset_in_alarm = "gated"`: a soft reset that would clear an alarm waits for a READY cell and no global stop; a reset while running is never held back."""

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
    def test_the_default_sends_the_reset_even_in_alarm(self) -> None:
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
        self.assertEqual(parse_config({"controller": {"kind": "linuxcnc"}}).reset_in_alarm, "allow")
        self.assertEqual(parse_config({"controller": {"kind": "linuxcnc"}, "gate": {"reset_in_alarm": "gated"}}).reset_in_alarm, "gated")
        with self.assertRaisesRegex(ConfigError, "reset_in_alarm"):
            parse_config({"controller": {"kind": "linuxcnc"}, "gate": {"reset_in_alarm": "maybe"}})
        with self.assertRaises(ValueError):
            CncMqttBridge(Serial(RUN), lambda: False, lambda: True, lambda: CellState.READY, reset_in_alarm="maybe")


if __name__ == "__main__":
    unittest.main()
