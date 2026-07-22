@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto environment_error
".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if errorlevel 1 goto environment_error

echo Starting MeshTelegram Bridge...
echo.
".venv\Scripts\python.exe" main.py
set "BRIDGE_EXIT_CODE=%ERRORLEVEL%"
echo.
echo MeshTelegram Bridge has stopped.
pause
exit /b %BRIDGE_EXIT_CODE%

:environment_error
echo No valid local Python environment was found.
echo Run setup_windows.bat first, then start this program again.
pause
exit /b 1
