@echo off
rem Launch GUI without console window (production launcher).
rem Uses the project's local venv so all dependencies resolve.
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0gui.pyw"
