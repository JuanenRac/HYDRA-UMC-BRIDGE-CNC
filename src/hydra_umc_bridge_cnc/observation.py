# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Read-only controller observation normalization
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Normalize local controller evidence without opening a controller connection."""

from __future__ import annotations

from collections.abc import Mapping

from .cell import CncSnapshot


def _strict_bool(value: object, safe_default: bool) -> bool:
    """Reject strings and numbers; safety signals must be actual booleans."""

    return value if isinstance(value, bool) else safe_default


def snapshot_from_mapping(payload: object) -> CncSnapshot:
    """Build a fail-safe snapshot from already-collected controller evidence."""

    if not isinstance(payload, Mapping):
        return CncSnapshot("", True, False)
    state = payload.get("controller_state", payload.get("state", ""))
    return CncSnapshot(
        state if isinstance(state, str) else "",
        _strict_bool(payload.get("estop"), True),
        _strict_bool(payload.get("door_closed"), False),
    )


def snapshot_from_grbl_status(status_line: object, *, estop: object, door_closed: object) -> CncSnapshot:
    """Extract only GRBL's leading state token; omitted safeguards fail closed."""

    state = ""
    if isinstance(status_line, str) and status_line.startswith("<") and "|" in status_line:
        state = status_line[1 : status_line.index("|")]
    return CncSnapshot(state, _strict_bool(estop, True), _strict_bool(door_closed, False))
