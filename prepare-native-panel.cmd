@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0runtime\python.exe" (
  "%~dp0runtime\python.exe" -E "%~dp0scripts\prepare_reshade.py"
) else (
  python "%~dp0scripts\prepare_reshade.py"
)
set "prepare_result=%errorlevel%"
pause
exit /b %prepare_result%
