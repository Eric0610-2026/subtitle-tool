@echo off
cd /d "%~dp0"
echo Checking dependencies...
pip install -r requirements.txt -q 2>nul
if %errorlevel% neq 0 (
    echo Failed to install dependencies. Run: pip install -r requirements.txt
    pause
    exit /b
)
start "" pythonw.exe "subtitle_app.py"
