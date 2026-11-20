@echo off
call "%~dp0run.cmd" -m ripples.receive --seconds 0 %*
exit /b %ERRORLEVEL%
