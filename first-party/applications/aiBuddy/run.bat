@echo off
setlocal
rem AI Buddy terminal launcher. Requires root Tertalis setup (python-embedded).
set "TERTALIS_ROOT=%~dp0..\..\.."
set "TERTALIS_PYTHON=%TERTALIS_ROOT%\python-embedded\python.exe"
if not exist "%TERTALIS_PYTHON%" (
    echo Tertalis Python was not found at "%TERTALIS_PYTHON%".
    echo Run the root setup.bat first.
    if not defined TERTALIS_RUN_NO_PAUSE pause
    exit /b 1
)
"%TERTALIS_PYTHON%" -X frozen_modules=off "%~dp0run.py" %*
set "runExitCode=%ERRORLEVEL%"
if not "%runExitCode%"=="0" if not defined TERTALIS_RUN_NO_PAUSE pause
exit /b %runExitCode%
