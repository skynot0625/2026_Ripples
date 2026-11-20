@echo off
setlocal
cd /d "%~dp0.."
set "RIPPLES_EFFECT=activity"
set /p "RIPPLES_CHOICE=1 actual firing (default), 2 slow burst-triggered waves: "
if "%RIPPLES_CHOICE%"=="2" set "RIPPLES_EFFECT=slow_waves"
set /p "RIPPLES_PORT=LED COM port (example COM3): "
if not defined RIPPLES_PORT exit /b 1
call "%~dp0run.cmd" -m ripples --port "%RIPPLES_PORT%" --seconds 0 --reservoir live --criticality %RIPPLES_EFFECT% --watch
pause
