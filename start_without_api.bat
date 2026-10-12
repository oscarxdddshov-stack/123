@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto run
py -3 -m venv .venv
if errorlevel 1 goto failed
:run
".venv\Scripts\python.exe" screen_clicker.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Failed. This mode needs Windows 10/11, Python 3.11+ with Tcl/Tk, and Windows OCR.
pause
exit /b 1
