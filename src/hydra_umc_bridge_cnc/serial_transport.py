# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Real GRBL serial transport
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Real, fail-closed GRBL serial transport - never a G-code streaming path.

This module can genuinely open a serial port and exchange bytes with a real GRBL controller. It stays inside the
boundary this bridge has always documented: it queries real-time status and sends only GRBL's own real-time single-byte
control characters (status query, feed hold, cycle start/resume, soft reset) - it never streams a G-code program. The
controller (GRBL itself) keeps all real-time trajectory, limits, spindle and safety authority, unchanged.

A GRBL controller has ONE serial port: while this module holds it open, no other sender (a G-code sender such as UGS, bCNC
or CNCjs) can use it. Whoever runs the machine's programs has to do it through something that shares the port (see the
CNCjs adapter, which observes and pauses without owning the port), or this bridge is not the right tool for that machine -
see docs/BRIDGE_GUIDE.md.

The port, the reconnecting connection, the status query and the real-time byte writer are the shared
``hydra_umc_sdk.serial_link`` (the laser bridge reads GRBL with the same code); this module adds what is specific to the CNC
bridge: turning a report into a ``CncSnapshot`` and gating the resume on the state of the cell. The logic is written
against the small ``SerialLike`` protocol, so every safety rule is unit-testable with an in-memory fake.
"""

from __future__ import annotations

from typing import Callable

from hydra_umc_sdk.bridge_contract import CellState, MachineState
from hydra_umc_sdk.serial_link import (
    CYCLE_START_RESUME,
    FEED_HOLD,
    SOFT_RESET,
    ReconnectingSerialConnection,
    RealtimeCommandResult,
    SerialLike,
    call_bool,
    open_serial_port,
    open_serial_port_by_id,
    read_status_line,
    resolve_serial_device_path,
    send_realtime,
)

from .cell import CncSnapshot
from .observation import snapshot_from_grbl_status

__all__ = [
    "GrblRealtimeControl",
    "GrblSerialProbe",
    "ReconnectingSerialConnection",
    "RealtimeCommandResult",
    "SerialLike",
    "open_serial_port",
    "open_serial_port_by_id",
    "resolve_serial_device_path",
]


class GrblSerialProbe:
    """Query real GRBL status over an already-open serial-like connection."""

    def query_status(
        self,
        connection: SerialLike,
        *,
        estop: Callable[[], object],
        door_closed: Callable[[], object],
    ) -> CncSnapshot:
        # `estop`/`door_closed` are callables, not already-read values, and are deliberately called AFTER the blocking
        # write()/readline() below, never before. `readline()` can block for up to the real timeout of the connection
        # waiting on GRBL's own reply - an E-STOP hit or a door opened DURING that wait must still be reflected in the
        # snapshot this call returns.
        try:
            status_line = read_status_line(connection)
        except Exception:  # noqa: BLE001 - whatever goes wrong on the link, the machine is not known to be fine
            return CncSnapshot("", call_bool(estop, True), False)
        return snapshot_from_grbl_status(status_line, estop=call_bool(estop, True), door_closed=call_bool(door_closed, False))


class GrblRealtimeControl:
    """Send only GRBL's own real-time control bytes - never a G-code program.

    Feed hold and soft reset are always allowed (same de-escalation reasoning as ABORT/HOLD_POSITION elsewhere in this
    ecosystem - an operator must always be able to pause or reset); resume uses a standalone gate requiring a genuinely
    HOLDING machine, not the generic IDLE-based gate (which is backwards for resuming a paused job).
    """

    def feed_hold(self, connection: SerialLike) -> RealtimeCommandResult:
        return send_realtime(connection, FEED_HOLD, "feed hold sent")

    def soft_reset(self, connection: SerialLike) -> RealtimeCommandResult:
        return send_realtime(connection, SOFT_RESET, "soft reset sent")

    def cycle_start_resume(
        self, connection: SerialLike, cell_state: CellState, cnc: CncSnapshot
    ) -> RealtimeCommandResult:
        if cell_state is not CellState.READY:
            return RealtimeCommandResult(False, False, f"cell is {cell_state.value}, not READY")
        machine_state = cnc.machine_state()
        if machine_state is not MachineState.HOLDING:
            return RealtimeCommandResult(
                False, False, f"CNC is {machine_state.value}, not HOLDING (nothing to resume)"
            )
        return send_realtime(connection, CYCLE_START_RESUME, "cycle start/resume sent")
