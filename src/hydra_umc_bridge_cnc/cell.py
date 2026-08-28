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
        state = self.controller_state.upper()
        if state == "IDLE":
            return MachineState.IDLE
        if state in {"RUN", "RUNNING", "HOLD"}:
            return MachineState.RUNNING
        if state in {"FAULT", "ERROR", "OFF"}:
            return MachineState.FAULT
        return MachineState.OFFLINE


class CncCellBridge:
    """Permit robot auxiliary work only beside an idle, safeguarded CNC."""

    def plan(self, job: BridgeJob, cell_state: CellState, cnc: CncSnapshot) -> GateDecision:
        observed = BridgeJob(job.job_id, job.idempotency_key, job.source, job.phase, cnc.machine_state(), job.parameters)
        return evaluate_job(observed, cell_state)
