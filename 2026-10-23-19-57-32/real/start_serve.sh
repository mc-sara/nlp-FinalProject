#!/bin/sh
# 集群侧：重启模型服务（只做 forward）。用法：sh start_serve.sh [端口]
# 注意：不要 set -e —— 某些系统上 source env.sh 会返回非零，导致脚本静默退出。
cd /public/home/scnsjwf2qh/P2_SCNet_20261105/p2_scnet
source /opt/dtk-26.04/env.sh >/dev/null 2>&1 || true
export PATH=$PWD/.venv/bin:$PATH PYTHONPATH=$PWD/vendor/LlamaFactory/src TMPDIR=/public/home/scnsjwf2qh/tmp
PORT=${1:-8000}

for pid in $(pgrep -f "serve_policy[.]py"); do
  kill -9 "$pid" 2>/dev/null || true
done
sleep 2

setsid nohup .venv/bin/python serve_policy.py \
  --model /public/home/scnsjwf2qh/models/Qwen3.5-9B \
  --adapter artifacts/adapters/C60 \
  --port "$PORT" > artifacts/runs/serve_policy.log 2>&1 < /dev/null &
sleep 6
pgrep -af "serve_policy[.]py" | head -1
