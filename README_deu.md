<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - CNC-Zellenkoordinierungsbruecke
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-CNC

🇺🇸 [English](README.md) | 🇪🇸 [Español](README_spa.md) | 🇫🇷 [Français](README_fra.md) | 🇮🇹 [Italiano](README_ita.md) | 🇩🇪 **Deutsch** | 🇨🇳 [简体中文](README_zho.md) | 🇯🇵 [日本語](README_jpn.md)

Brücke auf hoher Ebene für CNC-Zellen und robotische HYDRA-UMC-
Hilfseinrichtungen: Beladen, Entladen, Teilehandhabung und überwachte
Hilfsaufgaben. Sie wird niemals zu einer Echtzeit-Trajektoriensteuerung.

## Architektur

```text
CNC-Steuerung/HAL <-> BRIDGE-CNC <-> SDK <-> SERVER <-> MCU und Zellsicherheit
```

Der Kern wandelt den beobachteten Steuerungszustand, E-STOP und Türzustand in
einen ausfallsicheren Maschinenzustand um. Eine offene Tür oder ein aktiver
E-STOP blockiert stets produktive Hilfsarbeit. Die Steuerung behält die
Hoheit über Trajektorie, Spindel und Maschinengrenzen.

## Build & Test

Unter Windows `build-test.bat` oder unter Linux `bash build-test.sh` ausführen.
Der Test verändert nichts und prüft sichere Leerlaufzulassung, Ablehnung bei
offener Tür und Abbruchweiterleitung. Ein LinuxCNC-HAL-Adapter bleibt ein
Hardware-/Software-Integrationsschritt und keine Zusage dieses lokalen Kerns.

## Verwandte Projekte

| Projekt | Rolle |
| --- | --- |
| [HYDRA-UMC-SDK](https://github.com/JuanenRac/HYDRA-UMC-SDK) | Gemeinsames Arbeitsgatter. |
| [HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER) | Authentifizierter Koordinator. |
| [HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE) | Zukünftiger Steuerungsnachweis. |

## Status

Version `0.0.1` stellt einen lokal getesteten ausfallsicheren Koordinator
bereit. Keine reale CNC, kein HAL-Pin und kein Roboter wurde angesteuert.

## ⚙️ Versionierter Build

`build-test.bat` / `build-test.sh` validieren ohne das Repository zu ändern.
`build.bat` / `build.sh` führen zuerst diese Validierung aus und
synchronisieren nur bei Erfolg native Version, Manifest und `CHANGELOG.md`.
Vor einer validierten Steuerungsintegration gibt es keinen CNC-`run`-Befehl.
