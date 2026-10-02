@echo off
rem Runs CourtPilot locally with demo data. Double-click to start; close the window to stop.
title CourtPilot preview
cd /d "%~dp0"
echo Starting CourtPilot (the first start takes about a minute)...
start "" /b cmd /c "timeout /t 12 /nobreak >nul & start http://localhost:8010"
".venv\Scripts\python.exe" scripts\preview_local.py
pause
