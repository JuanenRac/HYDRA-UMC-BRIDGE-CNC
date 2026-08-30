<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - Technical bridge guide
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-CNC Technical Guide

## Scope and operating model

`CncSnapshot` makes a CNC state usable by the shared SDK gate only after independent E-STOP and door evidence are present. `observation.py` normalizes an already-saved mapping or supplied GRBL status line. Missing or non-Boolean E-STOP/door values fail closed. The bridge coordinates auxiliary loading/unloading; it does not control trajectory, spindle, axes or G-code.

## Compatible software

The current parser accepts generic controller-state evidence, the leading state token of a supplied GRBL status line, and saved MTConnect `execution` values (`READY`/`STOPPED`, `ACTIVE`/`EXECUTING`, `FEED_HOLD`/`INTERRUPTED`, `FAULT`). MTConnect is read-only evidence only: it neither opens a controller connection nor offers a command path. LinuxCNC/HAL is an intended future controller integration, but is not connected today. Other controllers can be supported only through a documented read-only adapter that preserves independent guard and E-STOP signals.

## Scripts and verification

| Script | Purpose | Changes version/CHANGELOG? |
|---|---|---|
| `build-test.bat` / `build-test.sh` | Compile and run local safety tests | No |
| `build.bat` / `build.sh` | Validate, then increment version and CHANGELOG | Yes, after success |
| `tools/inspect_controller_evidence.py` | Normalize a saved JSON capture only | No |

## Adding a new script

Add the standard header, explain the safe scope, number console steps and use `pause` in `.bat`. Place parsing logic under test, compile `tools/` in the build check and document every command. No script may open a controller, emit G-code or change HAL state as a side effect.

## Hardware acceptance gate

Identify the controller and interface, bind evidence to that controller, validate E-STOP/door signals independently, test stale/offline behavior, conduct dry auxiliary-cell trials and complete HIL before any command path exists. The native CNC controller remains authoritative.
