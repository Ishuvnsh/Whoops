@echo off
cd /d "%~dp0"
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed. Check your internet connection and Python installation.
  pause
  exit /b 1
)
py connectin_module.py
pause
