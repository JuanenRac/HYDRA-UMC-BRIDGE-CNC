# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Request correlation tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A caller with several commands in flight matches each answer to its request through `request_id`."""

import json
import unittest

from hydra_umc_sdk.bridge_contract import BridgeJob, JobPhase, MachineState, job_to_dict
from hydra_umc_bridge_cnc.mqtt_transport import TOPIC_PREFIX, request_id_of, with_request_id
from hydra_umc_sdk.bridge_transport import MqttPublish
from test_mqtt_transport import bridge


def job_payload(request_id=None):
    body = job_to_dict(BridgeJob("job-1", "key-1", "cnc-1", JobPhase.PROCESS, MachineState.IDLE, {}))
    if request_id is not None:
        body["request_id"] = request_id
    return json.dumps(body).encode()


class RequestIdTests(unittest.TestCase):
    def test_a_plain_token_is_taken_and_anything_else_is_not(self):
        self.assertEqual(request_id_of(b'{"request_id": "req-1.a:b"}'), "req-1.a:b")
        for bad in (b"", b"not json", b"[1]", b'{"request_id": 5}', b'{"request_id": ""}', b'{"request_id": "has space"}', b'{"request_id": "' + b"x" * 65 + b'"}', b'{"a": 1}'):
            self.assertIsNone(request_id_of(bad), bad)

    def test_the_result_of_a_job_carries_the_request_id(self):
        b, _, _ = bridge()
        out = b.handle_message(f"{TOPIC_PREFIX}cmd/job", job_payload("req-7"))
        self.assertEqual(json.loads(out[0].payload)["request_id"], "req-7")

    def test_a_job_without_one_gets_no_request_id(self):
        b, _, _ = bridge()
        out = b.handle_message(f"{TOPIC_PREFIX}cmd/job", job_payload())
        self.assertNotIn("request_id", json.loads(out[0].payload))

    def test_a_refusal_for_a_payload_that_cannot_be_read_is_not_decorated_and_does_not_raise(self):
        b, _, _ = bridge()
        out = b.handle_message(f"{TOPIC_PREFIX}cmd/job", b"{broken")
        self.assertFalse(json.loads(out[0].payload)["allowed"])

    def test_state_topics_are_left_alone(self):
        publishes = [MqttPublish(f"{TOPIC_PREFIX}state", json.dumps({"a": 1}), retain=True)]
        self.assertEqual(with_request_id(publishes, "req-1")[0].payload, publishes[0].payload)

    def test_two_jobs_each_get_their_own_request_id(self):
        b, _, _ = bridge()
        b2 = b
        first = b2.handle_message(f"{TOPIC_PREFIX}cmd/job", job_payload("req-1"))
        second = b2.handle_message(f"{TOPIC_PREFIX}cmd/job", job_payload("req-2"))
        self.assertEqual(json.loads(first[0].payload)["request_id"], "req-1")
        self.assertEqual(json.loads(second[0].payload)["request_id"], "req-2")


if __name__ == "__main__":
    unittest.main()
