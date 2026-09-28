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
:run
%PY% bot.py --loop
if errorlevel 3 if not errorlevel 4 (pause & exit /b)
echo Bot stopped - restarting in 10 seconds...
timeout /t 10 >nul
goto run
