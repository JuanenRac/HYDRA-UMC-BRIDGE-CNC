<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - CNC cell coordination bridge
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-CNC

🇺🇸 **English** | 🇪🇸 [Español](README_spa.md) | 🇫🇷 [Français](README_fra.md) | 🇮🇹 [Italiano](README_ita.md) | 🇩🇪 [Deutsch](README_deu.md) | 🇨🇳 [简体中文](README_zho.md) | 🇯🇵 [日本語](README_jpn.md)

High-level bridge for CNC cells and HYDRA-UMC robot auxiliaries: loading,
unloading, part handling and supervised auxiliary tasks. It never becomes a
real-time trajectory controller.

## Architecture

```text
CNC controller/HAL <-> BRIDGE-CNC <-> SDK <-> SERVER <-> MCU and cell safety
```

The core converts observed controller state, E-STOP and door state into a
fail-safe machine state. An open door or asserted E-STOP always blocks
productive auxiliary work. The controller retains trajectory, spindle and
machine-limit authority.

## Build & Test

Run `build-test.bat` on Windows or `bash build-test.sh` on Linux. The test is
non-mutating and proves safe-idle admission, open-door rejection and abort
forwarding. A LinuxCNC HAL adapter remains a hardware/software integration
step, not a claim made by this local core.

## Related Projects

| Project | Role |
| --- | --- |
| [HYDRA-UMC-SDK](https://github.com/JuanenRac/HYDRA-UMC-SDK) | Shared job gate. |
| [HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER) | Authenticated coordinator. |
| [HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE) | Future controller evidence. |

## Status

Version `0.0.1` provides a locally tested fail-safe coordinator. No real CNC,
HAL pin or robot has been driven.

## ⚙️ Versioned Build

`build-test.bat` / `build-test.sh` validate without modifying the repository.
`build.bat` / `build.sh` run that validation first and, only on success,
synchronize the native package version, manifest and `CHANGELOG.md`. There is
no live CNC `run` command until a controller integration is validated.
