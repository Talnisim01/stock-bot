@echo off
chcp 65001 >nul
cd /d "%~dp0"
title MyRupeeBot - keep this window open
set PYTHONIOENCODING=utf-8
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (python --version >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python is not installed. Opening the download page...
  start "" https://www.python.org/downloads/
  pause
  exit /b
)
%PY% -c "import curl_cffi, tzdata" >nul 2>nul || (
  echo Installing browser engine - one time only...
  %PY% -m pip install --disable-pip-version-check --no-warn-script-location -q curl_cffi tzdata
)
:run
%PY% bot.py --loop
if errorlevel 3 if not errorlevel 4 (pause & exit /b)
echo Bot stopped - restarting in 10 seconds...
timeout /t 10 >nul
goto run
