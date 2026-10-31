# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Global stop tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The bridge and the announcement of the global stop: off by default, SAFE_STOP refuses what moves, READY resumes, an unreadable state fails closed."""

import json
import os
import unittest
from unittest import mock

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState, job_to_dict

from hydra_umc_bridge_cnc import CncMqttBridge
from hydra_umc_bridge_cnc.global_stop import DEFAULT_TOPIC, GlobalStopGuard, GlobalStopSettings, settings_from_environment
from hydra_umc_bridge_cnc.mqtt_transport import TOPIC_PREFIX, MessageDispatcher

SAFE_STOP = json.dumps({"schema_version": "1.0", "state": "SAFE_STOP", "source": "hydra-umc-server"}).encode()
READY = json.dumps({"schema_version": "1.0", "state": "READY", "source": "hydra-umc-server"}).encode()


class HoldingSerial:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.sent.append(data)
        return len(data)

    def readline(self) -> bytes:
        return b"<Hold:0|MPos:0.000,0.000,0.000|FS:0,0>\n"

    def close(self) -> None:
        pass


def bridge_with(guard):
    serial = HoldingSerial()
    bridge = CncMqttBridge(serial, lambda: False, lambda: True, lambda: CellState.READY, global_stop=guard)
    return bridge, serial


def job(phase: JobPhase) -> bytes:
    return json.dumps(job_to_dict(BridgeJob("j-1", "key-" + phase.value, "orchestrator", phase, MachineState.IDLE, {}))).encode()


def ask(bridge: CncMqttBridge, verb: str, payload: bytes = b"") -> dict:
    out = bridge.handle_message(f"{TOPIC_PREFIX}cmd/{verb}", payload)
    results = [json.loads(p.payload) for p in out if p.topic.endswith(f"cmd/{verb}/result")]
    return results[0] if results else {}


class SettingsTests(unittest.TestCase):
    def test_the_default_is_off_and_the_topic_is_the_servers(self) -> None:
        settings = GlobalStopSettings()
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.strict)
        self.assertEqual(settings.topic, DEFAULT_TOPIC)
        self.assertEqual(DEFAULT_TOPIC, "hydra/swarm/safety/estop")

    def test_a_topic_with_wildcards_is_refused(self) -> None:
        for bad in ("", "hydra/#", "hydra/+/estop", "/hydra", "hydra/"):
            with self.subTest(topic=bad), self.assertRaises(ValueError):
                GlobalStopSettings(True, bad)

    def test_the_environment_switches_it_on(self) -> None:
        wanted = {"HYDRA_UMC_CNC_GLOBAL_STOP": "1", "HYDRA_UMC_CNC_GLOBAL_STOP_TOPIC": "plant/safety/estop", "HYDRA_UMC_CNC_GLOBAL_STOP_STRICT": "true"}
        with mock.patch.dict(os.environ, wanted):
            settings = settings_from_environment("CNC")
        self.assertEqual((settings.enabled, settings.topic, settings.strict), (True, "plant/safety/estop", True))
        with mock.patch.dict(os.environ):
            os.environ.pop("HYDRA_UMC_CNC_GLOBAL_STOP", None)
            self.assertFalse(settings_from_environment("CNC").enabled)


class GuardTests(unittest.TestCase):
    def guard(self, **kw) -> GlobalStopGuard:
        return GlobalStopGuard(GlobalStopSettings(True, DEFAULT_TOPIC, **kw))

    def test_stop_then_ready(self) -> None:
        guard = self.guard()
        self.assertFalse(guard.blocks())
        guard.observe(SAFE_STOP)
        self.assertTrue(guard.blocks())
        guard.observe(READY)
        self.assertFalse(guard.blocks())

    def test_anything_unclear_is_a_stop_while_enabled(self) -> None:
        for payload in (b"", b"not json", b"[]", b'{"state": "INHIBITED"}', b'{"state": "FAULT"}', b'{"state": "ready"}', b'{"x": 1}'):
            with self.subTest(payload=payload):
                guard = self.guard()
                guard.observe(READY)
                guard.observe(payload)
                self.assertTrue(guard.blocks())

    def test_strict_also_refuses_before_the_first_message(self) -> None:
        self.assertFalse(self.guard().blocks())
        strict = self.guard(strict=True)
        self.assertTrue(strict.blocks())
        strict.observe(READY)
        self.assertFalse(strict.blocks())

    def test_a_switched_off_guard_never_blocks(self) -> None:
        guard = GlobalStopGuard()
        guard.observe(SAFE_STOP)
        self.assertFalse(guard.blocks())
        self.assertIsNone(guard.refusal("cmd/job", job(JobPhase.PREPARE), moving=frozenset({"cmd/x"})))

    def test_an_abort_is_let_through_and_a_malformed_job_is_not(self) -> None:
        guard = self.guard()
        guard.observe(SAFE_STOP)
        self.assertFalse(guard.blocks_job(json.dumps({"phase": "ABORT"}).encode()))
        self.assertTrue(guard.blocks_job(json.dumps({"phase": "LOAD"}).encode()))
        self.assertTrue(guard.blocks_job(b"garbage"))

    def test_withdrawing_a_line_is_allowed_asserting_is_not(self) -> None:
        guard = self.guard()
        guard.observe(SAFE_STOP)
        self.assertFalse(guard.blocks_assertion(b'{"ready": false}', "ready"))
        self.assertTrue(guard.blocks_assertion(b'{"ready": true}', "ready"))
        self.assertTrue(guard.blocks_assertion(b"", "ready"))


