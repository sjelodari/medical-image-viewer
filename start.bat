@echo off
REM Medical Image Viewer - double-click launcher for Windows.
REM On first run it sets up everything automatically; later runs start instantly.

cd /d "%~dp0"

REM 1. Find Python (prefer the py launcher, then python on PATH)
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo   Python 3.10 or newer is required but was not found.
  echo   Please install it from https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH" during setup, then run this again.
  echo.
  pause
  exit /b 1
)

REM 2. First-time setup: create the environment and install dependencies
if not exist ".venv\" (
  echo First-time setup: creating environment ^(this can take a few minutes^)...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Could not create the Python environment.
    pause
    exit /b 1
  )
)
if not exist ".venv\.deps_ok" (
  echo Installing dependencies... please wait.
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Could not install the required packages. Check your internet connection.
    pause
    exit /b 1
  )
  type nul > ".venv\.deps_ok"
)

REM 3. Launch
echo.
echo ===============================================
echo   Medical Image Viewer is starting - your browser will
echo   open automatically in a moment.
echo.
echo   Keep this window open while you work.
echo   To stop: click "Quit" in the app, or just
echo   close this window.
echo ===============================================
echo.
".venv\Scripts\python.exe" app.py
pause
