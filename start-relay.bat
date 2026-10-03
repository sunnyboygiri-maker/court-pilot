@echo off
rem Lets the CourtPilot server reach eCourts through this PC's internet.
rem Needs Tailscale running and signed in on this PC (see docs\RELAY.md).
rem Keep this window open or minimised. It restarts itself if anything drops.
title CourtPilot eCourts relay
cd /d "%~dp0"
:loop
".venv\Scripts\python.exe" scripts\ecourts_relay.py
echo.
echo Relay stopped. Restarting in 30 seconds (close this window to stop it for good)...
timeout /t 30 /nobreak >nul
goto loop
