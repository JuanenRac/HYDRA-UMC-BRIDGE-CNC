<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - CNC 单元协调桥接
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-CNC

🇺🇸 [English](README.md) | 🇪🇸 [Español](README_spa.md) | 🇫🇷 [Français](README_fra.md) | 🇮🇹 [Italiano](README_ita.md) | 🇩🇪 [Deutsch](README_deu.md) | 🇨🇳 **简体中文** | 🇯🇵 [日本語](README_jpn.md)

面向 CNC 单元和 HYDRA-UMC 机器人辅助设备的高级桥接：上料、下料、零件搬运和
受监督的辅助任务。它绝不会成为实时轨迹控制器。

## 架构

```text
CNC 控制器/HAL <-> BRIDGE-CNC <-> SDK <-> SERVER <-> MCU 与单元安全
```

核心将观测到的控制器状态、急停和门状态转换为默认安全的机器状态。门打开或
急停触发总会阻止生产性辅助工作。控制器保留对轨迹、主轴和机器限位的权威。

## 构建与测试

Windows 运行 `build-test.bat`，Linux 运行 `bash build-test.sh`。该测试不修改
任何内容，验证安全空闲许可、开门拒绝和中止转发。LinuxCNC HAL 适配器仍是
软硬件集成步骤，并非此本地核心已经实现的声明。

## 相关项目

| 项目 | 作用 |
| --- | --- |
| [HYDRA-UMC-SDK](https://github.com/JuanenRac/HYDRA-UMC-SDK) | 共享作业门。 |
| [HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER) | 已认证协调器。 |
| [HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE) | 未来的控制器证据。 |

## 状态

版本 `0.0.1` 提供本地测试的默认安全协调器。尚未驱动真实 CNC、HAL 引脚或机器人。

## ⚙️ 版本化构建

`build-test.bat` / `build-test.sh` 只验证，不修改仓库。`build.bat` /
`build.sh` 先运行该验证，只有成功后才同步原生包版本、清单和 `CHANGELOG.md`。
在控制器集成验证前，不提供 CNC `run` 命令。
