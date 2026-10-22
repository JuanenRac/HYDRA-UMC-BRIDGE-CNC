#!/usr/bin/env bash
# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Incremental build workflow
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
trap '[ -t 0 ] && read -r -p "Press Enter to close..." _' EXIT
printf '%s\n' '*******************************************************************************' '* HYDRA-UMC-BRIDGE-CNC - build.sh / INCREMENTAL BUILD' '* JuanenRac (Electro Hobby 3D) - electrohobby3d@gmail.com - GPL-3.0-or-later' '* 1. Validate non-mutating safety tests.  2. Building never changes the version (explicit bump command).' '*******************************************************************************'
python3 tools/build_test.py
HYDRA_UMC_VERSION_BUILT="$(python3 -c 'import json; print(json.load(open("hydra-umc.project.json", encoding="utf-8"))["version"])')"
printf 'BUILD=PASS. Built v%s. Building never changes the version; to raise it run: python tools/hydra_project_tool.py bump\n' "$HYDRA_UMC_VERSION_BUILT"
