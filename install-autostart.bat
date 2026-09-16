@echo off
cd /d "%~dp0"

set "TASK=ARTFrance-ThreadsParser"
schtasks /Create /TN "%TASK%" /SC ONLOGON /RL LIMITED /F /TR "\"%~dp0start.bat\""
if errorlevel 1 (
  echo Failed to create autostart task.
  pause
  exit /b 1
)

echo OK: start.bat will run at Windows logon.
echo Starting now.
start "" "%~dp0start.bat"
pause
