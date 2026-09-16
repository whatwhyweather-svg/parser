@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo  Threads Posts ARTFrance - setup
echo ========================================
echo.

where py >nul 2>&1
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>&1
  if %errorlevel%==0 (
    set "PY=python"
  ) else (
    echo [ERR] Python not found. Install Python 3.11+ from python.org
    echo       Enable "Add python.exe to PATH".
    pause
    exit /b 1
  )
)

echo [1/4] Python...
%PY% --version
if errorlevel 1 (
  echo [ERR] Python failed to start
  pause
  exit /b 1
)

echo.
echo [2/4] venv...
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [ERR] venv not created
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"

echo.
echo [3/4] pip + requirements...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERR] pip install failed
  pause
  exit /b 1
)

echo.
echo [4/4] Playwright Chromium...
set "PLAYWRIGHT_BROWSERS_PATH="
python -m playwright install chromium
if errorlevel 1 (
  echo [ERR] playwright install failed
  pause
  exit /b 1
)

if not exist ".env" (
  if exist ".env.example" (
    copy /Y ".env.example" ".env" >nul
    echo.
    echo [!] Created .env from .env.example - fill TELEGRAM / DEEPSEEK
  )
)

echo.
echo ========================================
echo  Done.
echo  1) Edit .env
echo  2) Run start.bat
echo  3) In the window: login Threads, then START
echo ========================================
pause
