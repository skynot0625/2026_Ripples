@echo off
if not exist "%~dp0..\runtime" mkdir "%~dp0..\runtime"
>"%~dp0..\runtime\state.txt" echo sleep
