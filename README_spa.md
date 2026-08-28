<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - Puente de coordinación de celda CNC
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-CNC

🇺🇸 [English](README.md) | 🇪🇸 **Español** | 🇫🇷 [Français](README_fra.md) | 🇮🇹 [Italiano](README_ita.md) | 🇩🇪 [Deutsch](README_deu.md) | 🇨🇳 [简体中文](README_zho.md) | 🇯🇵 [日本語](README_jpn.md)

🇺🇸 [English](README.md) | 🇪🇸 **Español** | 🇫🇷 [Français](README_fra.md) | 🇮🇹 [Italiano](README_ita.md) | 🇩🇪 [Deutsch](README_deu.md) | 🇨🇳 [简体中文](README_zho.md) | 🇯🇵 [日本語](README_jpn.md)

Puente de alto nivel para celdas CNC y auxiliares robóticos HYDRA-UMC: carga,
descarga, manipulación de piezas y tareas auxiliares supervisadas. Nunca se
convierte en un controlador de trayectorias en tiempo real.

## Arquitectura

```text
Controlador CNC/HAL <-> BRIDGE-CNC <-> SDK <-> SERVER <-> MCU y seguridad de celda
```

El núcleo convierte el estado observado del controlador, el E-STOP y el estado
de puerta en un estado de máquina seguro por defecto. Una puerta abierta o un
E-STOP activo siempre bloquean trabajo auxiliar productivo. El controlador
conserva la autoridad sobre trayectoria, husillo y límites de máquina.

## Compilación y prueba

Ejecuta `build-test.bat` en Windows o `bash build-test.sh` en Linux. La prueba
no modifica nada y verifica la admisión en reposo seguro, el rechazo con puerta
abierta y el reenvío de aborto. Un adaptador LinuxCNC HAL sigue siendo una fase
de integración de hardware/software, no una afirmación de este núcleo local.

## Proyectos relacionados

| Proyecto | Función |
| --- | --- |
| [HYDRA-UMC-SDK](https://github.com/JuanenRac/HYDRA-UMC-SDK) | Puerta de trabajo compartida. |
| [HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER) | Coordinador autenticado. |
| [HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE) | Evidencia futura del controlador. |

## Estado

La versión `0.0.1` proporciona un coordinador seguro por defecto probado en
local. No se ha accionado una CNC real, un pin HAL ni un robot.
