@echo off
rem ============================================================================
rem  setup_env.bat - create / update the project's virtual environment.
rem
rem  Used in two places:
rem    * by the Inno Setup installer, right after it (optionally) installs
rem      Python - it passes the known python.exe path as %1;
rem    * standalone, when called from setup.bat or by hand - then no arg is
rem      given and we locate Python ourselves.
rem
rem  Idempotent: if .venv already exists we reuse it and just (re)install the
rem  requirements, so running it again after a code/deps update is safe and
rem  cheap. Runs from its own directory regardless of where it was invoked.
rem ============================================================================
setlocal
cd /d "%~dp0"

rem --- locate a Python interpreter ------------------------------------------
rem  Priority: explicit arg (from the installer) > py -3.13 launcher >
rem  plain python on PATH > common fixed install locations. We need a SYSTEM
rem  Python here to build the venv from - not the venv's own python.
set "PYEXE=%~1"
if not "%PYEXE%"=="" goto have_py

py -3.13 --version >nul 2>&1
if %errorlevel%==0 (
    set "PYEXE=py -3.13"
    goto have_py
)

where python >nul 2>&1
if %errorlevel%==0 (
    set "PYEXE=python"
    goto have_py
)

if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    goto have_py
)
if exist "%ProgramFiles%\Python313\python.exe" (
    set "PYEXE=%ProgramFiles%\Python313\python.exe"
    goto have_py
)
if exist "C:\Python313\python.exe" (
    set "PYEXE=C:\Python313\python.exe"
    goto have_py
)

echo [ERROR] Python 3.13 not found. Install it from python.org or run the full
echo         installer (it bundles Python).
exit /b 1

:have_py
echo [setup_env] Using Python: %PYEXE%

rem --- create venv if missing ------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [setup_env] Creating virtual environment in .venv ...
    %PYEXE% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] venv creation failed.
        exit /b 1
    )
) else (
    echo [setup_env] Reusing existing .venv
)

rem --- install / update dependencies ----------------------------------------
echo [setup_env] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --disable-pip-version-check -q
echo [setup_env] Installing requirements ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo [ERROR] dependency installation failed.
    exit /b 1
)

echo [setup_env] Done.
exit /b 0
