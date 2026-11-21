@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
set "RIPPLES_PY=python"
if exist ".venv\Scripts\python.exe" set "RIPPLES_PY=%CD%\.venv\Scripts\python.exe"
if exist "..\..\work\venv\Scripts\python.exe" set "RIPPLES_PY=%CD%\..\..\work\venv\Scripts\python.exe"
echo.
echo FOUR SPEAKER TEST - Realtek / two stereo amplifiers
echo Set BOTH amplifier gains LOW before starting.
echo Green jack: OUT 1 / 2. Blue jack: OUT 3 / 4 after output retasking.
echo Test peak: 2 percent (-34 dBFS). Ctrl+C stops. No automatic repeat.
echo.
echo A = test all speakers once, 1 to 4 with two-second gaps
echo 1 / 2 / 3 / 4 = test only that speaker
echo Q = quit without sound
choice /c A1234Q /n /m "Select: "
set "RIPPLES_CHOICE=%ERRORLEVEL%"
if "%RIPPLES_CHOICE%"=="6" exit /b 0
if "%RIPPLES_CHOICE%"=="1" goto all
if %RIPPLES_CHOICE% LSS 2 exit /b 1
if %RIPPLES_CHOICE% GTR 5 exit /b 1
set /a RIPPLES_SPEAKER=RIPPLES_CHOICE-1
"%RIPPLES_PY%" -X utf8 -m ripples.audio_play --config configs/audio.realtek.m30.json --test-speaker %RIPPLES_SPEAKER%
goto done
:all
"%RIPPLES_PY%" -X utf8 -m ripples.audio_play --config configs/audio.realtek.m30.json --test-speakers
:done
set "RIPPLES_RESULT=%ERRORLEVEL%"
echo.
echo Test finished. This window does not confirm the physical wiring automatically.
pause
exit /b %RIPPLES_RESULT%
