@echo off
setlocal
cd /d "%~dp0.."
set /p "RIPPLES_PORT=LED COM port (example COM3): "
if not defined RIPPLES_PORT exit /b 1
call "%~dp0run.cmd" -m ripples.live --port "%RIPPLES_PORT%" --seconds 0 --reservoir live --criticality activity
pause
