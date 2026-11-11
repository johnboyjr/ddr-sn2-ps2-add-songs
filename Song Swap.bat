@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py sn2_gui.py) || (python sn2_gui.py)
if errorlevel 1 (
  echo.
  echo Could not start. Python 3 may not be installed.
  pause
)
