@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto environment_error
".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if errorlevel 1 goto environment_error

".venv\Scripts\pythonw.exe" settings_ui.py
exit /b %ERRORLEVEL%

:environment_error
echo No valid local Python environment was found.
echo Run setup_windows.bat first, then open the settings tool again.
pause
exit /b 1
