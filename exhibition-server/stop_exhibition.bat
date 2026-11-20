@echo off
rem Stop the exhibition: runs exhibition.py stop in this folder (no administrator rights needed).
rem Uses .venv\Scripts\python.exe when present, otherwise the Python launcher (py -3).
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" exhibition.py stop %*
) else (
  py -3 exhibition.py stop %*
)
if errorlevel 1 pause
