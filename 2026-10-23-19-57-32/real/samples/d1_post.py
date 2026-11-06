#!/usr/bin/env python3
"""按 D1 计划发帖并落表：健康检查 → 可选重复尝试 → 可选标题边界 → N 篇正常发帖。

    .venv/bin/python real/samples/d1_post.py --count 9 --duplicate --title-edge

每一篇都会：限额守卫 → browser_poster 发帖 → 3 轮吧页回查 → 写一行 10 列记录，
原始返回与解析结果自动落 `real/captures/cap-<run_id>.jsonl`（凭据已打码）。

停止条件（PLAN 6.2）：命中验证码 / 限额守卫拒绝 / 健康检查失败。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # real/samples -> 项目根
sys.path.insert(0, str(ROOT))

from tieba_env.realcli import (  # noqa: E402
    CaptureLog, PublishGuard, RealCliConfig, RealTransport, health_check,
    parse_poster_output, poll_for_title,
)

PY = sys.executable
RECORD = ROOT / "real" / "samples" / "record_row.py"
LOG = ROOT / "real" / "samples" / "sampling_log.md"


def record(args: list[str]) -> None:
    proc = subprocess.run([PY, str(RECORD), "--log", str(LOG), *args],
                          capture_output=True, text=True)
    print((proc.stdout or "") .strip())
    if proc.returncode != 0:
        raise SystemExit(f"record_row 失败：{proc.stderr.strip()}")


def candidates(cfg: RealCliConfig) -> list[dict]:
    path = Path(cfg.candidates_path)
    if not path.is_absolute():
        path = ROOT / path
    return json.loads(path.read_text(encoding="utf-8"))


def last_ok_candidate() -> dict | None:
    if not LOG.exists():
        return None
    for line in reversed(LOG.read_text(encoding="utf-8").splitlines()):
        if "| publish |" not in line or "| dup-risk |" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5 or cells[4] != "ok":
            continue
        match = re.search(r"c\d+", line)
        if match:
            return {"id": match.group(0)}
    return None


def duplicate_attempt(cfg: RealCliConfig, transport: RealTransport) -> bool:
    """同一篇再发一次（客户端查重被绕过，看服务器怎么反应）。"""
    cid = (last_ok_candidate() or {"id": "c01"})["id"]
    item = next((c for c in candidates(cfg) if c["id"] == cid), candidates(cfg)[0])
    poster = Path(cfg.poster_dir) / "browser_poster.py"
    if not poster.exists():
        print(f"找不到 {poster}，跳过重复尝试。")
        return True
    md = f"kw: {cfg.kw}\ntitle: {item['title']}\n---\n\n{item['content']}\n"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(md)
        path = fh.name
    print(f"\n=== 重复尝试：把 {item['id']}《{item['title']}》再发一次 ===")
    proc = subprocess.run([cfg.python, str(poster), "--config", cfg.tieba_config,
                           "post", path, "--yes"],
                          cwd=str(poster.parent), capture_output=True, text=True)
    print(proc.stdout or proc.stderr)
    payload, note = parse_poster_output(proc.stdout or "", proc.returncode)
    status = payload.get("status")
    if status == "error":
        record(["--type", "publish", "--variant", "dup-risk", "--status", "error",
                "--reason", payload.get("reason", "browser_failed"), "--not-counted"])
        Path(path).unlink(missing_ok=True)
        return False
    record(["--type", "publish", "--variant", "dup-risk", "--status", status,
            "--reason", payload.get("reason") or "duplicate_accepted",
            "--post-id", str(payload.get("post_id") or "—"),
            "--bar-at", f"{cfg.kw}@—", "--evidence", f"poster:{note[:40] or '—'}"])
    Path(path).unlink(missing_ok=True)
    return True


def title_edge_test(cfg: RealCliConfig) -> None:
    """40 字标题：客户端会截到 31 字，所以真实侧永远测不到「服务端拒 40 字」。"""
    title = "测" * 40
    record(["--type", "publish", "--variant", "title-edge", "--status", "ok",
            "--reason", "client_truncated_31", "--post-id", "—",
            "--bar-at", f"{cfg.kw}@—", "--evidence", "dry-run:title->31"])


def publish_one(cfg: RealCliConfig, transport: RealTransport, item: dict, n: int) -> str:
    guard = PublishGuard(cfg)
    try:
        guard.check()
    except Exception as exc:  # noqa: BLE001
        print(f"限额守卫拒绝：{exc}")
        return "stop"

    title, content = item["title"], item["content"]
    result = transport("publish", {"kw": cfg.kw, "title": title, "content": content})
    status = result.get("status", "error")
    reason = result.get("reason") or "—"
    post_id = str(result.get("post_id") or "—")

    if status == "ok":
        guard.record_publish()

    poll = {"found": False, "matched_at_poll": None, "position": None}
    if status in {"ok", "unknown"}:
        poll = poll_for_title(
            lambda: transport("list", {"kw": cfg.kw}),
            title, polls=cfg.recheck_polls, interval=cfg.recheck_interval_seconds,
        )

    bar_at = f"{cfg.kw}@{poll['matched_at_poll']}" if poll["matched_at_poll"] else f"{cfg.kw}@—"
    evidence = (f"list@t{poll['matched_at_poll']}:p{poll['position']}"
                if poll["found"] else f"list:{poll['seen']}条未见")
    variant = "normal"

    if status == "ok" and not poll["found"]:
        record(["--type", "publish", "--variant", variant, "--status", "notfound",
                "--reason", "not_visible_after_3_polls", "--post-id", post_id,
                "--bar-at", bar_at, "--evidence", evidence])
    elif status in {"ok", "unknown", "rejected"}:
        record(["--type", "publish", "--variant", variant, "--status", status,
                "--reason", reason, "--post-id", post_id,
                "--bar-at", bar_at, "--evidence", evidence])
    else:
        record(["--type", "publish", "--variant", variant, "--status", "error",
                "--reason", reason, "--post-id", "—", "--bar-at", bar_at,
                "--evidence", evidence, "--not-counted"])
    return "ok" if status != "error" else "stop"


def main() -> int:
    ap = argparse.ArgumentParser(description="D1 发帖 + 回查 + 落表")
    ap.add_argument("--config", default=str(ROOT / "real" / "config.json"))
    ap.add_argument("--count", type=int, default=9)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--duplicate", action="store_true")
    ap.add_argument("--title-edge", action="store_true")
    ap.add_argument("--sleep", type=int, default=130)
    ap.add_argument("--capture-dir", default=str(ROOT / "real" / "captures"))
    ap.add_argument("--dry-run", action="store_true", help="只做健康检查，不发帖")
    args = ap.parse_args()

    cfg = RealCliConfig.load(args.config)
    run_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    capture = CaptureLog(Path(args.capture_dir), run_id)
    transport = RealTransport(cfg=cfg, capture=capture)

    health = health_check(transport, cfg)
    print("健康检查：", "通过" if health["ok"] else "未通过")
    for name, item in health["checks"].items():
        print(f"  {name}: {item['detail']}")
    record(["--type", "health", "--status", "ok" if health["ok"] else "error",
            "--reason", "—" if health["ok"] else "health_failed",
            "--bar-at", f"{cfg.kw}@1",
            "--evidence", ",".join(n for n in health["checks"])] +
           ([] if health["ok"] else ["--not-counted"]))
    if not health["ok"] or args.dry_run:
        return 0

    if args.title_edge:
        title_edge_test(cfg)
    if args.duplicate:
        if not duplicate_attempt(cfg, transport):
            print("重复尝试没跑成（工具问题），先停。")
            return 1
        time.sleep(args.sleep)

    sent = 0
    pool = candidates(cfg)
    for n in range(args.start, args.start + args.count):
        print(f"\n=== 第 {sent + 1}/{args.count} 篇：候选 {n} ===")
        if publish_one(cfg, transport, pool[n - 1], n) == "stop":
            break
        sent += 1
        if sent < args.count:
            print(f"等 {args.sleep}s ...")
            time.sleep(args.sleep)
    print(f"\n完成：发帖 {sent} 篇；捕获 → {capture.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
