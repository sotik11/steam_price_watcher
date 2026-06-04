@echo off
rem Launch GUI WITH console — used for diagnosing startup errors.
rem Uses the project's local venv so all dependencies resolve.
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0gui.pyw"
echo.
echo --- GUI exited with errorlevel %errorlevel% ---
pause
