@echo off
rem ============================================================================
rem  setup.bat - fallback / no-installer setup.
rem
rem  Use this when you copied the project folder by hand instead of running
rem  the .exe installer. It:
rem    1. checks for Python 3.13; if missing, downloads the official installer
rem       from python.org and installs it silently (per-user, no admin);
rem    2. creates the .venv and installs dependencies (delegates to
rem       setup_env.bat - single source of truth for that step);
rem    3. drops a "Steam Card Price Watch" shortcut on the current user's
rem       Desktop pointing at the GUI.
rem
rem  Safe to re-run: every step is idempotent.
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PYVER=3.13.7"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"

echo ============================================================
echo   Steam Card Price Watch - setup
echo ============================================================
echo.

rem --- 1. Python -------------------------------------------------------------
py -3.13 --version >nul 2>&1
if %errorlevel%==0 (
    echo [1/3] Python 3.13 found.
    goto env
)
where python >nul 2>&1
if %errorlevel%==0 (
    echo [1/3] Python found on PATH.
    goto env
)

echo [1/3] Python 3.13 not found - downloading %PYVER% ...
set "PYINST=%TEMP%\python-%PYVER%-amd64.exe"
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYINST%' -UseBasicParsing } catch { exit 1 }"
if not exist "%PYINST%" (
    echo [ERROR] Could not download Python. Check your internet connection,
    echo         or install Python 3.13 manually from python.org, then re-run.
    pause
    exit /b 1
)
echo       Installing Python silently (per-user) ...
"%PYINST%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1
del "%PYINST%" >nul 2>&1

rem --- 2. venv + deps --------------------------------------------------------
:env
echo [2/3] Setting up environment and dependencies ...
call "%~dp0setup_env.bat"
if errorlevel 1 (
    echo [ERROR] Environment setup failed - see messages above.
    pause
    exit /b 1
)

rem --- 3. Desktop shortcut ---------------------------------------------------
echo [3/3] Creating Desktop shortcut ...
set "TARGET=%~dp0.venv\Scripts\pythonw.exe"
set "GUISCRIPT=%~dp0gui.pyw"
set "ICON=%~dp0icon.ico"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Steam Card Price Watch.lnk');" ^
  "$s.TargetPath='%TARGET%';" ^
  "$s.Arguments='\"%GUISCRIPT%\"';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.IconLocation='%ICON%';" ^
  "$s.Save()"

echo.
echo ============================================================
echo   Done. Launch from the Desktop shortcut or gui.bat.
echo ============================================================
pause
exit /b 0
