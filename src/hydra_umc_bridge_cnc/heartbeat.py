# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - src/hydra_umc_bridge_cnc/heartbeat.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A periodic publication of the state of the cnc cell, off by default.

The retained `state` carries `observed_at`, so a consumer can tell an old reading from a new one, but nothing refreshes it between two
commands. With `HYDRA_UMC_CNC_HEARTBEAT_S` set to a number of seconds (1 to 3600) the bridge reads the state that often and publishes it the same way
`cmd/status` does. Unset, empty, zero, negative or not a number all mean off. The reading goes through the same lock as every other reading of the
bridge, so it never interleaves with a command. A failed reading is counted and logged and the next one is tried; the loop never stops by itself.
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping

HEARTBEAT_ENVIRONMENT = "HYDRA_UMC_CNC_HEARTBEAT_S"
MIN_INTERVAL_S = 1.0
MAX_INTERVAL_S = 3600.0

_LOG = logging.getLogger(__name__)


def interval_from_environment(env: Mapping[str, str] | None = None) -> float:
    """The interval in seconds, clamped to 1..3600, or 0.0 (off) when the variable is unset, empty, not a finite number or not above zero."""
    raw = (env if env is not None else os.environ).get(HEARTBEAT_ENVIRONMENT, "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    if not value > 0.0 or value != value or value == float("inf"):
        return 0.0
    return min(max(value, MIN_INTERVAL_S), MAX_INTERVAL_S)


class StateHeartbeat:
    """Calls `produce()` every `interval_s` seconds and hands every message it returns to `publish`."""

    def __init__(self, produce: Callable[[], list], publish: Callable[[object], object], interval_s: float) -> None:
        if not interval_s > 0.0:
            raise ValueError("interval_s must be above zero")
        self._produce = produce
        self._publish = publish
        self.interval_s = interval_s
        self.published = 0
        self.failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> int:
        """One round: read and publish. Returns how many messages went out; a failure of the reading or of the publication is counted, never raised."""
        try:
            messages = self._produce()
            for message in messages:
                self._publish(message)
        except Exception:  # noqa: BLE001 - a bad round must not end the loop
            self.failures += 1
            _LOG.exception("state heartbeat failed")
            return 0
        self.published += len(messages)
        return len(messages)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="state-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s + 1.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.tick()
