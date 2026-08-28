#!/usr/bin/env python3
# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Non-mutating build verification
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
from __future__ import annotations
import os, py_compile, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
os.environ["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT.parent / "HYDRA-UMC-SDK" / "clients" / "python" / "src")))
for source in (ROOT / "src").rglob("*.py"): py_compile.compile(str(source), doraise=True)
subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT, env=os.environ.copy(), check=True)
print("BUILD_TEST=PASS versioning=unchanged changelog=unchanged")
