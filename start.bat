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
echo Starting ARTFrance...
".venv\Scripts\python.exe" -X utf8 -u windows_supervisor.py
set "ERR=%ERRORLEVEL%"
echo.
echo Stopped, code %ERR%
if not "%ERR%"=="0" pause
