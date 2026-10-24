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

        if self.spindle_speed is None:
            return None
        if self.spindle_speed > 0 or (self.feed_rate is not None and self.feed_rate > 0):
            return False
        return True

    def machine_state(self) -> MachineState:
        if self.estop or not self.door_closed:
            return MachineState.SAFE_STOP
        if not isinstance(self.controller_state, str):
            return MachineState.OFFLINE
        # Real GRBL v1.1 status reports append a numeric substate to Hold
        # and Door (e.g. "Hold:0"/"Hold:1", "Door:0".."Door:3" - see
        # github.com/gnea/grbl/wiki/Grbl-v1.1-Interface#---current-status),
        # never bare "Hold"/"Door" - a real controller connected over the
        # serial transport would always send the suffixed form, which the
        # exact-match checks below would otherwise silently miss and fall
        # through to OFFLINE. Splitting off the substate here is safe for
        # every other token too, since none of them use a colon.
        state = self.controller_state.upper().split(":")[0]
        if state == "IDLE":
            return MachineState.IDLE
        # Real GRBL v1.1 status-report state tokens (researched against
        # github.com/gnea/grbl/wiki/Grbl-v1.1-Interface): "Run"/"Jog"/
        # "Home" are all real active-motion states, not just "Run" alone -
        # a machine jogging or homing is exactly as busy as one running a
        # program. "RUNNING" also stays accepted here as the already
        # source-normalized token snapshot_from_mtconnect_execution()
        # produces for MTConnect's ACTIVE/EXECUTING, not a raw GRBL token.
        if state in {"RUN", "RUNNING", "JOG", "HOME"}:
            return MachineState.RUNNING
        # GRBL's real "Hold" state means execution is intentionally paused
        # (feed hold), not actively running - collapsing it into RUNNING
        # hid a real, distinct condition. HOLDING already exists in the
        # shared SDK for exactly this; this does not change any dispatch
        # decision (evaluate_job() only allows productive work on IDLE
        # either way) but makes the reported state accurate, matching the
        # same real print_stats.state=paused -> HOLDING fix already made
        # in the sibling PRINTER3D bridge.
        if state == "HOLD":
            return MachineState.HOLDING
        # GRBL's real "Alarm" state is its most safety-critical report - a
        # limit-switch trip, lost position, or unresolved E-STOP - and was
        # previously swallowed by the OFFLINE fallback below, indistinguishable
        # from "controller not reporting". "Door" is GRBL's own real safety-
        # interlock state; this bridge already has a separate door_closed
        # input, so recognizing GRBL's own "Door" token too is a defensive,
        # belt-and-suspenders second signal, not a replacement for it.
        if state in {"FAULT", "ERROR", "OFF", "ALARM"}:
            return MachineState.FAULT
        if state == "DOOR":
            return MachineState.SAFE_STOP
        return MachineState.OFFLINE


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
