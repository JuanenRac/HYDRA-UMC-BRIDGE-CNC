# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Integration with a real MQTT broker
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""This bridge's transport against a real HYDRA-UMC-MQTT-BROKER on a free local port (see broker_harness.py).

The tests skip themselves when node, paho-mqtt or the installed broker checkout are not there, so the ordinary run does not need a broker.
"""

import json
import unittest

import broker_harness as harness
from hydra_umc_sdk.bridge_contract import CellState

from hydra_umc_bridge_cnc import CncMqttBridge
from hydra_umc_bridge_cnc.mqtt_transport import TOPIC_PREFIX, MessageDispatcher


class FakeSerial:
    def write(self, data: bytes) -> int:
        return len(data)

    def readline(self) -> bytes:
        return b"<Idle|MPos:0,0,0>\n"

    def close(self) -> None:
        pass


class CncOverBroker(harness.BridgeOverBrokerCases):
    PREFIX = TOPIC_PREFIX
    COMMAND_VERB = "status"
    RESULT_SUFFIX = "state"

    def make_bridge(self) -> object:
        return CncMqttBridge(FakeSerial(), lambda: False, lambda: True, lambda: CellState.READY)

    def make_dispatcher(self, bridge: object) -> object:
        return MessageDispatcher(bridge)

    def command_payload(self) -> bytes:
        return json.dumps({"request_id": "it-1"}).encode()

    def answer_is_right(self, body: dict) -> bool:
        return body.get("machine_state") == "IDLE" and "observed_at" in body


if __name__ == "__main__":
    unittest.main()
