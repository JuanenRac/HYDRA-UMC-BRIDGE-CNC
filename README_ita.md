<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - Ponte di coordinamento cella CNC
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-CNC

🇺🇸 [English](README.md) | 🇪🇸 [Español](README_spa.md) | 🇫🇷 [Français](README_fra.md) | 🇮🇹 **Italiano** | 🇩🇪 [Deutsch](README_deu.md) | 🇨🇳 [简体中文](README_zho.md) | 🇯🇵 [日本語](README_jpn.md)

🇺🇸 [English](README.md) | 🇪🇸 [Español](README_spa.md) | 🇫🇷 [Français](README_fra.md) | 🇮🇹 **Italiano** | 🇩🇪 [Deutsch](README_deu.md) | 🇨🇳 [简体中文](README_zho.md) | 🇯🇵 [日本語](README_jpn.md)

Ponte ad alto livello per celle CNC e ausiliari robotici HYDRA-UMC: carico,
scarico, movimentazione dei pezzi e compiti ausiliari supervisionati. Non
diventa mai un controllore di traiettoria in tempo reale.

## Architettura

```text
Controllore CNC/HAL <-> BRIDGE-CNC <-> SDK <-> SERVER <-> MCU e sicurezza cella
```

Il nucleo converte lo stato osservato del controllore, l'E-STOP e lo stato
della porta in uno stato macchina sicuro per impostazione predefinita. Una
porta aperta o un E-STOP attivo bloccano sempre il lavoro ausiliario
produttivo. Il controllore mantiene l'autorità su traiettoria, mandrino e
limiti macchina.

## Build e test

Eseguire `build-test.bat` su Windows o `bash build-test.sh` su Linux. Il test
non modifica nulla e verifica l'ammissione a riposo sicuro, il rifiuto con
porta aperta e l'inoltro dell'interruzione. Un adattatore LinuxCNC HAL rimane
una fase di integrazione hardware/software, non un'affermazione di questo
nucleo locale.

## Progetti correlati

| Progetto | Ruolo |
| --- | --- |
| [HYDRA-UMC-SDK](https://github.com/JuanenRac/HYDRA-UMC-SDK) | Porta di lavoro condivisa. |
| [HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER) | Coordinatore autenticato. |
| [HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE) | Futura evidenza del controllore. |

## Stato

La versione `0.0.1` fornisce un coordinatore locale testato e sicuro per
impostazione predefinita. Non sono stati azionati una CNC reale, un pin HAL o
un robot.
