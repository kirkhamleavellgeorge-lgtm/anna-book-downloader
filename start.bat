@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Preparing Python environment...
    py -3 -m venv .venv
    if errorlevel 1 python -m venv .venv
    if errorlevel 1 goto :failed
)
".venv\Scripts\python.exe" -c "import playwright, bs4, websockets" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies for the first launch...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :failed
)
start "" ".venv\Scripts\pythonw.exe" -m anna_downloader
exit /b 0
:failed
echo Setup failed. Install Python 3.10 or newer, check your network, then try again.
pause
exit /b 1
