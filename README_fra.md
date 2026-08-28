<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - Pont de coordination de cellule CNC
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-CNC

🇺🇸 [English](README.md) | 🇪🇸 [Español](README_spa.md) | 🇫🇷 **Français** | 🇮🇹 [Italiano](README_ita.md) | 🇩🇪 [Deutsch](README_deu.md) | 🇨🇳 [简体中文](README_zho.md) | 🇯🇵 [日本語](README_jpn.md)

🇺🇸 [English](README.md) | 🇪🇸 [Español](README_spa.md) | 🇫🇷 **Français** | 🇮🇹 [Italiano](README_ita.md) | 🇩🇪 [Deutsch](README_deu.md) | 🇨🇳 [简体中文](README_zho.md) | 🇯🇵 [日本語](README_jpn.md)

Pont de haut niveau pour les cellules CNC et les auxiliaires robotiques
HYDRA-UMC : chargement, déchargement, manutention et tâches auxiliaires
supervisées. Il ne devient jamais un contrôleur de trajectoire temps réel.

## Architecture

```text
Contrôleur CNC/HAL <-> BRIDGE-CNC <-> SDK <-> SERVER <-> MCU et sécurité de cellule
```

Le noyau transforme l'état observé du contrôleur, l'arrêt d'urgence et l'état
de porte en un état de machine sûr par défaut. Une porte ouverte ou un arrêt
d'urgence actif bloque toujours le travail auxiliaire productif. Le contrôleur
conserve l'autorité sur la trajectoire, la broche et les limites machine.

## Compilation et test

Exécutez `build-test.bat` sous Windows ou `bash build-test.sh` sous Linux. Le
test ne modifie rien et vérifie l'admission au repos sûr, le rejet porte ouverte
et le transfert de l'annulation. Un adaptateur LinuxCNC HAL reste une étape
d'intégration matériel/logiciel, pas une promesse de ce noyau local.

## Projets associés

| Projet | Rôle |
| --- | --- |
| [HYDRA-UMC-SDK](https://github.com/JuanenRac/HYDRA-UMC-SDK) | Porte de travail partagée. |
| [HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER) | Coordinateur authentifié. |
| [HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE) | Future preuve de contrôleur. |

## État

La version `0.0.1` fournit un coordinateur local testé et sûr par défaut.
Aucune CNC réelle, broche HAL ni robot n'a été piloté.
