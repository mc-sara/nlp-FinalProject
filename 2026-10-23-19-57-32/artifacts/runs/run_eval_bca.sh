#!/bin/bash
set -u
cd /public/home/scnsjwf2qh/P2_SCNet_20261105/p2_scnet
source /opt/dtk-26.04/env.sh >/dev/null 2>&1
export PATH=$PWD/.venv/bin:$PATH
export PYTHONPATH=$PWD/vendor/LlamaFactory/src
export TMPDIR=/public/home/scnsjwf2qh/tmp
MODEL=/public/home/scnsjwf2qh/models/Qwen3.5-9B
COMMON="--model $MODEL --split test --per-family 20 --seed 1042 --context 8192 --max-new 512"
echo "[$(date +%H:%M:%S)] B start"
.venv/bin/python p2.py collect $COMMON --group B --adapter artifacts/adapters/B60 --out artifacts/runs/B_test_1042.jsonl >> artifacts/runs/eval_B.log 2>&1 || { echo "[$(date +%H:%M:%S)] B failed"; exit 1; }
echo "[$(date +%H:%M:%S)] B done; C start"
.venv/bin/python p2.py collect $COMMON --group C --adapter artifacts/adapters/C60 --out artifacts/runs/C_test_1042.jsonl >> artifacts/runs/eval_C.log 2>&1 || { echo "[$(date +%H:%M:%S)] C failed"; exit 1; }
echo "[$(date +%H:%M:%S)] C done; A start"
.venv/bin/python p2.py collect $COMMON --group A --out artifacts/runs/A_test_1042.jsonl >> artifacts/runs/eval_A.log 2>&1 || { echo "[$(date +%H:%M:%S)] A failed"; exit 1; }
echo "[$(date +%H:%M:%S)] ALL DONE"
