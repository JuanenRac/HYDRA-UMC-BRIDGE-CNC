# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - CNC cell boundary
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Translate controller observations to a safe high-level coordination gate."""

from __future__ import annotations

from dataclasses import dataclass

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, GateDecision, MachineState, evaluate_job


@dataclass(frozen=True)
class CncSnapshot:
    controller_state: str
    estop: bool
    door_closed: bool

    def machine_state(self) -> MachineState:
        if self.estop or not self.door_closed:
            return MachineState.SAFE_STOP
        if not isinstance(self.controller_state, str):
            return MachineState.OFFLINE
        state = self.controller_state.upper()
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
    """Permit robot auxiliary work only beside an idle, safeguarded CNC."""

    def plan(self, job: BridgeJob, cell_state: CellState, cnc: CncSnapshot) -> GateDecision:
        observed = BridgeJob(job.job_id, job.idempotency_key, job.source, job.phase, cnc.machine_state(), job.parameters)
        return evaluate_job(observed, cell_state)
