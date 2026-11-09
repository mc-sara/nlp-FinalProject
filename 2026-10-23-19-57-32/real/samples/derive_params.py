#!/usr/bin/env python3
"""把 D1 采样表（10 列）变成模拟器参数。

    python real/samples/derive_params.py            # 默认读 real/samples/sampling_log.md
    python real/samples/derive_params.py --log ... --out artifacts/params_d1.json

只读表，不改任何东西。算四件事：
  1. k            —— 发帖后第几次轮询可见：第 1 轮可见记 0，第 n 轮记 n-1
  2. status_weights —— ok / unknown / rejected 占比（notfound 不算状态，只记数量）
  3. unknown_causes —— unknown 的 reason 分布
  4. title_rule / duplicate_rule —— title-edge、dup-risk 两种变体的实测结果

输出 JSON 给 P2 用，同时打印人看的摘要。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # real/samples/ -> 项目根
COLUMNS = ["#", "时间", "类型", "变体", "status", "reason", "post_id", "吧@t", "证据", "计入统计"]
STATUSES = ("ok", "unknown", "rejected")
EXCLUDED = ("否", "no", "n", "false")
POLL_RE = re.compile(r"@\s*(\d+)")


def parse_rows(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """返回 (行, 被跳过的行)；列数不是正好 10 就整行跳过并报出来。"""
    rows: list[dict[str, str]] = []
    bad: list[str] = []
    started = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## 记录表"):
            started = True
            continue
        if not started or not s.startswith("|"):
            if rows and not s.startswith("|"):
                break            # 表后第一段非表格文字就停
            continue
        body = s[1:] if s.startswith("|") else s
        if body.endswith("|") and not body.endswith("\\|"):
            body = body[:-1]
        cells = [c.strip().replace("\u0001", "|") for c in body.replace("\\|", "\u0001").split("|")]
        if len(cells) != len(COLUMNS):
            hint = "（很可能命令/证据里有未转义的 |）" if len(cells) > len(COLUMNS) else ""
            bad.append("|".join(cells)[:100] + hint)
            continue
        if set(cells[0]) <= set("-: "):
            continue             # 分隔行
        if cells[0] == "#":
            continue             # 表头
        rows.append(dict(zip(COLUMNS, cells)))
    return rows, bad


CSV_COLUMNS = ["#", "time", "type", "variant", "status", "reason", "post_id", "bar@t", "evidence", "counted"]


def read_csv_rows(paths: list[Path]) -> list[dict[str, str]]:
    """读 table1/table2；列名统一成表头里的中文/英文两套。"""
    rows: list[dict[str, str]] = []
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for item in csv.DictReader(fh):
                rows.append({
                    "": item.get("#", ""),
                    "类型": item.get("type", ""),
                    "变体": item.get("variant", ""),
                    "status": item.get("status", ""),
                    "reason": item.get("reason", ""),
                    "post_id": item.get("post_id", ""),
                    "吧@t": item.get("bar@t", ""),
                    "证据": item.get("evidence", ""),
                    "计入统计": item.get("counted", ""),
                })
    return rows


def counted(row: dict[str, str]) -> bool:
    return row.get("计入统计", "").strip().lower() not in EXCLUDED


def poll_to_k(value: str) -> int | None:
    match = POLL_RE.search(value or "")
    if not match:
        return None
    poll = int(match.group(1))
    if poll < 1:
        return None
    return poll - 1


def main() -> int:
    ap = argparse.ArgumentParser(description="D1 采样表/CSV -> params_d1.json")
    ap.add_argument("--csv", action="append", default=None,
                    help="采样 CSV（可多次传），默认读 table1/table2")
    ap.add_argument("--log", default=None, help="旧的 Markdown 表（CSV 模式不用）")
    ap.add_argument("--out", default=str(ROOT / "artifacts" / "params_d1.json"))
    args = ap.parse_args()

    samples = ROOT / "real" / "samples"
    if args.csv:
        paths = [Path(p) for p in args.csv]
    elif args.log:
        paths = []
    else:
        paths = [samples / "table1_agenttest.csv", samples / "table2_public.csv"]
        paths = [p for p in paths if p.exists()]

    rows: list[dict[str, str]] = []
    if paths:
        rows = read_csv_rows(paths)
        source = ", ".join(p.name for p in paths)
    else:
        log_path = Path(args.log or samples / "sampling_log.md")
        if not log_path.exists():
            print(f"找不到采样表：{log_path}")
            return 2
        rows, _bad = parse_rows(log_path.read_text(encoding="utf-8"))
        source = log_path.name

    if not rows:
        print("记录表还是空的。")
        return 0

    publishes = [r for r in rows if r["类型"] == "publish" and counted(r)]
    health_rows = [r for r in rows if r["类型"] == "health" and counted(r)]
    health_failed = [r for r in rows if r["类型"] == "health" and r["status"] == "error"]
    reads = [r for r in rows if r["类型"] in ("health", "read") and counted(r)]
    warnings: list[str] = []

    # 1) k
    ks = [k for k in (poll_to_k(r["吧@t"]) for r in publishes if r["status"] in ("ok", "unknown")) if k is not None]
    k_median = statistics.median(ks) if ks else None

    # 2) 状态分布（notfound 不算状态）
    weighted = [r for r in publishes if r["status"] != "notfound"]
    n_notfound = len(publishes) - len(weighted)
    status_counter = Counter(r["status"] for r in weighted)
    total = sum(status_counter.values())
    weights = {s: round(status_counter[s] / total, 4) for s in STATUSES} if total else {}

    # 3) unknown 成因
    causes = Counter(r["reason"] for r in publishes if r["status"] == "unknown" and r["reason"] not in ("", "—"))

    # 4) 规则
    dup_rows = [r for r in publishes if r["变体"] == "dup-risk"]
    if dup_rows:
        dup_rule = "allow" if any(r["status"] == "ok" for r in dup_rows) else "reject"
    else:
        dup_rule = "unknown"

    # poster 自己报 unknown、回查才确认已发的条数（工具假失败，不是稀有情况）
    poster_unknown = [r for r in publishes if "poster" in (r["证据"] or "")]
    n_poster_unknown = len(poster_unknown)

    params = {
        "generated_from": source,
        "n_rows": len(rows),
        "n_counted_read": len(reads),
        "n_counted_publish": len(publishes),
        "n_notfound": n_notfound,
        "k": k_median,
        "k_samples": ks,
        "k_max": max(ks) if ks else None,
        "status_weights": weights,
        "status_counts": dict(status_counter),
        "unknown_causes": dict(causes),
        "duplicate_rule": dup_rule,
        "title_rule": "client_truncate_31",
        "n_health": len(health_rows) + len(health_failed),
        "n_health_failed": len(health_failed),
        "health_fail_rate": round(len(health_failed) / (len(health_rows) + len(health_failed)), 4)
        if health_rows or health_failed else None,
        "n_poster_unknown": n_poster_unknown,
        "warnings": warnings,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(params, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("=== D1 采样摘要 ===")
    print(f"来源：{source}（{len(rows)} 行；只读 {len(reads)} / 发帖 {len(publishes)}）")
    print(f"k：{k_median}（样本 {ks}，最大 {params['k_max']}）")
    print(f"状态占比：{weights or '无'}")
    print(f"unknown 成因：{dict(causes) or '无'}")
    print(f"重复规则：{dup_rule}　标题规则：client_truncate_31")
    print(f"健康检查失败：{len(health_failed)}/{params['n_health']}　poster 假失败条数：{n_poster_unknown}")
    print(f"\n已写入：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
