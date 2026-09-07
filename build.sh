#!/usr/bin/env bash
# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Incremental build workflow
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"
trap '[ -t 0 ] && read -r -p "Press Enter to close..." _' EXIT
printf '%s\n' '*******************************************************************************' '* HYDRA-UMC-BRIDGE-CNC - build.sh / INCREMENTAL BUILD' '* JuanenRac (Electro Hobby 3D) - electrohobby3d@gmail.com - GPL-3.0-or-later' '* 1. Validate non-mutating safety tests.  2. Synchronize version and CHANGELOG.' '*******************************************************************************'
python3 tools/build_test.py
python3 tools/bump_version.py
echo 'BUILD=PASS. Version, manifest and CHANGELOG were synchronized.'
