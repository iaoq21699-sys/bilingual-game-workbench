@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0runtime\pythonw.exe" (
  start "" "%~dp0runtime\pythonw.exe" -E "%~dp0main.py"
  exit /b
)
where pyw >nul 2>nul
if not errorlevel 1 (
  start "" pyw -3 "%~dp0main.py"
  exit /b
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw "%~dp0main.py"
  exit /b
)
echo Use the Windows x64 portable ZIP, or install Python 3.10 or later with Tcl/Tk.
pause
exit /b 1
