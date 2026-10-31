# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Answers are published at QoS 1
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The answer to a command is published at QoS 1; the retained state topics keep QoS 0.

The unit tests need nothing; the last one runs the bridge through a real HYDRA-UMC-MQTT-BROKER on a free local port (see broker_harness.py) and skips itself when
node, paho-mqtt or the installed broker checkout are not there.
"""

import json
import os
import unittest

import broker_harness as harness
from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState, job_to_dict

from hydra_umc_bridge_cnc import CncMqttBridge
from hydra_umc_bridge_cnc.mqtt_transport import TOPIC_PREFIX, MessageDispatcher


class Serial:
    def write(self, data: bytes) -> int:
        return len(data)

    def readline(self) -> bytes:
        return b"<Idle|MPos:0,0,0>\n"

    def close(self) -> None:
        pass


def make_bridge() -> CncMqttBridge:
    return CncMqttBridge(Serial(), lambda: False, lambda: True, lambda: CellState.READY)


def job_payload() -> bytes:
    job = BridgeJob("job-1", "key-1", "orchestrator", JobPhase.PREPARE, MachineState.IDLE, {})
    return json.dumps(job_to_dict(job)).encode()


class ResultQosUnitTests(unittest.TestCase):
    def test_every_answer_is_qos_one_and_the_state_is_not(self) -> None:
        bridge = make_bridge()
        for verb in ("feed_hold", "soft_reset", "cycle_start_resume"):
            with self.subTest(verb=verb):
                (answer,) = bridge.handle_message(f"{TOPIC_PREFIX}cmd/{verb}", b"")
                self.assertEqual(answer.topic, f"{TOPIC_PREFIX}cmd/{verb}/result")
                self.assertEqual(answer.qos, 1)
                self.assertFalse(answer.retain)
        status = bridge.handle_message(f"{TOPIC_PREFIX}cmd/status", b"")
        self.assertTrue(status and all(p.retain and p.qos == 0 for p in status))

    def test_job_answers_refusals_and_repeats_are_qos_one(self) -> None:
        bridge = make_bridge()
        for payload in (job_payload(), job_payload(), b"{not json", b"[]"):
            with self.subTest(payload=payload[:12]):
                (answer,) = bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", payload)
                self.assertEqual(answer.qos, 1)

    def test_refusals_for_an_unknown_payload_size_and_a_request_id_keep_qos_one(self) -> None:
        bridge = make_bridge()
        (too_big,) = bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", b" " * 20000)
        self.assertEqual(too_big.qos, 1)
        (with_id,) = bridge.handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", json.dumps({"request_id": "r-1"}).encode())
        self.assertEqual(with_id.qos, 1)
        self.assertEqual(json.loads(with_id.payload)["request_id"], "r-1")


class ResultQosOverBroker(unittest.TestCase):
    def setUp(self) -> None:
        reason = harness.skip_reason()
        if reason:
            self.skipTest(reason)
        self._closers: list = []
        self.broker = harness.BrokerProcess()
        self.broker.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for closer in reversed(self._closers):
            try:
                closer()
            except Exception:  # noqa: BLE001 - cleanup of a test must not hide its result
                pass
        self.broker.stop()

    def test_the_answer_reaches_a_qos_two_subscriber_as_qos_one(self) -> None:
        from hydra_umc_sdk.bridge_transport import open_bridge_client

        dispatcher = MessageDispatcher(make_bridge())
        client = open_bridge_client(
            "127.0.0.1",
            self.broker.port,
            client_id=f"it-qos-{os.getpid()}",
            topic_prefix=TOPIC_PREFIX,
            dispatcher=dispatcher,
            max_connect_attempts=5,
            connect_retry_delay_seconds=0.5,
        )
        client.loop_start()  # type: ignore[attr-defined]

        def close() -> None:
            client.loop_stop()  # type: ignore[attr-defined]
            client.disconnect()  # type: ignore[attr-defined]
            dispatcher.close(wait=False)

        self._closers.append(close)
        asker = harness.Collector(self.broker.port, f"it-qos-asker-{os.getpid()}")
        self._closers.append(asker.close)
        asker.subscribe(f"{TOPIC_PREFIX}availability")
        self.assertIsNotNone(asker.get(15.0, f"{TOPIC_PREFIX}availability"), "the bridge never announced itself")
        asker.subscribe(f"{TOPIC_PREFIX}cmd/+/result", 2)
        for verb, payload in (("feed_hold", b'{"request_id": "r-1"}'), ("job", job_payload()), ("job", b"{not json")):
            for asked in (0, 1, 2):
                with self.subTest(verb=verb, asked=asked):
                    asker.drain()
                    asker.publish(f"{TOPIC_PREFIX}cmd/{verb}", payload, qos=asked)
                    message = asker.get(10.0, f"{TOPIC_PREFIX}cmd/{verb}/result")
                    self.assertIsNotNone(message, "no answer")
                    assert message is not None
                    self.assertEqual(message[2], 1)
                    self.assertFalse(message[3])


if __name__ == "__main__":
    unittest.main()
