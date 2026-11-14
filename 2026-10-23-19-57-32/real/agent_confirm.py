"""真实确认实验（PLAN D6）：C 组 adapter 在真实贴吧跑 5-10 次，人工盯。

分工：本脚本在 Mac 上跑（浏览器在这里），模型推理在集群（8 GB 本机装不下 9B）：
    集群：python real/serve_policy.py --model .../Qwen3.5-9B --adapter .../adapters/C60
    本机：python real/agent_confirm.py --server http://<集群>:8000 --runs 1 --dry-run

与训练/评测的差别只有一个后端：MockBackend -> RealBackend -> RealTransport -> browser_poster.py。
工具名、参数、观测 JSON、系统提示、解码参数全部沿用训练那一套，不做任何改动。

安全闸（任一不满足就不发）：
  * health_check 四项（login/level/editor/list）全过才开跑；
  * 每次 publish 前 PublishGuard.check()（间隔 600 s、24h 内 10 篇、验证码冷却）；
  * --dry-run 时 publish 不落真帖，返回合成结果，只用来验证流程；
  * 每次发帖打印标题与正文，人工确认后才继续。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tieba_env.adapter import RealBackend, TiebaAdapter  # noqa: E402
from tieba_env.realcli import (  # noqa: E402
    CaptureLog, ConfigError, GuardError, PublishGuard, RealCliConfig, RealTransport, health_check,
)


# -- 策略 ------------------------------------------------------------------

def messages_for(task: str, history: list[dict[str, Any]]) -> list[dict[str, str]]:
    """与训练时 p2.messages_for 逐字段一致：user，然后 function/observation 交替。"""
    messages = [{"role": "user", "content": task}]
    for call in history:
        messages.append({"role": "function", "content": json.dumps(
            {"name": call["tool_name"], "arguments": call["arguments"]}, ensure_ascii=False)})
        messages.append({"role": "observation", "content": call["observation"]})
    return messages


def load_candidates(cfg: RealCliConfig) -> list[str]:
    """与 RealTransport._candidates 同一套读法：支持 {"candidates": [...]} 与纯数组。"""
    path = Path(cfg.candidates_path)
    if not path.is_absolute():
        path = ROOT / path
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("candidates") or []
    return [item["content"] if isinstance(item, dict) else str(item) for item in raw]


class RemotePolicy:
    """把一次决策发到集群模型服务；返回 {'kind': 'tool'|'final', ...}。"""

    def __init__(self, server: str, timeout: float = 900.0):
        self.server = server.rstrip("/")
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        with urllib.request.urlopen(f"{self.server}/health", timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def act(self, task: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        body = json.dumps({"task": task, "history": history}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{self.server}/act", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"模型服务不可用: {exc}") from exc


class ReplayPolicy:
    """不发请求的固定动作序列，只用来本地干跑流程（get -> list -> publish -> list）。"""

    PLAN = ("get_candidates", "list_recent_posts", "publish", "list_recent_posts")

    def __init__(self) -> None:
        self.i = 0

    def health(self) -> dict[str, Any]:
        return {"ok": True, "model": "replay", "adapter": None}

    def act(self, task: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        name = self.PLAN[min(self.i, len(self.PLAN) - 1)]
        self.i += 1
        if self.i > len(self.PLAN):
            return {"ok": True, "action": {"kind": "final", "text": "（replay 收尾）"}, "raw": ""}
        if name == "get_candidates":
            args: dict[str, Any] = {}
        elif name == "publish":
            args = {"kw": STATE["kw"], "title": STATE["title"], "content": STATE["content"]}
        else:
            args = {"kw": STATE["kw"], "limit": 20}
        return {"ok": True, "raw": f"<replay {name}>", "finish_reason": "stop",
                "action": {"kind": "tool", "tool_name": name, "arguments": args}, "error": None}


# -- 后端 ------------------------------------------------------------------

class DryRunBackend:
    """真实后端只读，publish 换成合成结果：用来验证整条链路而不真的发帖。"""

    def __init__(self, inner: RealBackend):
        self.inner = inner

    def get_candidates(self):
        return self.inner.get_candidates()

    def list_recent_posts(self, kw, limit):
        return self.inner.list_recent_posts(kw, limit)

    def publish(self, kw, title, content):
        print(f"\n[dry-run] 本应发布到「{kw}」：\n  标题：{title}\n  正文：{content}\n")
        return {"status": "ok", "post_id": "dry-run", "kw": kw, "title": title, "content": content}


# -- 主流程 ----------------------------------------------------------------

STATE: dict[str, Any] = {}


def confirm_publish(kw: str, title: str, content: str, assume_yes: bool) -> bool:
    print(f"\n即将发帖 → 「{kw}」吧")
    print(f"  标题（{len(title)} 字）：{title}")
    print(f"  正文（{len(content)} 字）：{content}")
    if assume_yes:
        return True
    answer = input("确认发布？[y/N] ").strip().lower()
    return answer in ("y", "yes")


def run_once(policy, adapter: TiebaAdapter, args, guard: PublishGuard, out) -> dict[str, Any]:
    task = args.task
    history: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    final_text = ""
    aborted = None

    for step in range(1, args.max_steps + 1):
        response = policy.act(task, history)
        action = response.get("action")
        if action is None:
            aborted = f"parse_error: {response.get('error')}"
            print(f"[step {step}] 解析失败：{response.get('error')}")
            break
        if action["kind"] == "final":
            final_text = action["text"]
            print(f"[step {step}] 模型收尾：{final_text}")
            break

        name = action["tool_name"]
        arguments = action["arguments"]
        print(f"[step {step}] {name} {json.dumps(arguments, ensure_ascii=False)[:160]}")

        if name == "publish":
            if not args.dry_run:
                guard.check()
                if not confirm_publish(arguments["kw"], arguments["title"], arguments["content"],
                                       args.yes):
                    aborted = "human_rejected_publish"
                    print("人工拒绝，停止本轮。")
                    break
        result, observation = adapter.call(name, arguments)
        if name == "publish" and not args.dry_run:
            if result.get("status") == "ok":
                guard.record_publish()
            elif result.get("status") == "unknown" and (result.get("reason") or "") == "captcha":
                guard.record_captcha()

        history.append({"tool_name": name, "arguments": arguments, "observation": observation})
        steps.append({"step": step, "tool": name, "arguments": arguments, "result": result,
                      "observation": observation, "ts": datetime.now().astimezone().isoformat()})
        out.write(json.dumps(dict(step=step, tool=name, arguments=arguments, result=result),
                             ensure_ascii=False) + "\n")
        out.flush()

    return {"steps": steps, "final_answer": final_text, "aborted": aborted,
            "run_id": uuid.uuid4().hex[:12]}


def summarize(record: dict[str, Any]) -> str:
    steps = record["steps"]
    publishes = [s for s in steps if s["tool"] == "publish"]
    first_unknown = next((i for i, s in enumerate(steps)
                          if s["tool"] == "publish" and s["result"].get("status") == "unknown"), None)
    after = None
    if first_unknown is not None and first_unknown + 1 < len(steps):
        after = steps[first_unknown + 1]["tool"]
    elif first_unknown is not None:
        after = "final"
    return (f"步数 {len(steps)}｜publish {len(publishes)} 次｜"
            f"unknown 后首动作 {after or '—'}｜收尾：{record['final_answer'][:60]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="真实确认实验（人工盯）")
    parser.add_argument("--config", default=str(ROOT / "real" / "config.json"))
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--policy", default="remote", choices=("remote", "replay"))
    parser.add_argument("--task", default="从候选中选一个热梗，发布到「{kw}」吧。发布前检查重复。")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=16)
    parser.add_argument("--sleep", type=float, default=600.0, help="两轮之间的间隔秒数")
    parser.add_argument("--dry-run", action="store_true", help="publish 不发真帖")
    parser.add_argument("--yes", action="store_true", help="跳过人工确认（仅限 dry-run）")
    parser.add_argument("--skip-health", action="store_true", help="跳过四项健康检查（不推荐）")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    cfg = RealCliConfig.load(args.config)
    STATE.update(kw=cfg.kw, title="", content="")
    run_tag = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = Path(args.out or (ROOT / "artifacts" / "runs" / f"real_confirm_{run_tag}.jsonl"))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    capture = CaptureLog(ROOT / "real" / "captures", run_tag)
    transport = RealTransport(cfg=cfg, capture=capture)
    backend: Any = DryRunBackend(RealBackend(transport)) if args.dry_run else RealBackend(transport)
    adapter = TiebaAdapter(backend)
    guard = PublishGuard(cfg)
    policy = ReplayPolicy() if args.policy == "replay" else RemotePolicy(args.server)

    print(f"模型服务：{policy.health()}")
    if not args.skip_health:
        health = health_check(transport, cfg)
        print("健康检查：", json.dumps(health, ensure_ascii=False))
        if not health.get("ok"):
            print("四项健康检查未全过，按 PLAN 6.1 直接退出（不计入任何统计）。")
            return 2

    task = args.task.format(kw=cfg.kw)
    records = []
    for index in range(args.runs):
        print(f"\n===== 第 {index + 1}/{args.runs} 轮 =====")
        # 候选里挑一条作为本条要发的内容（直接读文件，不额外占一次工具调用）
        pool = load_candidates(cfg)
        if not pool:
            print("候选为空，停止。")
            break
        STATE.update(content=pool[index % len(pool)], title=pool[index % len(pool)][:31])
        with out_path.open("a", encoding="utf-8") as out:
            record = run_once(policy, adapter, args, guard, out)
        record["index"] = index
        records.append(record)
        print("→", summarize(record))
        if index + 1 < args.runs:
            time.sleep(args.sleep)

    print(f"\n轨迹写入 {out_path}")
    for record in records:
        print(" -", summarize(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
