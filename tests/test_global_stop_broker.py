# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - The global stop against a real MQTT broker
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The bridge follows the retained announcement of the global stop through a real HYDRA-UMC-MQTT-BROKER on a free local port (see broker_harness.py).

The tests skip themselves when node, paho-mqtt or the installed broker checkout are not there, so the ordinary run does not need a broker.
"""

import json
import os
import time
import unittest

import broker_harness as harness
from hydra_umc_sdk.bridge_contract import CellState

from hydra_umc_bridge_cnc import CncMqttBridge
from hydra_umc_bridge_cnc.global_stop import DEFAULT_TOPIC, GlobalStopSettings, follow_on_connect
from hydra_umc_bridge_cnc.mqtt_transport import TOPIC_PREFIX, MessageDispatcher

SAFE_STOP = json.dumps({"schema_version": "1.0", "state": "SAFE_STOP", "source": "hydra-umc-server"})
READY = json.dumps({"schema_version": "1.0", "state": "READY", "source": "hydra-umc-server"})

MOVING_VERB = "cycle_start_resume"
STOP_VERB = "feed_hold"


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


class GlobalStopOverBroker(unittest.TestCase):
    def setUp(self) -> None:
        reason = harness.skip_reason()
        if reason:
            self.skipTest(reason)
        self._closers = []
        self.broker = harness.BrokerProcess()
        self.broker.start()
        self.addCleanup(self._cleanup)
        self.serial = HoldingSerial()

    def _cleanup(self) -> None:
        for closer in reversed(self._closers):
            try:
                closer()
            except Exception:  # noqa: BLE001 - cleanup of a test must not hide its result
                pass
        self.broker.stop()

    def collector(self, name: str) -> harness.Collector:
        collector = harness.Collector(self.broker.port, f"it-{name}-{os.getpid()}")
        self._closers.append(collector.close)
        return collector

    def start_bridge(self, settings: GlobalStopSettings) -> CncMqttBridge:
        from hydra_umc_sdk.bridge_transport import open_bridge_client

        bridge = CncMqttBridge(self.serial, lambda: False, lambda: True, lambda: CellState.READY, global_stop=settings)
        dispatcher = MessageDispatcher(bridge)
        client = open_bridge_client(
            "127.0.0.1",
            self.broker.port,
            client_id=f"it-stop-{os.getpid()}",
            topic_prefix=TOPIC_PREFIX,
            dispatcher=dispatcher,
            max_connect_attempts=5,
            connect_retry_delay_seconds=0.5,
            on_connected=follow_on_connect(bridge.global_stop),
        )
        client.loop_start()  # type: ignore[attr-defined]

        def close() -> None:
            client.loop_stop()  # type: ignore[attr-defined]
            client.disconnect()  # type: ignore[attr-defined]
            dispatcher.close(wait=False)

        self._closers.append(close)
        return bridge

    def wait_for(self, condition, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.1)
        return False

    def ask(self, asker: harness.Collector, verb: str, qos: int = 1) -> dict | None:
        asker.drain()
        asker.publish(f"{TOPIC_PREFIX}cmd/{verb}", b"", qos=qos)
        message = asker.get(10.0, f"{TOPIC_PREFIX}cmd/{verb}/result")
        return json.loads(message[1]) if message else None

    def asker(self) -> harness.Collector:
        asker = self.collector("asker")
        asker.subscribe(f"{TOPIC_PREFIX}availability")
        message = asker.get(15.0, f"{TOPIC_PREFIX}availability")
        self.assertIsNotNone(message, "the bridge never announced itself")
        asker.subscribe(f"{TOPIC_PREFIX}cmd/+/result", 2)
        return asker

    def test_a_retained_stop_is_obeyed_at_connect_and_ready_ends_it(self) -> None:
        server = self.collector("server")
        server.publish(DEFAULT_TOPIC, SAFE_STOP, qos=1, retain=True)  # the stop sits on the broker before the bridge exists
        bridge = self.start_bridge(GlobalStopSettings(True))
        self.assertTrue(self.wait_for(lambda: bridge.global_stop.state == "SAFE_STOP"), "the retained stop never reached the bridge")
        asker = self.asker()
        refused = self.ask(asker, MOVING_VERB)
        assert refused is not None
        self.assertFalse(refused["executed"])
        self.assertIn("global stop", refused["reason"])
        self.assertNotIn(b"~", self.serial.sent)
        # the stops still work while stopped
        held = self.ask(asker, STOP_VERB)
        assert held is not None
        self.assertTrue(held["executed"], held)
        # READY ends it
        server.publish(DEFAULT_TOPIC, READY, qos=1, retain=True)
        self.assertTrue(self.wait_for(lambda: bridge.global_stop.state == "READY"))
        resumed = self.ask(asker, MOVING_VERB)
        assert resumed is not None
        self.assertTrue(resumed["executed"], resumed)
        self.assertIn(b"~", self.serial.sent)

    def test_a_live_stop_arrives_at_every_qos_and_refuses_at_once(self) -> None:
        bridge = self.start_bridge(GlobalStopSettings(True))
        asker = self.asker()
        server = self.collector("server")
        for qos in (0, 1, 2):
            with self.subTest(qos=qos):
                server.publish(DEFAULT_TOPIC, READY, qos=1, retain=True)
                self.assertTrue(self.wait_for(lambda: bridge.global_stop.state == "READY"))
                server.publish(DEFAULT_TOPIC, SAFE_STOP, qos=qos, retain=True)
                self.assertTrue(self.wait_for(lambda: bridge.global_stop.blocks()))
                answer = self.ask(asker, MOVING_VERB)
                assert answer is not None
                self.assertFalse(answer["executed"])

    def test_the_switch_off_ignores_a_retained_stop(self) -> None:
        server = self.collector("server")
        server.publish(DEFAULT_TOPIC, SAFE_STOP, qos=1, retain=True)
        bridge = self.start_bridge(GlobalStopSettings(False))
        asker = self.asker()
        answer = self.ask(asker, MOVING_VERB)
        assert answer is not None
        self.assertTrue(answer["executed"], answer)
        self.assertEqual(bridge.global_stop.state, "UNKNOWN")

    def test_an_unreadable_announcement_fails_closed_through_the_broker(self) -> None:
        server = self.collector("server")
        server.publish(DEFAULT_TOPIC, "{ not json", qos=1, retain=True)
        bridge = self.start_bridge(GlobalStopSettings(True))
        self.assertTrue(self.wait_for(lambda: bridge.global_stop.state == "UNREADABLE"))
        asker = self.asker()
        answer = self.ask(asker, MOVING_VERB)
        assert answer is not None
        self.assertFalse(answer["executed"])

    def test_the_stop_is_read_again_after_the_broker_comes_back(self) -> None:
        server = self.collector("server")
        server.publish(DEFAULT_TOPIC, SAFE_STOP, qos=1, retain=True)
        bridge = self.start_bridge(GlobalStopSettings(True))
        self.assertTrue(self.wait_for(lambda: bridge.global_stop.state == "SAFE_STOP"))
        # a broker that restarts keeps no retained message here: the latch is lost with it, but the bridge keeps what it heard
        port = self.broker.port
        self.broker.stop()
        time.sleep(1.0)
        self.assertTrue(bridge.global_stop.blocks(), "the stop must survive the loss of the broker")
        self.broker = harness.BrokerProcess(port)
        self.broker.start()
        self.assertTrue(bridge.global_stop.blocks())


if __name__ == "__main__":
    unittest.main()
