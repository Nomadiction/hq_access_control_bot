@echo off
chcp 65001 >nul
REM Local launch on Windows. Create .env first (copy from .env.example) and put your token in it.
cd /d %~dp0

if not exist venv (
    echo [*] First run: creating venv and installing dependencies...
    python -m venv venv
    venv\Scripts\python -m pip install --upgrade pip
    venv\Scripts\pip install -r requirements.txt
)

echo [*] Starting bot. Press Ctrl+C to stop.
venv\Scripts\python bot.py
pause
