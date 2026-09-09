@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 正在创建 Python 环境，需要 Python 3.11 或更高版本...
  py -3 -m venv .venv
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, playwright, patchright, httpx, multipart" >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" run.py
if errorlevel 1 goto fail
exit /b 0
:fail
echo 启动失败，请检查上面的错误信息。
pause
exit /b 1
