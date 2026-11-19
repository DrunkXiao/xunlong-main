@echo off
setlocal
cd /d "%~dp0"

set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo 未找到项目虚拟环境：.venv\Scripts\python.exe
    echo 请先创建虚拟环境并安装 requirements-build.txt
    exit /b 1
)

"%PYTHON%" -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1

"%PYTHON%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name xunlong-dingpan ^
    --icon "%~dp0icon.ico" ^
    --add-data "%~dp0app\db\schema.sql;app\db" ^
    --add-data "%~dp0app\ui\theme.css;app\ui" ^
    --add-data "%~dp0config.example.yaml;." ^
    "%~dp0scripts\run.py"
if errorlevel 1 exit /b 1

echo.
echo 打包完成：dist\xunlong-dingpan.exe