class BridgeTests(unittest.TestCase):
    def test_off_by_default_nothing_changes(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("HYDRA_UMC_CNC_GLOBAL_STOP", None)
            bridge, serial = bridge_with(None)
        self.assertFalse(bridge.global_stop.enabled)
        bridge.global_stop.observe(SAFE_STOP)  # even a stop that was heard is ignored while the switch is off
        answer = ask(bridge, "cycle_start_resume")
        self.assertTrue(answer.get("executed"), answer)
        self.assertIn(b"~", serial.sent)

    def test_a_resume_is_refused_during_a_stop_and_runs_after_ready(self) -> None:
        bridge, serial = bridge_with(GlobalStopSettings(True))
        bridge.global_stop.observe(SAFE_STOP)
        answer = ask(bridge, "cycle_start_resume")
        self.assertFalse(answer["executed"])
        self.assertFalse(answer["allowed"])
        self.assertIn("global stop", answer["reason"])
        self.assertNotIn(b"~", serial.sent)
        bridge.global_stop.observe(READY)
        self.assertTrue(ask(bridge, "cycle_start_resume").get("executed"))
        self.assertIn(b"~", serial.sent)

    def test_stops_and_status_keep_working_during_a_stop(self) -> None:
        bridge, serial = bridge_with(GlobalStopSettings(True))
        bridge.global_stop.observe(SAFE_STOP)
        self.assertTrue(ask(bridge, "feed_hold").get("executed"))
        self.assertTrue(ask(bridge, "soft_reset").get("executed"))
        out = bridge.handle_message(f"{TOPIC_PREFIX}cmd/status", b"")
        self.assertTrue(any(p.topic == f"{TOPIC_PREFIX}state" for p in out))

    def test_a_job_is_refused_during_a_stop_but_an_abort_passes(self) -> None:
        bridge, _ = bridge_with(GlobalStopSettings(True))
        bridge.global_stop.observe(SAFE_STOP)
        refused = ask(bridge, "job", job(JobPhase.LOAD))
        self.assertFalse(refused["allowed"])
        self.assertIn("global stop", refused["reason"])
        aborted = ask(bridge, "job", job(JobPhase.ABORT))
        self.assertNotIn("global stop", aborted.get("reason", ""))

    def test_the_refusal_carries_the_request_id(self) -> None:
        bridge, _ = bridge_with(GlobalStopSettings(True))
        bridge.global_stop.observe(SAFE_STOP)
        answer = ask(bridge, "cycle_start_resume", json.dumps({"request_id": "r-9"}).encode())
        self.assertEqual(answer["request_id"], "r-9")

    def test_an_unreadable_announcement_fails_closed(self) -> None:
        bridge, serial = bridge_with(GlobalStopSettings(True))
        bridge.global_stop.observe(READY)
        bridge.global_stop.observe(b"\xff\xfe")
        self.assertFalse(ask(bridge, "cycle_start_resume")["executed"])
        self.assertNotIn(b"~", serial.sent)


class FakeMessage:
    def __init__(self, topic: str, payload: bytes, retain: bool) -> None:
        self.topic, self.payload, self.retain = topic, payload, retain


class DispatcherTests(unittest.TestCase):
    def test_the_retained_announcement_is_the_state_not_a_replay(self) -> None:
        bridge, _ = bridge_with(GlobalStopSettings(True))
        dispatcher = MessageDispatcher(bridge)
        self.addCleanup(dispatcher.close)
        dispatcher.on_message(None, None, FakeMessage(DEFAULT_TOPIC, SAFE_STOP, True))
        self.assertTrue(bridge.global_stop.blocks())
        dispatcher.on_message(None, None, FakeMessage(DEFAULT_TOPIC, READY, True))
        self.assertFalse(bridge.global_stop.blocks())

    def test_a_switched_off_bridge_does_not_listen_to_the_topic(self) -> None:
        bridge, _ = bridge_with(GlobalStopSettings(False))
        dispatcher = MessageDispatcher(bridge)
        self.addCleanup(dispatcher.close)
        dispatcher.on_message(None, None, FakeMessage(DEFAULT_TOPIC, SAFE_STOP, True))
        self.assertEqual(bridge.global_stop.state, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
