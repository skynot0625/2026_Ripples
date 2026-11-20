@echo off
call "%~dp0run.cmd" -m ripples.live --seconds 30 %*
exit /b %ERRORLEVEL%
