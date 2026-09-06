@echo off
chcp 65001 > nul
echo --------------------------------------------
echo Starting Telegram Bot...
echo --------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment .venv not found.
    echo Please run: python -m venv .venv
    echo and: .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python main.py
pause
