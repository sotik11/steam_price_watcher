@echo off
rem ============================================================================
rem  build_installer.bat - compile the distributable Setup .exe.
rem
rem  Steps:
rem    1. Download the official Python installer for PYVER (cached - skipped if
rem       already present in installer\python\).
rem    2. Run Inno Setup's ISCC.exe on installer.iss, producing
rem       installer\dist\SteamCardWatch-Setup-<ver>.exe.
rem
rem  WHEN RELEASING A NEW VERSION: bump PYVER below to the latest compatible
rem  Python 3.13.x (check https://www.python.org/downloads/), and bump
rem  MyAppVersion + PyVersion in installer.iss to match. Keep them on 3.13.x -
rem  3.14 may lack prebuilt wheels for our deps.
rem ============================================================================
setlocal
cd /d "%~dp0"

set "PYVER=3.13.7"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
set "PYDIR=python"
set "PYEXE=%PYDIR%\python-%PYVER%-amd64.exe"

rem --- locate ISCC.exe (winget installs it per-user) ------------------------
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo [ERROR] ISCC.exe not found. Install Inno Setup 6:
    echo         winget install JRSoftware.InnoSetup
    pause
    exit /b 1
)

rem --- 1. download bundled Python (cached) -----------------------------------
if not exist "%PYDIR%" mkdir "%PYDIR%"
if exist "%PYEXE%" (
    echo [1/2] Python %PYVER% installer already cached.
) else (
    echo [1/2] Downloading Python %PYVER% ...
    powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYEXE%' -UseBasicParsing } catch { exit 1 }"
    if not exist "%PYEXE%" (
        echo [ERROR] Download failed. Check the version/URL or your connection.
        pause
        exit /b 1
    )
)

rem --- 2. compile -----------------------------------------------------------
echo [2/2] Compiling installer ...
"%ISCC%" installer.iss
if errorlevel 1 (
    echo [ERROR] ISCC compilation failed - see messages above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done. Installer is in: installer\dist\
echo ============================================================
pause
exit /b 0
