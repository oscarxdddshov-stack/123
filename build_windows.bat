@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
py -3 -m venv .venv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt pyinstaller==6.16.0
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name AyuClicker run.py
if errorlevel 1 goto failed
echo.
echo Created: dist\AyuClicker.exe
pause
exit /b 0
:failed
echo.
echo Build failed. Run this script on Windows with Python 3.11+ installed.
pause
exit /b 1
