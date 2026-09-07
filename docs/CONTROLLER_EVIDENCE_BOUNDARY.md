<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - Controller evidence boundary
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# Controller Evidence Boundary

`snapshot_from_mapping()` and `snapshot_from_grbl_status()` transform evidence already collected by another layer into `CncSnapshot`. They do not open serial ports, connect to LinuxCNC/HAL, execute G-code, control a spindle or move an axis.

The minimum evidence for an idle snapshot is a literal controller state plus two real Boolean signals: `estop: false` and `door_closed: true`. Missing, numeric or text-like Boolean values fail closed. The GRBL helper reads only the leading state token from a supplied status line and still requires the two independent safety signals.

Any future controller adapter must authenticate/identify its controller, preserve independent E-STOP and guard authority, and complete bench/HIL validation before it can be connected to a real CNC cell.

For an offline review, run `py tools/inspect_controller_evidence.py evidence.json`. It reads a saved JSON mapping only and emits the canonical SDK machine state; it never opens a serial, HAL or network link.
