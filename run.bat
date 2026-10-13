@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist config.json (
  echo [!] 未找到 config.json，正在从 config.example.json 复制...
  copy config.example.json config.json >nul
  echo [!] 请先编辑 config.json 填入 UserToken 后重新运行本脚本。
  pause
  exit /b 1
)

echo === 1/2 检查 token ===
python -m vocabgo check || (echo [!] token 无效，请重新获取 & pause & exit /b 1)

echo.
echo === 2/2 开始刷班级任务（任务列表） ===
python -m vocabgo tasks --kind class

echo.
set /p IDX=请输入要刷的任务序号（直接回车默认 0）:
if "%IDX%"=="" set IDX=0

python -m vocabgo run --kind class --index %IDX% --loop 3
pause
