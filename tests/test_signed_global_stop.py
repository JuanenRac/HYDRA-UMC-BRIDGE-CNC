# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - tests/test_signed_global_stop.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""With a key file a SAFE_STOP is obeyed as before and only a signed READY ends it."""

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from hydra_umc_sdk.global_stop import DEFAULT_TOPIC, GlobalStopGuard, sign_announcement

from hydra_umc_bridge_cnc.mqtt_transport import GLOBAL_STOP_ENVIRONMENT_NAME
from hydra_umc_bridge_cnc.service import parse_config

ROOT = Path(__file__).resolve().parents[1]
KEY = b"k" * 32


def announcement(state):
    return {"schema_version": "1.0", "state": state, "source": "test"}


class SignedGlobalStop(unittest.TestCase):
    def test_only_a_signed_ready_ends_a_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            key_file = Path(folder) / "stop.key"
            key_file.write_bytes(KEY)
            os.chmod(key_file, 0o600)
            base = f"HYDRA_UMC_{GLOBAL_STOP_ENVIRONMENT_NAME}_GLOBAL_STOP"
            with mock.patch.dict(os.environ, {base: "1", f"{base}_KEY_FILE": str(key_file)}):
                guard = GlobalStopGuard.from_environment(GLOBAL_STOP_ENVIRONMENT_NAME)
        guard.observe(json.dumps(announcement("SAFE_STOP")).encode())
        self.assertTrue(guard.blocks())
        guard.observe(json.dumps(announcement("READY")).encode())
        self.assertTrue(guard.blocks())
        guard.observe(json.dumps(sign_announcement(DEFAULT_TOPIC, announcement("READY"), KEY)).encode())
        self.assertFalse(guard.blocks())

    def test_the_key_file_of_the_configuration_file_reaches_the_guard_settings(self):
        data = tomllib.loads((ROOT / "deploy/bridge-cnc.example.toml").read_text(encoding="utf-8"))
        data["global_stop"]["ready_key_file"] = "/etc/hydra-umc/global-stop.key"
        self.assertEqual(parse_config(data).global_stop.ready_key_file, "/etc/hydra-umc/global-stop.key")
        data["global_stop"]["ready_state_file"] = "/var/lib/hydra-umc/global-stop.json"
        self.assertEqual(parse_config(data).global_stop.ready_state_file, "/var/lib/hydra-umc/global-stop.json")

    def test_an_old_release_does_not_end_the_stop_after_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            key_file = Path(folder) / "stop.key"
            key_file.write_bytes(KEY)
            os.chmod(key_file, 0o600)
            base = f"HYDRA_UMC_{GLOBAL_STOP_ENVIRONMENT_NAME}_GLOBAL_STOP"
            environment = {base: "1", f"{base}_KEY_FILE": str(key_file), f"{base}_STATE_FILE": str(Path(folder) / "mark.json")}
            old, stop, current = (json.dumps(sign_announcement(DEFAULT_TOPIC, announcement(state), KEY, now_ms=at)).encode()
                                  for state, at in (("READY", 1000), ("SAFE_STOP", 2000), ("READY", 3000)))
            with mock.patch.dict(os.environ, environment):
                first = GlobalStopGuard.from_environment(GLOBAL_STOP_ENVIRONMENT_NAME)
                for payload in (old, stop, current):
                    first.observe(payload)
                restarted = GlobalStopGuard.from_environment(GLOBAL_STOP_ENVIRONMENT_NAME)
                restarted.observe(old)
                self.assertTrue(restarted.blocks())
                restarted.observe(current)
                self.assertFalse(restarted.blocks())


if __name__ == "__main__":
    unittest.main()
