# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - CNC cell boundary
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Translate controller observations to a safe high-level coordination gate."""

from __future__ import annotations

import os
from dataclasses import dataclass

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, GateDecision, JobPhase, MachineState, evaluate_job
from hydra_umc_sdk.grbl_status import grbl_machine_state

# A machine that is stopped and does not go on until an operator resets it (MTConnect STOPPED). It is its own state: it is not
# IDLE (no work may start on it) and not HOLDING (it cannot be resumed). The state exists in the SDK from the release that adds
# it; with an older SDK the bridge falls back to FAULT, which refuses everything a stopped machine refuses.
STOPPED_STATE = getattr(MachineState, "STOPPED", MachineState.FAULT)

# The words this bridge reads on top of the shared table of the SDK.
CNC_EXTRA_STATES = {"OFF": MachineState.FAULT}

# The phases in which a robot arm reaches into the machine (it loads or unloads a part): a spindle that is still turning,
# or a feed that is still moving, is a hazard that an IDLE controller state alone does not rule out.
ARM_ENTRY_PHASES = frozenset({JobPhase.LOAD, JobPhase.UNLOAD})

SPINDLE_GATE_OFF = "off"
SPINDLE_GATE_WHEN_KNOWN = "when_known"
SPINDLE_GATE_REQUIRED = "required"
SPINDLE_GATE_MODES = (SPINDLE_GATE_OFF, SPINDLE_GATE_WHEN_KNOWN, SPINDLE_GATE_REQUIRED)
SPINDLE_GATE_ENVIRONMENT = "HYDRA_UMC_CNC_SPINDLE_GATE"


def spindle_gate_from_environment(environment: dict[str, str] | None = None) -> str:
    """The configured spindle gate mode; an unset or unknown value is the conservative default, ``when_known``."""

    value = (environment if environment is not None else os.environ).get(SPINDLE_GATE_ENVIRONMENT, "").strip().lower()
    return value if value in SPINDLE_GATE_MODES else SPINDLE_GATE_WHEN_KNOWN


@dataclass(frozen=True)
class CncSnapshot:
    controller_state: str
    estop: bool
    door_closed: bool
    # What the controller reported besides its state. ``None`` is "the controller did not say", never zero.
    spindle_speed: float | None = None
    feed_rate: float | None = None
    machine_position: tuple[float, ...] | None = None
    work_position: tuple[float, ...] | None = None
    work_offset: tuple[float, ...] | None = None
    overrides: tuple[float, float, float] | None = None  # feed %, rapid %, spindle %

    def spindle_stopped(self) -> bool | None:
        """True when the controller reports no spindle speed and no feed, False when it reports either, None when it did not say."""

        # Either value that is known and above zero is enough: a report with a feed but no spindle speed is not "did not say".
        if (self.spindle_speed is not None and self.spindle_speed > 0) or (self.feed_rate is not None and self.feed_rate > 0):
            return False
        if self.spindle_speed is None:
            return None
        return True

    def machine_state(self) -> MachineState:
        if self.estop or not self.door_closed:
            return MachineState.SAFE_STOP
        if not isinstance(self.controller_state, str):
            return MachineState.OFFLINE
        # The state word of a GRBL v1.1 report (numeric substate dropped, case ignored, unknown is OFFLINE) is read by the table every bridge shares;
        # OFF is the one word this bridge adds on top of it (a fault).
        return grbl_machine_state(self.controller_state, CNC_EXTRA_STATES)


class CncCellBridge:
    """Permit robot auxiliary work only beside an idle, safeguarded CNC.

    ``spindle_gate`` adds a second condition to the phases in which an arm reaches into the machine (load and unload):
    the spindle must be stopped as well as the controller idle. ``when_known`` (the default) refuses when the controller
    reports a turning spindle or a moving feed and says nothing when it does not report them; ``required`` also refuses
    when the controller does not say; ``off`` is the behaviour without this condition.
    """

    def __init__(self, spindle_gate: str = SPINDLE_GATE_WHEN_KNOWN) -> None:
        if spindle_gate not in SPINDLE_GATE_MODES:
            raise ValueError(f"spindle_gate must be one of {SPINDLE_GATE_MODES}, got {spindle_gate!r}")
        self._spindle_gate = spindle_gate

    def plan(self, job: BridgeJob, cell_state: CellState, cnc: CncSnapshot) -> GateDecision:
        observed = BridgeJob(job.job_id, job.idempotency_key, job.source, job.phase, cnc.machine_state(), job.parameters)
        decision = evaluate_job(observed, cell_state, cnc.machine_state())
        if not decision.allowed or job.phase not in ARM_ENTRY_PHASES or self._spindle_gate == SPINDLE_GATE_OFF:
            return decision
        stopped = cnc.spindle_stopped()
        if stopped is False:
            return GateDecision(False, "the spindle is not stopped (the controller reports a spindle speed or a feed): no arm may reach into the machine")
        if stopped is None and self._spindle_gate == SPINDLE_GATE_REQUIRED:
            return GateDecision(False, "the controller did not report the spindle speed, and this cell requires a stopped spindle before an arm reaches into the machine")
        return decision
