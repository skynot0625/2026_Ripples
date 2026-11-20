@echo off
call "%~dp0run.cmd" -m ripples.receive --seconds 30 %*
exit /b %ERRORLEVEL%
