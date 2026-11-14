@echo off

if exist ".venv\Scripts\pythonw.exe" (
    start ".venv\Scripts\pythonw.exe" scripts\run.py
) else (
    echo 未找到虚拟环境 .venv，请先执行:
    echo   python -m venv .venv
    echo   .venv\Scripts\python -m pip install -r requirements.txt
    pause
)
