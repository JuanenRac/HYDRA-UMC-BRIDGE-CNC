# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Controller link contract
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The four things the bridge needs of a CNC controller, whatever the controller is.

``ControllerLink`` is what ``CncMqttBridge`` talks to: read the state (``read_snapshot``), and send the three
de-escalating or resuming requests the bridge has always offered (``feed_hold``, ``soft_reset``, ``cycle_start_resume``).
GRBL over a serial port (``GrblController``), a LinuxCNC installation (``LinuxcncController``) and a CNCjs server that
owns the port (``CncjsController``) all implement it, so the topics, the gate and the idempotency stay one thing.

A link never streams a program. A link that cannot send a request answers with ``allowed=False`` and a reason: a request
that did not happen is never reported as one that did.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from hydra_umc_sdk.bridge_contract import CellState, MachineState
from hydra_umc_sdk.serial_link import RealtimeCommandResult, SerialLike

from .cell import CncSnapshot
from .serial_transport import GrblRealtimeControl, GrblSerialProbe


@runtime_checkable
class ControllerLink(Protocol):
    """A CNC controller as the bridge sees it."""

    name: str

    def read_snapshot(self, *, estop: Callable[[], object], door_closed: Callable[[], object]) -> CncSnapshot:
        """The state of the controller now. The two callables are the physical safeguards of the cell, read after any blocking I/O."""
        ...

    def feed_hold(self) -> RealtimeCommandResult: ...

    def soft_reset(self) -> RealtimeCommandResult: ...

    def cycle_start_resume(self, cell_state: CellState, snapshot: CncSnapshot) -> RealtimeCommandResult: ...


class GrblController:
    """GRBL over a serial port the bridge holds open."""

    name = "grbl"

    def __init__(self, connection: SerialLike) -> None:
        self._connection = connection
        self._probe = GrblSerialProbe()
        self._realtime = GrblRealtimeControl()

    @property
    def connection(self) -> SerialLike:
        return self._connection

    def read_snapshot(self, *, estop: Callable[[], object], door_closed: Callable[[], object]) -> CncSnapshot:
        return self._probe.query_status(self._connection, estop=estop, door_closed=door_closed)

    def feed_hold(self) -> RealtimeCommandResult:
        return self._realtime.feed_hold(self._connection)

    def soft_reset(self) -> RealtimeCommandResult:
        return self._realtime.soft_reset(self._connection)

    def cycle_start_resume(self, cell_state: CellState, snapshot: CncSnapshot) -> RealtimeCommandResult:
        return self._realtime.cycle_start_resume(self._connection, cell_state, snapshot)


class ReadOnlyController:
    """Base of a link that observes and sends nothing: every request is refused with the reason."""

    name = "read-only"

    def read_snapshot(self, *, estop: Callable[[], object], door_closed: Callable[[], object]) -> CncSnapshot:
        raise NotImplementedError

    def feed_hold(self) -> RealtimeCommandResult:
        return RealtimeCommandResult(False, False, f"the {self.name} link is read-only: it cannot send a feed hold")

    def soft_reset(self) -> RealtimeCommandResult:
        return RealtimeCommandResult(False, False, f"the {self.name} link is read-only: it cannot send a reset")

    def cycle_start_resume(self, cell_state: CellState, snapshot: CncSnapshot) -> RealtimeCommandResult:
        return RealtimeCommandResult(False, False, f"the {self.name} link is read-only: it cannot resume")


def resume_refusal(cell_state: CellState, snapshot: CncSnapshot) -> RealtimeCommandResult | None:
    """The refusal for a resume that is not allowed, or None when it is: the cell must be READY and the machine really HOLDING.

    Every link applies this same rule before sending a resume, so a resume is never gentler on one controller than on another.
    """

    if cell_state is not CellState.READY:
        return RealtimeCommandResult(False, False, f"cell is {cell_state.value}, not READY")
    machine_state = snapshot.machine_state()
    if machine_state is not MachineState.HOLDING:
        return RealtimeCommandResult(False, False, f"CNC is {machine_state.value}, not HOLDING (nothing to resume)")
    return None
