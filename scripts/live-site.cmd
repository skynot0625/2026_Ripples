@echo off
call "%~dp0run.cmd" -m ripples.receive --config configs/live.m30.json --seconds 0 %*
exit /b %ERRORLEVEL%
