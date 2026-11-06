#!/usr/bin/env python3
"""往 D1 采样表追加一行（10 列，省得手抄）。

    # 只读
    python real/samples/record_row.py --type read --variant list \
        --bar-at agenttest@1 --status ok --evidence "list@t1:见20条"

    # 发帖：第 3 轮才看到 → 吧@t 写 agenttest@3
    python real/samples/record_row.py --type publish --variant normal \
        --status ok --post-id 11139485781 --bar-at agenttest@3 --evidence "list@t3:p1#2"

    # 发完没看到
    python real/samples/record_row.py --type publish --variant normal \
        --status notfound --bar-at agenttest@3 --reason not_visible_after_3_polls

    # 健康检查（失败时传 --not-counted）
    python real/samples/record_row.py --type health --status ok --evidence "login,level,editor,list"

规则：
  - 列数固定 10，`|` 自动转义；
  - 序号 = 现有数据行数 + 1；
  - `计入统计` 默认「是」，健康检查失败/工具异常传 --not-counted。
"""

from __future__ import annotations

import argparse
import datetime as _dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG = ROOT / "real" / "samples" / "sampling_log.md"
HEADER = "| # | 时间 | 类型 | 变体 | status | reason | post_id | 吧@t | 证据 | 计入统计 |"


def esc(text: str) -> str:
    return (text or "").replace("|", "\\|").strip() or "—"


def main() -> int:
    ap = argparse.ArgumentParser(description="追加一行到 D1 采样表（10 列）")
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--type", required=True, choices=["health", "read", "publish"])
    ap.add_argument("--variant", default="—",
                    help="read: list / publish: normal|title-edge|dup-risk / health: —")
    ap.add_argument("--status", default="ok",
                    choices=["ok", "unknown", "rejected", "notfound", "error"])
    ap.add_argument("--reason", default="—")
    ap.add_argument("--post-id", default="—")
    ap.add_argument("--bar-at", default="—", help="哪个吧@第几轮，如 agenttest@2")
    ap.add_argument("--evidence", default="—", help="证据来源，如 list@t2:p1#3 / detail@t3:200")
    ap.add_argument("--not-counted", action="store_true", help="计入统计=否")
    args = ap.parse_args()

    log = Path(args.log)
    if not log.exists():
        raise SystemExit(f"找不到采样表：{log}")
    lines = log.read_text(encoding="utf-8").splitlines()

    try:
        start = lines.index(HEADER)
    except ValueError:
        raise SystemExit(f"找不到 10 列表头，确认 {log} 未被改回旧格式")

    # 表头下一行是分隔行，再往下是数据行
    end = start + 2
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    idx = end - (start + 2) + 1

    cells = [
        str(idx),
        _dt.datetime.now().strftime("%H:%M:%S"),
        args.type,
        esc(args.variant),
        args.status,
        esc(args.reason),
        esc(args.post_id),
        esc(args.bar_at),
        esc(args.evidence),
        "否" if args.not_counted else "是",
    ]
    line = "| " + " | ".join(cells) + " |"

    lines.insert(end, line)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已追加第 {idx} 行：{line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
