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

rem  All progress goes to setup_env.log next to this script, so a failed
rem  install (which runs hidden via the installer) leaves a trail to read.
set "LOG=%~dp0setup_env.log"
echo ==== setup_env %DATE% %TIME% ==== > "%LOG%"

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
echo [ERROR] Python 3.13 not found.>> "%LOG%"
exit /b 1

:have_py
echo [setup_env] Using Python: %PYEXE%>> "%LOG%"

rem --- create venv if missing ------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [setup_env] Creating virtual environment in .venv ...>> "%LOG%"
    %PYEXE% -m venv .venv >> "%LOG%" 2>&1
    if errorlevel 1 (
        echo [ERROR] venv creation failed.>> "%LOG%"
        exit /b 1
    )
) else (
    echo [setup_env] Reusing existing .venv>> "%LOG%"
)

rem --- install / update dependencies ----------------------------------------
echo [setup_env] Upgrading pip ...>> "%LOG%"
".venv\Scripts\python.exe" -m pip install --upgrade pip --disable-pip-version-check -q >> "%LOG%" 2>&1
echo [setup_env] Installing requirements ...>> "%LOG%"
".venv\Scripts\python.exe" -m pip install -r requirements.txt --disable-pip-version-check >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] dependency installation failed.>> "%LOG%"
    exit /b 1
)

rem  Optional deps (rookiepy) - best effort, must NOT fail the whole setup.
rem  rookiepy is a Rust extension with no prebuilt wheel on some Python
rem  versions; building from source needs Rust + the MSVC linker (link.exe),
rem  absent on a clean PC - that's the 3-minute hang-then-crash we hit.
rem  --only-binary=:all: tells pip to use a wheel or skip, never compile.
rem  The app imports rookiepy inside try/except, so missing it is harmless
rem  (only Chrome/Edge/Opera App-Bound cookie extraction is unavailable).
if exist "requirements-optional.txt" (
    echo [setup_env] Installing optional deps ^(best effort^) ...>> "%LOG%"
    if exist "wheels" (
        rem  Install strictly from the bundled wheels\ folder - offline, no
        rem  PyPI, no compilation. rookiepy ships here as a prebuilt wheel
        rem  (PyPI has none for 3.13). A platform/version mismatch just makes
        rem  pip skip; we ignore the failure either way.
        ".venv\Scripts\python.exe" -m pip install -r requirements-optional.txt --no-index --find-links "%~dp0wheels" --disable-pip-version-check >> "%LOG%" 2>&1
    ) else (
        ".venv\Scripts\python.exe" -m pip install -r requirements-optional.txt --only-binary=:all: --disable-pip-version-check >> "%LOG%" 2>&1
    )
    if errorlevel 1 (
        echo [setup_env] Optional deps skipped - OK ^(app works without them^).>> "%LOG%"
    )
)

echo [setup_env] Done.>> "%LOG%"
exit /b 0
