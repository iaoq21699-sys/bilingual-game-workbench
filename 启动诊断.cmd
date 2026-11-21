@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0runtime\python.exe" (
  "%~dp0runtime\python.exe" -E "%~dp0main.py"
) else (
  python "%~dp0main.py"
)
set "launch_result=%errorlevel%"
pause
exit /b %launch_result%
