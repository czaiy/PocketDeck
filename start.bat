@echo off
title KZQ PC Remote
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  echo [KZQ] .venv not found, falling back to system Python
  set "PY=python"
)
"%PY%" server.py
pause
