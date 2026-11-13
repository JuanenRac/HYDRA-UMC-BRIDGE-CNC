# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - tests/test_heartbeat.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The periodic publication of the state: off by default, bounded, and never ended by a bad round."""

import os
import threading
import unittest
from unittest import mock

from hydra_umc_bridge_cnc.heartbeat import (
    HEARTBEAT_ENVIRONMENT,
    MAX_INTERVAL_S,
    MIN_INTERVAL_S,
    StateHeartbeat,
    interval_from_environment,
)


class IntervalTests(unittest.TestCase):
    def test_the_variable_is_named_after_the_bridge(self):
        self.assertEqual(HEARTBEAT_ENVIRONMENT, "HYDRA_UMC_CNC_HEARTBEAT_S")

    def test_unset_or_empty_is_off(self):
        self.assertEqual(interval_from_environment({}), 0.0)
        self.assertEqual(interval_from_environment({HEARTBEAT_ENVIRONMENT: "  "}), 0.0)

    def test_values_that_are_not_a_positive_finite_number_are_off(self):
        for raw in ("abc", "0", "-5", "nan", "inf", "-inf", "1,5"):
            self.assertEqual(interval_from_environment({HEARTBEAT_ENVIRONMENT: raw}), 0.0, raw)

    def test_a_number_is_clamped_to_the_allowed_range(self):
        self.assertEqual(interval_from_environment({HEARTBEAT_ENVIRONMENT: "0.1"}), MIN_INTERVAL_S)
        self.assertEqual(interval_from_environment({HEARTBEAT_ENVIRONMENT: "30"}), 30.0)
        self.assertEqual(interval_from_environment({HEARTBEAT_ENVIRONMENT: "999999"}), MAX_INTERVAL_S)


class HeartbeatTests(unittest.TestCase):
    def test_a_tick_publishes_every_message_it_reads(self):
        sent = []
        beat = StateHeartbeat(lambda: ["a", "b"], sent.append, 5.0)
        self.assertEqual(beat.tick(), 2)
        self.assertEqual(sent, ["a", "b"])
        self.assertEqual((beat.published, beat.failures), (2, 0))

    def test_a_failed_reading_is_counted_and_the_next_round_still_runs(self):
        calls = []

        def produce():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("the link is down")
            return ["ok"]

        sent = []
        beat = StateHeartbeat(produce, sent.append, 5.0)
        with self.assertLogs("hydra_umc_bridge_cnc.heartbeat", level="ERROR"):
            self.assertEqual(beat.tick(), 0)
        self.assertEqual(beat.tick(), 1)
        self.assertEqual((beat.published, beat.failures, sent), (1, 1, ["ok"]))

    def test_a_failed_publication_is_counted_not_raised(self):
        def publish(_message):
            raise RuntimeError("not connected")

        beat = StateHeartbeat(lambda: ["a"], publish, 5.0)
        with self.assertLogs("hydra_umc_bridge_cnc.heartbeat", level="ERROR"):
            self.assertEqual(beat.tick(), 0)
        self.assertEqual(beat.failures, 1)

    def test_the_interval_must_be_above_zero(self):
        for bad in (0.0, -1.0, float("nan")):
            with self.assertRaises(ValueError):
                StateHeartbeat(lambda: [], print, bad)

    def test_the_thread_publishes_until_it_is_stopped(self):
        got = threading.Event()
        count = []

        def publish(message):
            count.append(message)
            got.set()

        beat = StateHeartbeat(lambda: ["state"], publish, 0.01)
        beat.start()
        beat.start()  # a second start does not start a second loop
        self.assertTrue(got.wait(5.0))
        beat.stop()
        after = len(count)
        threading.Event().wait(0.1)
        self.assertEqual(len(count), after)

    def test_stop_without_start_is_harmless(self):
        StateHeartbeat(lambda: [], print, 1.0).stop()


class WiringTests(unittest.TestCase):
    def _run(self, bridge, **kwargs):
        from hydra_umc_bridge_cnc import mqtt_transport

        client = mock.MagicMock()
        published = threading.Event()
        client.publish.side_effect = lambda *a, **k: published.set()
        client.loop_forever.side_effect = lambda: published.wait(0.5)
        with mock.patch.object(mqtt_transport, "open_bridge_client", return_value=client):
            mqtt_transport.run_forever(bridge, "127.0.0.1", **kwargs)
        return client

    def _bridge(self):
        from hydra_umc_bridge_cnc.mqtt_transport import MqttPublish

        bridge = mock.MagicMock()
        bridge.global_stop = None
        bridge.handle_message.return_value = [MqttPublish("hydra/bridges/cnc/state", "{}", retain=True)]
        return bridge

    def test_with_an_interval_the_state_goes_out_while_the_bridge_runs(self):
        bridge = self._bridge()
        client = self._run(bridge, heartbeat_interval_s=0.01)
        bridge.handle_message.assert_any_call("hydra/bridges/cnc/cmd/status", b"")
        args, kwargs = client.publish.call_args
        self.assertEqual(args[0], "hydra/bridges/cnc/state")
        self.assertTrue(kwargs["retain"])

    def test_without_an_interval_nothing_is_published_by_the_loop(self):
        os.environ.pop(HEARTBEAT_ENVIRONMENT, None)
        bridge = self._bridge()
        client = self._run(bridge)
        bridge.handle_message.assert_not_called()
        client.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
