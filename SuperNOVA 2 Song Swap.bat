@echo off
cd /d "%~dp0"
title SuperNOVA 2 Song Swap
python sn2_gui.py
if errorlevel 1 py sn2_gui.py
pause
