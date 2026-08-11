@echo off
setlocal
cd /d "%~dp0"

echo Setting up the MeshBridge environment...
echo.

where py >nul 2>&1
if not errorlevel 1 goto use_py
where python >nul 2>&1
if errorlevel 1 goto python_missing
set "PYTHON_COMMAND=python"
goto python_found

:use_py
set "PYTHON_COMMAND=py -3"

:python_found
echo Creating a local virtual environment...
%PYTHON_COMMAND% -m venv --clear .venv
if errorlevel 1 goto setup_failed

echo Installing required packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto setup_failed

echo.
echo Setup completed successfully.
echo You can now run run_meshbridge.bat.
goto finish_success

:python_missing
echo Python was not found.
echo Install Python 3.10 or newer and enable Add Python to PATH.
goto finish_error

:setup_failed
echo.
echo Setup failed. Review the error messages above.
goto finish_error

:finish_success
echo.
pause
exit /b 0

:finish_error
echo.
pause
exit /b 1
