@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py sm2ddr_gui.py) || (python sm2ddr_gui.py)
if errorlevel 1 (
  echo.
  echo Could not start. Python 3 may not be installed.
  pause
)
