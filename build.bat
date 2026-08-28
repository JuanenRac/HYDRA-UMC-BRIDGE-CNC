@echo off
REM =============================================================================
REM HYDRA-UMC-BRIDGE-CNC - Incremental build workflow
REM Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
REM GPL-3.0-or-later - see LICENSE
REM =============================================================================
setlocal
cd /d "%~dp0"
echo.
echo *******************************************************************************
echo * HYDRA-UMC-BRIDGE-CNC - build.bat / INCREMENTAL BUILD
echo * JuanenRac ^(Electro Hobby 3D^) - electrohobby3d@gmail.com - GPL-3.0-or-later
echo * 1. Validate non-mutating safety tests.  2. Synchronize version and CHANGELOG.
echo *******************************************************************************
where py >nul 2>&1
if errorlevel 1 (python tools\build_test.py) else (py -3 tools\build_test.py)
if errorlevel 1 goto :error
where py >nul 2>&1
if errorlevel 1 (python tools\bump_version.py) else (py -3 tools\bump_version.py)
if errorlevel 1 goto :error
echo BUILD=PASS. Version, manifest and CHANGELOG were synchronized.
pause
exit /b 0
:error
echo BUILD FAILED. No version increment follows a failed validation.
pause
exit /b 1
