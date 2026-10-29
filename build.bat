@echo off
rem PRETTY_RUN_BEGIN
if not defined PRETTY_MODE set "PRETTY_MODE=build"
set "PRETTY_PY=python"
where py >nul 2>&1 && set "PRETTY_PY=py -3"
if not defined PRETTY_RUN if exist "%~dp0tools\pretty_run.py" (
    set "PRETTY_RUN=1"
    %PRETTY_PY% "%~dp0tools\pretty_run.py" --wrap --mode %PRETTY_MODE% -- cmd /d /c "%~f0" %*
    exit /b
)
rem PRETTY_RUN_END
REM =============================================================================
REM HYDRA-UMC-BRIDGE-CNC - Incremental build workflow
REM Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
REM GPL-3.0-or-later - see LICENSE
REM =============================================================================
setlocal
rem Keep the window open for people, never in automation (CI / HYDRA_UMC_CI).
set "KEEP_OPEN=pause"
if defined CI set "KEEP_OPEN=ver >nul"
if "%HYDRA_UMC_CI%"=="1" set "KEEP_OPEN=ver >nul"
cd /d "%~dp0"
echo.
echo *******************************************************************************
echo * HYDRA-UMC-BRIDGE-CNC - build.bat / INCREMENTAL BUILD
echo * JuanenRac ^(Electro Hobby 3D^) - electrohobby3d@gmail.com - GPL-3.0-or-later
echo * 1. Validate non-mutating safety tests.  2. Building never changes the version (explicit bump command).
echo *******************************************************************************
where py >nul 2>&1
if errorlevel 1 (python tools\build_test.py) else (py -3 tools\build_test.py)
if errorlevel 1 goto :error
where py >nul 2>&1
if errorlevel 1 goto :error
for /f "usebackq delims=" %%V in (`python -c "import json; print(json.load(open(r'%~dp0hydra-umc.project.json', encoding='utf-8'))['version'])"`) do set "HYDRA_UMC_VERSION_BUILT=%%V"
echo BUILD=PASS. Built v%HYDRA_UMC_VERSION_BUILT%. Building never changes the version; to raise it run: python tools\hydra_project_tool.py bump
%KEEP_OPEN%
exit /b 0
:error
echo BUILD FAILED.
%KEEP_OPEN%
exit /b 1
