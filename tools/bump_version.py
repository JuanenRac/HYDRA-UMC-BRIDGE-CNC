#!/usr/bin/env python3
# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Standalone build version synchronizer
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Synchronize pyproject, manifest and CHANGELOG after a successful build."""
from __future__ import annotations
import json, re
from datetime import date
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent; MANIFEST=ROOT/'hydra-umc.project.json'; PYPROJECT=ROOT/'pyproject.toml'; CHANGELOG=ROOT/'CHANGELOG.md'
PATTERN=re.compile(r'(?m)^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"\s*$')
def next_version(value: str) -> str:
    major, minor, patch=(int(part) for part in value.split('.')); patch+=1
    if patch==10: minor, patch=minor+1, 0
    if minor==10: major, minor=major+1, 0
    return f'{major}.{minor}.{patch}'
def main() -> int:
    manifest=json.loads(MANIFEST.read_text(encoding='utf-8')); declared=manifest.get('version'); pyproject=PYPROJECT.read_text(encoding='utf-8'); match=PATTERN.search(pyproject)
    if not isinstance(declared,str) or match is None: raise SystemExit('ERROR: manifest or pyproject native version is invalid')
    native='.'.join(match.group(index) for index in (1,2,3))
    if native != declared: raise SystemExit(f'ERROR: native version {native} differs from manifest {declared}; repair the mismatch before building')
    updated=next_version(native); pyproject=pyproject[:match.start()]+f'version = "{updated}"'+pyproject[match.end():]; manifest['version']=updated
    changelog=CHANGELOG.read_text(encoding='utf-8'); first=re.search(r'(?m)^## \[\d+\.\d+\.\d+\]',changelog)
    if first is None: raise SystemExit('ERROR: CHANGELOG.md has no release heading')
    entry=f'## [{updated}] - {date.today().isoformat()}\n\n- Successful incremental build: synchronized package metadata and `hydra-umc.project.json`.\n\n'
    PYPROJECT.write_text(pyproject,encoding='utf-8'); MANIFEST.write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8'); CHANGELOG.write_text(changelog[:first.start()]+entry+changelog[first.start():],encoding='utf-8')
    print(f'HYDRA-UMC-BRIDGE-CNC version: v{native} -> v{updated}')
    return 0
if __name__ == '__main__': raise SystemExit(main())
