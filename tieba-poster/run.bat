@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY set "PY=python"

echo ============================================
echo   百度贴吧自动发帖
echo   解释器: %PY%
echo ============================================
echo.
if not exist config.json (
  echo [!] 没有找到 config.json
  echo     先执行: copy config.example.json config.json
  echo     然后用记事本把 cookie 字段填上
  echo.
  pause
  exit /b 1
)

echo [1] 检查登录态 ...
"%PY%" tieba_poster.py check
if errorlevel 1 (
  echo.
  echo [!] 登录态无效，进入 Cookie 填写向导
  echo     到浏览器 F12 - Network - 任一请求 - 标头 - Request Headers - Cookie，复制整行
  echo.
  "%PY%" setup_cookie.py
  if errorlevel 1 (
    echo.
    echo [!] Cookie 仍然无效，先手动修好再回来
    pause
    exit /b 1
  )
)

echo.
set /p TARGET=请输入帖子文件或目录（如 posts\example.md）：
if "%TARGET%"=="" (
  echo 未输入，退出
  pause
  exit /b 1
)

echo.
echo [2] 确认后会用浏览器（Edge）真实发帖
set /p GO=确认发布？输入 y 回车：
if /i not "%GO%"=="y" (
  echo 已取消
  pause
  exit /b 0
)

echo.
"%PY%" browser_poster.py post "%TARGET%" --yes
echo.
pause
