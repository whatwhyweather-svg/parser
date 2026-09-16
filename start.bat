@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first
  pause
  exit /b 1
)

if not exist ".env" (
  echo Missing .env - copy .env.example to .env
  pause
  exit /b 1
)

set "PLAYWRIGHT_BROWSERS_PATH="
".venv\Scripts\python.exe" windows_supervisor.py
if errorlevel 1 pause
