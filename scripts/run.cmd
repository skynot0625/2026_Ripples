@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONUTF8=1"
set "OPENBLAS_NUM_THREADS=1"
set "OMP_NUM_THREADS=1"
set "PYTHONPATH=%CD%\src"
if not exist ".venv\Scripts\python.exe" (
  echo Create .venv and install with: python -m pip install -e ".[test]"
  exit /b 2
)
".venv\Scripts\python.exe" %*
exit /b %ERRORLEVEL%
