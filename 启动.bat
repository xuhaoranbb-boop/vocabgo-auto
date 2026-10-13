@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [!] 没找到 python，请先安装 Python 3.10+ 并勾选 Add to PATH
  pause
  exit /b 1
)

python -c "import requests" >nul 2>nul
if errorlevel 1 (
  echo [*] 首次运行，正在安装依赖...
  python -m pip install -r requirements.txt
)

echo [*] 启动图形界面...
python vocabgo_app.py
if errorlevel 1 pause
