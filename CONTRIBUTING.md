<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - Contribution guide
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# Contributing

Keep this bridge outside the real-time motion loop: the CNC controller retains
trajectory, spindle and machine-limit authority. Every observed state mapping
must fail closed, and auxiliary work must pass the shared SDK job gate.

Before opening a change, run `build-test.bat` on Windows or `bash build-test.sh`
on Linux. Add a focused test for every guard or admission rule changed. Hardware
behavior must identify its tested controller interface and safe failure mode;
untested CNC/HAL support is not ready support.
