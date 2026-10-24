# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Public package interface
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Safe high-level CNC cell coordination; never a real-time motion driver."""

from .cell import CncCellBridge, CncSnapshot, spindle_gate_from_environment
from .cncjs_link import CncjsController, SimulatedCncjs, open_cncjs, snapshot_from_cncjs_state
from .controller import ControllerLink, GrblController, ReadOnlyController
from .linuxcnc_link import LinuxcncController, SimulatedLinuxcnc, open_linuxcnc, snapshot_from_linuxcnc_stat
from .mtconnect_publish import ShdrAdapter, observations_from_snapshot, observations_payload, shdr_lines
from .mqtt_transport import CncMqttBridge, MqttPublish, connect_with_retry, run_forever
from .observation import snapshot_from_grbl_status, snapshot_from_mapping, snapshot_from_mtconnect_execution
from .serial_transport import (
    GrblRealtimeControl,
    GrblSerialProbe,
    ReconnectingSerialConnection,
    RealtimeCommandResult,
    SerialLike,
    open_serial_port,
    open_serial_port_by_id,
    resolve_serial_device_path,
)

__all__ = [
    "CncCellBridge",
    "CncSnapshot",
    "spindle_gate_from_environment",
    "ControllerLink",
    "GrblController",
    "ReadOnlyController",
    "LinuxcncController",
    "SimulatedLinuxcnc",
    "open_linuxcnc",
    "snapshot_from_linuxcnc_stat",
    "CncjsController",
    "SimulatedCncjs",
    "open_cncjs",
    "snapshot_from_cncjs_state",
    "ShdrAdapter",
    "observations_from_snapshot",
    "observations_payload",
    "shdr_lines",
    "snapshot_from_mapping",
    "snapshot_from_grbl_status",
    "snapshot_from_mtconnect_execution",
    "GrblSerialProbe",
    "GrblRealtimeControl",
    "RealtimeCommandResult",
    "SerialLike",
    "open_serial_port",
    "open_serial_port_by_id",
    "resolve_serial_device_path",
    "ReconnectingSerialConnection",
    "CncMqttBridge",
    "MqttPublish",
    "connect_with_retry",
    "run_forever",
]
