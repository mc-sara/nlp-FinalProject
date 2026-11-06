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
    ap = argparse.ArgumentParser(description="D1 采样表 -> params_d1.json")
    ap.add_argument("--log", default=str(ROOT / "real" / "samples" / "sampling_log.md"))
    ap.add_argument("--out", default=str(ROOT / "artifacts" / "params_d1.json"))
    args = ap.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"找不到采样表：{log_path}")
        return 2
    rows, bad = parse_rows(log_path.read_text(encoding="utf-8"))
    if not rows:
        if bad:
            print(f"有 {len(bad)} 行格式不对被跳过：")
            for b in bad[:5]:
                print(f"  {b}")
        print("记录表还是空的：先跑 real/samples/sampling_log.md 里的采集计划。")
        return 0

    publishes = [r for r in rows if r["类型"] == "publish" and counted(r)]
    reads = [r for r in rows if r["类型"] in ("health", "read") and counted(r)]
    warnings: list[str] = [f"跳过格式不对的行：{b}" for b in bad]

    # 1) k：只有 ok / unknown 才期待一个「第几轮看到」
    ks = [k for k in (poll_to_k(r["吧@t"]) for r in publishes if r["status"] in ("ok", "unknown"))
          if k is not None]
    k_median = statistics.median(ks) if ks else None
    missing_k = [r for r in publishes
                 if r["变体"] == "normal" and r["status"] in ("ok", "unknown")
                 and poll_to_k(r["吧@t"]) is None]
    if missing_k:
        warnings.append(f"{len(missing_k)} 条 ok/unknown 发帖行没填「吧@t 的轮次」，已跳过")

    # 2) 状态占比：notfound 是「观测不到」，不算状态
    weighted = [r for r in publishes if r["status"] != "notfound"]
    n_notfound = len(publishes) - len(weighted)
    status_counter = Counter(r["status"] for r in weighted)
    total = sum(status_counter.values())
    weights = {s: round(status_counter[s] / total, 4) for s in STATUSES} if total else {}
    if n_notfound:
        warnings.append(f"{n_notfound} 条 notfound 不进状态分布（可能被吞，也可能超过 3 轮）")
    for s in STATUSES:
        if total and status_counter[s] == 0:
            warnings.append(f"没采到 {s}；这一项按 0 记，报告里要写「未观测」")

    # 3) unknown 成因
    causes = Counter(
        r["reason"] for r in publishes
        if r["status"] == "unknown" and r["reason"] and r["reason"] != "—"
    )

    # 4) 两种变体：标题 40 字、重复发帖
    title_rows = [r for r in publishes if r["变体"] == "title-edge"]
    if not title_rows:
        title_rule = "client_truncate_31"
        warnings.append("没有 title-edge 行：client_truncate_31 依据 realcli.py 的 title_limit=31，不是采样证据")
    elif any("truncat" in (r["reason"] or "") for r in title_rows):
        title_rule = "client_truncate_31"
    elif any(r["status"] == "rejected" for r in title_rows):
        title_rule = "reject_40"
    else:
        title_rule = "accept_40"

    dup_rows = [r for r in publishes if r["变体"] == "dup-risk"]
    if not dup_rows:
        dup_rule = "unknown"
        warnings.append("没有 dup-risk 行，重复发帖规则仍是未知")
    else:
        dup_rule = "reject" if any(r["status"] == "rejected" for r in dup_rows) else "allow"

    params = {
        "generated_from": str(log_path),
        "n_rows": len(rows),
        "n_counted_read": len(reads),
        "n_counted_publish": len(publishes),
        "n_notfound": n_notfound,
        "k": k_median,
        "k_samples": ks,
        "status_weights": weights,
        "status_counts": dict(status_counter),
        "unknown_causes": dict(causes),
        "title_rule": title_rule,
        "duplicate_rule": dup_rule,
        "warnings": warnings,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(params, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("=== D1 采样摘要 ===")
    print(f"记录行：{len(rows)}（只读 {len(reads)} / 发帖 {len(publishes)}，其中 notfound {n_notfound}）")
    print(f"k：{k_median}（样本 {ks}）")
    print(f"状态占比：{weights or '无'}")
    print(f"unknown 成因：{dict(causes) or '无'}")
    print(f"标题规则：{title_rule}　重复规则：{dup_rule}")
    for w in warnings:
        print(f"  ! {w}")
    print(f"\n已写入：{out_path}")
    print("下一步：P2 把 k / status_weights / title_rule / duplicate_rule 填进 scenarios.py 与 env.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
