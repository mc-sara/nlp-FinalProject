"""真实贴吧侧单条轨迹运行器。

    python real_run.py health                      # 只读：四项健康检查
    python real_run.py verify --title "标题"        # 只读：查吧页有没有这篇
    python real_run.py probe-delay --candidate 1   # 实发一篇并测回查延迟（D1 核心）
    python real_run.py publish --candidate 1 --yes # 实发一篇，全程记录

三条安全闸，缺一条都发不出去：
  1. kw 必须在 config.json 的 allowed_publish_kw 里（默认空 → 谁也发不出去）；
  2. 必须显式 --yes（非交互环境没有确认机会，所以要显式表态）；
  3. PublishGuard：24h 上限、验证码冷却。（发帖间隔默认关闭，见 publish_interval_seconds）

加 --simulate 可以在不起浏览器、不发真实请求的情况下把整条链跑一遍。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from tieba_env.adapter import MockBackend, RealBackend, TiebaAdapter
from tieba_env.env import MockTiebaEnv
from tieba_env.realcli import (
    ConfigError,
    GuardError,
    PublishGuard,
    RealCliConfig,
    build_transport,
    health_check,
    poll_for_title,
)
from tieba_env.types import STATUS_ERROR, STATUS_OK, STATUS_UNKNOWN, FaultSpec

ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs" / "real"
SAMPLES_DIR = ROOT / "real" / "samples"


# --------------------------------------------------------------------------- 模拟后端


class SimulatedTransport:
    """用 MockTiebaEnv 顶替真实工具，让整条编排链可跑、可测。"""

    def __init__(self, cfg: RealCliConfig, fault: str = "ok") -> None:
        self.cfg = cfg
        self.env = MockTiebaEnv(candidates=["热梗1", "热梗2"], delay_k=max(1, int(cfg.recheck_interval_seconds)))
        plans = {
            "ok": {},
            "unknown_visible": {1: FaultSpec(STATUS_UNKNOWN, "submit_timeout", published=True, visible_after=0)},
            "unknown_delayed": {1: FaultSpec(STATUS_UNKNOWN, "submit_timeout", published=True, visible_after=2)},
            "unknown_not_published": {1: FaultSpec(STATUS_UNKNOWN, "captcha", published=False)},
            "rejected": {1: FaultSpec("rejected", "title_too_long")},
            "tool_error": {1: FaultSpec(STATUS_ERROR, "login_expired")},
        }
        if fault not in plans:
            raise ConfigError(f"未知 simulate fault: {fault}")
        self.env.reset(fault_plan=plans[fault], seed=0)
        self.fault = fault

    def __call__(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        if name == "login":
            return {"ok": True, "user": "simulated", "uid": "0", "tbs_ok": True}
        if name == "level":
            return {"kw": args.get("kw"), "level": 6, "liked": True, "fid": "0", "signed": False}
        if name == "editor":
            return {"editor_ready": True, "detail": "模拟后端：标题框/正文框/发布键 全部命中"}
        if name in ("list", "list_recent_posts"):
            return self.env.list_recent_posts(args.get("kw"), args.get("limit"))
        if name == "get_candidates":
            return self.env.get_candidates()
        if name == "publish":
            return self.env.publish(args["kw"], args["title"], args["content"]).observation
        raise ConfigError(f"未知动作 {name}")

    def truth(self) -> dict[str, Any]:
        return self.env.truth()


# --------------------------------------------------------------------------- 公共


def load_config(path: str) -> RealCliConfig:
    cfg = RealCliConfig.load(path)
    return cfg


def make_adapter(cfg: RealCliConfig, simulate: bool, fault: str) -> tuple[TiebaAdapter, Any, str]:
    if simulate:
        transport = SimulatedTransport(cfg, fault)
        return TiebaAdapter(RealBackend(transport)), transport, "simulated"
    transport = build_transport(cfg)
    return TiebaAdapter(RealBackend(transport)), transport, "real"


def title_from(content: str, limit: int) -> str:
    head = content.split("：")[0].split("，")[0].strip()
    if len(head) > limit:
        head = head[:limit]
    return head or "分享"


def write_trajectory(payload: dict[str, Any]) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{payload['trajectory_id']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def print_health(report: dict[str, Any]) -> None:
    print("\n=== 发帖前健康检查（PLAN 6.1）===")
    for name, item in report["checks"].items():
        flag = "通过" if item["ok"] else "失败"
        skip = "（跳过）" if item.get("skipped") else ""
        print(f"  [{flag}]{skip} {name:8s} {item['detail']}")
    print(f"  总判定：{'可以继续' if report['ok'] else '整条轨迹应记 FAILED_TOOL'}")


def resolve_content(args: argparse.Namespace, cfg: RealCliConfig) -> tuple[str, str]:
    """返回 (正文, 候选自带的标题)。用 --content 自己给正文时标题为空串。"""
    if args.content:
        return args.content, ""
    path = Path(cfg.candidates_path)
    if not path.is_absolute():
        path = ROOT / path
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("candidates") or []
    # 与 publish_api.get_candidates 保持同一套过滤规则，否则两边 --candidate N 会指到不同条目
    def _body(entry: Any) -> str:
        raw_body = entry.get("content") if isinstance(entry, dict) else entry
        return str(raw_body or "").strip()

    items = [entry for entry in raw if _body(entry)]
    ref = str(args.candidate).strip()
    if ref.isdigit():
        index = int(ref)
        if not 1 <= index <= len(items):
            raise ConfigError(f"--candidate 越界：1..{len(items)}（收到 {index}）")
        item = items[index - 1]
    else:
        # id 写法（如 c01）—— 纯数字一律当序号，其余按 id 忽略大小写匹配
        hit = [
            entry
            for entry in items
            if isinstance(entry, dict) and str(entry.get("id") or "").lower() == ref.lower()
        ]
        if not hit:
            ids = ", ".join(
                str(entry.get("id")) for entry in items if isinstance(entry, dict) and entry.get("id")
            )
            raise ConfigError(f"找不到候选 {ref!r}。现有 id：{ids or '（这批候选没有 id）'}")
        item = hit[0]
    if isinstance(item, dict):
        content = str(item.get("content") or "")
        title = str(item.get("title") or "")
    else:
        content, title = str(item), ""
    if not content.strip():
        raise ConfigError(f"第 {index} 条候选没有 content")
    return content, title


# --------------------------------------------------------------------------- 子命令


def cmd_health(cfg: RealCliConfig, transport: Any, args: argparse.Namespace) -> int:
    report = health_check(transport, cfg)
    print_health(report)
    return 0 if report["ok"] else 2


def cmd_verify(cfg: RealCliConfig, adapter: TiebaAdapter, args: argparse.Namespace) -> int:
    """只读：看一眼吧页里有没有这篇。人工核对时反复用这个。"""
    result, _ = adapter.call("list_recent_posts", {"kw": cfg.kw, "limit": cfg.list_limit})
    posts = result.get("posts") or []
    target = args.title.replace(" ", "")
    hit = [p for p in posts if str(p.get("title", "")).replace(" ", "") == target]
    print(f"读到 {len(posts)} 条，标题命中 {len(hit)} 条")
    for post in hit:
        print(f"  命中：{post.get('post_id')} {post.get('title')}")
    if not hit:
        print("  未命中。注意：吧页列表按最后回复排序，无人回复的新帖可能不在首页。")
    return 0 if hit else 1


def run_publish_flow(
    cfg: RealCliConfig,
    adapter: TiebaAdapter,
    transport: Any,
    args: argparse.Namespace,
    *,
    purpose: str,
    simulate: bool,
    backend_name: str,
) -> int:
    content, cand_title = resolve_content(args, cfg)
    # 优先级：--title > 候选自带标题 > 从正文推（老格式纯字符串候选走到这一步）
    title = args.title or cand_title or title_from(content, cfg.title_limit)
    if len(title) > cfg.title_limit:
        title = title[: cfg.title_limit]

    print(f"目标吧：{cfg.kw}　标题：{title}　正文 {len(content)} 字")

    # 闸 1：实发白名单
    if not simulate and cfg.kw not in cfg.allowed_publish_kw:
        print(f"\n拒绝执行：kw={cfg.kw!r} 不在 allowed_publish_kw={cfg.allowed_publish_kw} 里。")
        print("这是防止误发到公共吧的唯一开关。确认是自建吧后再把它加进 real/config.json。")
        return 3
    # 闸 2：显式确认
    if not args.yes and not simulate:
        print("\n这是真实发帖，会真的发出去。确认无误请加 --yes 重跑。")
        return 3
    # 闸 3：限额
    guard = PublishGuard(cfg)
    if not simulate:
        try:
            guard.check()
        except GuardError as exc:
            print(f"\n限额守卫拒绝：{exc}")
            return 3

    report = health_check(transport, cfg)
    print_health(report)
    if not report["ok"]:
        payload = {
            "trajectory_id": f"real-{uuid.uuid4().hex[:8]}",
            "kind": purpose,
            "backend": backend_name,
            "task": f"在「{cfg.kw}」吧发布《{title}》",
            "kw": cfg.kw,
            "health": report,
            "tool_calls": [],
            "verdict": "FAILED_TOOL",
            "truth": {"post_count": None, "confirmed": False},
        }
        path = write_trajectory(payload)
        print(f"\n健康检查未过，已记 FAILED_TOOL 并落盘：{path}")
        return 2

    # 查重（发帖前）
    pre, pre_obs = adapter.call("list_recent_posts", {"kw": cfg.kw, "limit": cfg.list_limit})
    duplicate = [
        p for p in (pre.get("posts") or [])
        if str(p.get("title", "")).replace(" ", "") == title.replace(" ", "")
    ]
    if duplicate:
        print(f"\n吧页已有同标题帖子 {len(duplicate)} 条，不重复发：{duplicate[0].get('post_id')}")
        return 4

    if args.dry_run:
        print("\n--dry-run：跳过实际发帖。将要写入的帖子文件内容：")
        print(f"---\nkw: {cfg.kw}\ntitle: {title}\n---\n\n{content}\n")
        return 0

    print("\n提交发帖 ...")
    publish_result, publish_obs = adapter.call("publish", {"kw": cfg.kw, "title": title, "content": content})
    if not simulate:
        guard.record_publish()
    status = publish_result.get("status")
    print(f"  工具返回：{publish_obs}")

    if status == STATUS_ERROR:
        # 工具侧不可用 → 整条作废，但**不进冷却**：冷却只留给验证码，否则一次网络抖动会停一天
        print("  工具侧不可用，整条记 FAILED_TOOL（不进训练集，也不进分母）。")
    elif publish_result.get("reason") == "captcha":
        if not simulate:
            guard.record_captcha()
        print("  触发验证码，已停止当天实验（PLAN 6.2）。")

    # 回查真值
    print("\n回查吧页（这既是查证，也是回查延迟实测）...")
    poll = poll_for_title(
        lambda: adapter.call("list_recent_posts", {"kw": cfg.kw, "limit": cfg.list_limit})[0],
        title,
        polls=cfg.recheck_polls,
        interval=0.0 if simulate else cfg.recheck_interval_seconds,
    )
    print(
        f"  {'命中' if poll['found'] else '未命中'}：第 {poll['matched_at_poll']} 次查询，"
        f"耗时 {poll['elapsed_seconds']}s"
    )

    truth_posts = 1 if poll["found"] else 0
    if simulate and hasattr(transport, "truth"):
        truth_posts = transport.truth()["post_count"]

    verdict = _real_verdict(status, poll, publish_result)
    payload = {
        "trajectory_id": f"real-{uuid.uuid4().hex[:8]}",
        "kind": purpose,
        "backend": backend_name,
        "task": f"在「{cfg.kw}」吧发布《{title}》",
        "kw": cfg.kw,
        "title": title,
        "content": content,
        "health": report,
        "tool_calls": [
            {"step": 1, "tool_name": "list_recent_posts", "arguments": {"kw": cfg.kw, "limit": cfg.list_limit},
             "result": pre, "observation": pre_obs},
            {"step": 2, "tool_name": "publish", "arguments": {"kw": cfg.kw, "title": title, "content": content},
             "result": publish_result, "observation": publish_obs},
            {"step": 3, "tool_name": "list_recent_posts", "arguments": {"kw": cfg.kw, "limit": cfg.list_limit},
             "result": {"posts": [], "poll": poll}, "observation": ""},
        ],
        "publish": publish_result,
        "poll": poll,
        "verdict": verdict,
        "truth": {"post_count": truth_posts, "confirmed": poll["found"]},
        "measured_delay_k": (poll["matched_at_poll"] - 1) if poll["found"] else None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": publish_result.get("_note", ""),
    }
    path = write_trajectory(payload)
    print(f"\n判定：{verdict}　真值：{truth_posts} 篇　轨迹已落盘：{path}")

    if purpose == "probe-delay" and poll["found"]:
        _append_delay_sample(cfg, poll, path)
    return 0


def _real_verdict(status: str | None, poll: dict[str, Any], publish: dict[str, Any]) -> str:
    """真实侧判定（不套用五类标签：真实轨迹没有受控的期望真值）。

    只用三条事实：工具说了什么、吧页查到了什么、两者是否自相矛盾。
    """
    if status == STATUS_ERROR:
        return "FAILED_TOOL"
    if poll["found"] and status == STATUS_OK:
        return "CONFIRMED_PUBLISHED"
    if poll["found"] and status == STATUS_UNKNOWN:
        return "RECOVERED_CONFIRMED"      # 不确定后查证发现已发布
    if not poll["found"] and status == STATUS_OK:
        return "SUSPECT_NOT_VISIBLE"      # 工具说成功但吧页没看到，需人工核对
    if not poll["found"] and status == STATUS_UNKNOWN:
        return "NOT_PUBLISHED_OR_HIDDEN"  # 查不到，可能是没发出去，也可能被审核/列表延迟
    return f"OTHER({publish.get('reason')})"


def _append_delay_sample(cfg: RealCliConfig, poll: dict[str, Any], traj_path: Path) -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    line = {
        "kw": cfg.kw,
        "poll_interval_seconds": cfg.recheck_interval_seconds,
        "matched_at_poll": poll["matched_at_poll"],
        "elapsed_seconds": poll["elapsed_seconds"],
        "measured_delay_k": poll["matched_at_poll"] - 1,
        "trajectory": traj_path.name,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    with (SAMPLES_DIR / "delay_samples.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"  已记入回查延迟样本：{SAMPLES_DIR / 'delay_samples.jsonl'}")


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="真实贴吧侧运行器")
    ap.add_argument("--config", default=str(ROOT / "real" / "config.json"))
    ap.add_argument("--simulate", action="store_true", help="用模拟后端跑通编排，不碰真实贴吧")
    ap.add_argument("--simulate-fault", default="ok",
                    choices=("ok", "unknown_visible", "unknown_delayed", "unknown_not_published",
                             "rejected", "tool_error"))
    ap.add_argument("--no-editor", action="store_true", help="跳过编辑器探测（不启动浏览器）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    # 允许把 --no-editor 写在子命令后面（argparse 的默认位置很容易踩错）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-editor", action="store_true", default=argparse.SUPPRESS,
                        help="跳过编辑器探测（不启动浏览器）")

    sub.add_parser("health", parents=[common], help="只读：四项健康检查")

    sp_verify = sub.add_parser("verify", parents=[common], help="只读：查吧页有没有指定标题")
    sp_verify.add_argument("--title", required=True)

    for name, helptext in (("publish", "实发一篇并记录轨迹"), ("probe-delay", "实发一篇并测回查延迟")):
        sp = sub.add_parser(name, parents=[common], help=helptext)
        sp.add_argument("--content", default="", help="正文")
        sp.add_argument(
            "--candidate",
            default="1",
            metavar="N|ID",
            help="候选序号（从 1 开始）或 id（如 c01），两种写法等价",
        )
        sp.add_argument("--title", default="", help="标题，默认从正文截取")
        sp.add_argument("--dry-run", action="store_true", help="只打印将要发出的内容")
        sp.add_argument("--yes", action="store_true", help="确认真实发帖")

    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
        if getattr(args, "no_editor", False):
            cfg.editor_check = False
        adapter, transport, backend_name = make_adapter(cfg, args.simulate, args.simulate_fault)
    except (ConfigError, json.JSONDecodeError) as exc:
        print(f"配置有问题：{exc}")
        return 3

    if args.cmd == "health":
        return cmd_health(cfg, transport, args)
    if args.cmd == "verify":
        return cmd_verify(cfg, adapter, args)
    try:
        return run_publish_flow(
            cfg, adapter, transport, args,
            purpose=args.cmd, simulate=args.simulate, backend_name=backend_name,
        )
    except GuardError as exc:
        print(f"限额守卫拒绝：{exc}")
        return 3
    except ConfigError as exc:
        print(f"执行失败：{exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
