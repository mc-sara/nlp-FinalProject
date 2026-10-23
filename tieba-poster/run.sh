#!/usr/bin/env bash
# Git Bash / WSL 用的启动器（cmd 用 run.bat，PowerShell 直接敲命令）
set -uo pipefail
cd "$(dirname "$0")" || exit 1

# 优先用项目自带的 .venv，避免撞上 Anaconda / 系统里连不通百度的解释器
PY=""
for cand in "./.venv/Scripts/python.exe" "./.venv/bin/python" ".venv/Scripts/python.exe"; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  if command -v python >/dev/null 2>&1 && python -c "import requests" >/dev/null 2>&1; then
    PY="python"
    echo "[i] 用系统 PATH 里的 python（没找到 .venv）"
  else
    echo "[!] 找不到能用的 Python（缺 requests）"
    echo "    先执行这两行建立项目环境："
    echo "      python -m venv .venv"
    echo "      .venv/Scripts/pip.exe install requests websocket-client"
    exit 1
  fi
fi

echo "============================================"
echo "  百度贴吧自动发帖"
echo "  解释器: $PY"
echo "============================================"
echo

if [ ! -f config.json ]; then
  echo "[!] 没有 config.json，先复制一份：cp config.example.json config.json"
  exit 1
fi

echo "[1] 检查登录态 ..."
if ! "$PY" tieba_poster.py check; then
  echo
  echo "[!] 登录态无效，进入 Cookie 填写向导"
  echo "    浏览器 F12 - Network - 任一请求 - 标头 - Request Headers - Cookie，复制整行"
  echo
  if ! "$PY" setup_cookie.py; then
    echo "[!] Cookie 仍然无效，先修好再来"
    exit 1
  fi
fi

echo
read -r -p "请输入帖子文件或目录（如 posts/我的第一个帖子.md）： " TARGET
if [ -z "$TARGET" ]; then
  echo "未输入，退出"
  exit 1
fi

echo
echo "[2] 先预览一遍（只打印，不发帖）..."
"$PY" tieba_poster.py post "$TARGET" --dry-run

echo
echo "[3] 确认后会用浏览器（Edge）真实发帖"
read -r -p "确认发布？输入 y 回车： " GO
case "$GO" in
  y|Y) ;;
  *) echo "已取消"; exit 0 ;;
esac

echo
"$PY" browser_poster.py post "$TARGET" --yes
