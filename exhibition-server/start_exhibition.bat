@echo off
rem Start the exhibition: runs exhibition.py start in this folder (no administrator rights needed).
rem Uses .venv\Scripts\python.exe when present, otherwise the Python launcher (py -3).
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" exhibition.py start %*
) else (
  py -3 exhibition.py start %*
)
if errorlevel 1 pause
