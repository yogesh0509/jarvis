@echo off
rem Unified jarvis command -- "jarvis start|status|stats|debug|pause|resume|quit".
rem "start" launches a fresh visible instance (jarvisctl.py can't do
rem this itself -- it only talks to an already-running one). Everything
rem else is forwarded straight to jarvisctl.py.

set JARVIS_DIR=%~dp0

if "%1"=="start" (
    wt.exe -F -p Jarvis
    exit /b 0
)

if "%1"=="" (
    echo Usage: jarvis {start^|status^|stats^|debug^|pause^|resume^|quit}
    exit /b 1
)

"%JARVIS_DIR%.venv\Scripts\python.exe" "%JARVIS_DIR%jarvisctl.py" %*
