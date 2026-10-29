#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false
export HF_HOME="${HF_HOME:-$PWD/artifacts/cache/huggingface}"
export MODELSCOPE_CACHE="${MODELSCOPE_CACHE:-$PWD/artifacts/cache/modelscope}"
export MODEL_DIR="${MODEL_DIR:-$PWD/artifacts/models/Qwen3.5-9B}"
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$PWD/artifacts/cache/pip}"
mkdir -p artifacts/logs artifacts/runs artifacts/configs
stage="${1:-collect}"
case "$stage" in
  setup|download|collect|fewshot|prepare|train-B|train-C|evaluate) ;;
  *) echo "Stages: setup | download | collect | fewshot | prepare | train-B | train-C | evaluate" >&2; exit 2 ;;
esac
# Retain DTK's torch through system-site-packages, isolate application libraries.
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv --system-site-packages .venv
fi
source .venv/bin/activate
if [[ ! -f artifacts/environment/setup.complete ]]; then
  python install_environment.py
fi
if [[ "$stage" == setup ]]; then
  echo "Environment setup complete. Next: bash run_scnet.sh download"
  exit 0
fi
if [[ ! -f "$MODEL_DIR/config.json" || ! -f "$MODEL_DIR/p2-download.complete" ]]; then
  python download_model.py
fi
case "$stage" in
  download)
    echo "Model download complete. Next: bash run_scnet.sh collect"
    ;;
  collect)
    python p2.py collect --model "$MODEL_DIR" --group A --split train --per-family 20 --seed 42 --out artifacts/runs/A_train_42.jsonl
    python p2.py review --input artifacts/runs/A_train_42.jsonl --out artifacts/runs/A_train_42.review.json
    ;;
  fewshot)
    python fewshot.py --model "$MODEL_DIR" --input artifacts/runs/A_train_42.jsonl --review artifacts/runs/A_train_42.review.json --out artifacts/runs/fewshot_31415.jsonl
    python p2.py review --input artifacts/runs/fewshot_31415.jsonl --out artifacts/runs/fewshot_31415.review.json
    ;;
  prepare)
    python p2.py prepare --input artifacts/runs/A_train_42.jsonl --review artifacts/runs/A_train_42.review.json --out artifacts/data
    python audit_tokens.py --model "$MODEL_DIR" --data artifacts/data
    ;;
  train-B|train-C)
    python assess_fewshot.py
    group="${stage#train-}"
    python audit_tokens.py --model "$MODEL_DIR" --data artifacts/data
    if [[ -d "artifacts/adapters/$group" ]]; then
      echo "Existing adapter directory: preserve it; choose a new output or explicit resume config." >&2
      exit 1
    fi
    python p2.py config --model "$MODEL_DIR" --group "$group" --data artifacts/data --output "artifacts/adapters/$group" --steps 100 --out "artifacts/configs/train_$group.yaml"
    llamafactory-cli train "artifacts/configs/train_$group.yaml"
    ;;
  evaluate)
    for group in A B C; do
      extra=()
      if [[ "$group" != A ]]; then extra=(--adapter "$PWD/artifacts/adapters/$group"); fi
      python p2.py collect --model "$MODEL_DIR" --group "$group" "${extra[@]}" --split test --per-family 20 --seed 1042 --out "artifacts/runs/${group}_test_1042.jsonl"
    done
    ;;
  *) echo "Stages: collect | fewshot | prepare | train-B | train-C | evaluate" >&2; exit 2 ;;
esac
