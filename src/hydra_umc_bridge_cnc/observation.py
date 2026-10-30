# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Read-only controller observation normalization
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Normalize local controller evidence without opening a controller connection."""

from __future__ import annotations

import math
from collections.abc import Mapping

from hydra_umc_sdk.serial_link import parse_status_report

from .cell import CncSnapshot


def _strict_bool(value: object, safe_default: bool) -> bool:
    """Reject strings and numbers; safety signals must be actual booleans."""

    return value if isinstance(value, bool) else safe_default


def _finite_or_none(value: object) -> float | None:
    """A reported number, or None: a string, a bool, NaN or infinity is not a reading."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def snapshot_from_mapping(payload: object) -> CncSnapshot:
    """Build a fail-safe snapshot from already-collected controller evidence."""

    if not isinstance(payload, Mapping):
        return CncSnapshot("", True, False)
    state = payload.get("controller_state", payload.get("state", ""))
    return CncSnapshot(
        state if isinstance(state, str) else "",
        _strict_bool(payload.get("estop"), True),
        _strict_bool(payload.get("door_closed"), False),
        spindle_speed=_finite_or_none(payload.get("spindle_speed")),
        feed_rate=_finite_or_none(payload.get("feed_rate")),
    )


def snapshot_from_grbl_status(status_line: object, *, estop: object, door_closed: object) -> CncSnapshot:
    """Extract only GRBL's leading state token; omitted safeguards fail closed."""

    state = ""
    if isinstance(status_line, str) and status_line.startswith("<") and "|" in status_line:
        state = status_line[1 : status_line.index("|")]
    # Position, feed, spindle speed and overrides are read from the same line when it is a complete report; a field the
    # controller did not send stays None (the spindle gate tells "stopped" from "did not say").
    report = parse_status_report(status_line)
    return CncSnapshot(
        state,
        _strict_bool(estop, True),
        _strict_bool(door_closed, False),
        spindle_speed=report.spindle_speed,
        feed_rate=report.feed_rate,
        machine_position=report.machine_position,
        work_position=report.work_position,
        work_offset=report.work_offset,
        overrides=report.overrides,
    )


def snapshot_from_mtconnect_execution(execution: object, *, estop: object, door_closed: object) -> CncSnapshot:
    """Normalize saved MTConnect execution evidence without opening a controller.

    MTConnect is a read-only compatibility input here, not a command channel.
    Unknown or unavailable execution values remain ``OFFLINE`` so a caller
    cannot mistake partial telemetry for an idle, safe machine.
    """

    state_map = {
        "READY": "IDLE",
        "STOPPED": "STOPPED",
        "ACTIVE": "RUNNING",
        "EXECUTING": "RUNNING",
        "FEED_HOLD": "HOLD",
        "INTERRUPTED": "HOLD",
        "FAULT": "FAULT",
    }
    state = state_map.get(execution.upper(), "") if isinstance(execution, str) else ""
    return CncSnapshot(state, _strict_bool(estop, True), _strict_bool(door_closed, False))
