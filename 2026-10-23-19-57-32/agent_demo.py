"""模拟 agent 调用三个工具的最小闭环 —— 以 publish 为主。

    python agent_demo.py                  # 两种策略对照：谨慎 vs 不耐烦
    python agent_demo.py --state unknown_delayed
    python agent_demo.py --state ok

它做的事和真模型完全一样：
  1. 从 `tools.TOOL_SCHEMAS` 拿工具定义（这就是喂给模型的那份）；
  2. 模型每轮产出一个 tool_call（这里用脚本函数顶替模型）；
  3. `adapter.call(name, args)` 执行，拿到**模型可见的字符串** observation；
  4. observation 原样回灌给模型，进入下一轮。

换真模型只需要替换 `Planner` —— 把 planner 函数里"写死的动作序列"改成
"调一次大模型拿 tool_calls"。其余代码一行不用动，因为观测字符串的拼装
全部发生在 adapter 里，和模型无关。
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Callable

from tieba_env.adapter import MockBackend, TiebaAdapter
from tieba_env.env import MockTiebaEnv
from tieba_env.tools import DEFAULT_TASK_TEMPLATE, SYSTEM_PROMPT, TOOL_SCHEMAS
from tieba_env.types import STATUS_OK, STATUS_UNKNOWN, FaultSpec

KW = "测试"
TITLE = "早八人的咖啡因依赖"
CONTENT = "不喝第二杯就写不出第二行代码"

# 场景 -> 故障注入。回查延迟按"查询次数"建模：visible_after=2 表示
# 发布后前 2 次 list_recent_posts 查不到，第 3 次才命中。
STATES: dict[str, dict[int, FaultSpec]] = {
    "ok": {},
    "unknown_delayed": {
        1: FaultSpec(STATUS_UNKNOWN, "submit_timeout", published=True, visible_after=2)
    },
    "unknown_not_published": {
        1: FaultSpec(STATUS_UNKNOWN, "captcha", published=False)
    },
    "rejected": {1: FaultSpec("rejected", "title_too_long")},
    "error": {1: FaultSpec("error", "login_expired")},
    "ok_no_post_id": {1: FaultSpec(STATUS_OK, None, withhold_post_id=True)},
}


# --------------------------------------------------------------------------- 动作


def act_publish() -> dict[str, Any]:
    return {
        "name": "publish",
        "arguments": {"kw": KW, "title": TITLE, "content": CONTENT},
    }


def act_list() -> dict[str, Any]:
    return {"name": "list_recent_posts", "arguments": {"kw": KW, "limit": 20}}


def act_candidates() -> dict[str, Any]:
    return {"name": "get_candidates", "arguments": {}}


# --------------------------------------------------------------------------- 策略
# 签名统一：planner(history) -> 下一个动作 dict，返回 None 表示收工。
# history 里每条是 {"step", "name", "arguments", "result", "observation"}。

Planner = Callable[[list[dict[str, Any]]], dict[str, Any] | None]


def planner_careful(budget: int = 4) -> Planner:
    """谨慎：先查重 → 发 → 不确定就按预算回查，查不到也不重发。"""

    def planner(history: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not history:
            return act_list()                      # 第 1 步：查重

        publish = next((h for h in history if h["name"] == "publish"), None)
        if publish is None:
            # 查重通过，可以发
            return act_publish()
        if publish["result"]["status"] == STATUS_OK:
            return None                            # 成功，收工

        # 非 ok：只有两种情况值得继续 —— unknown 要回查
        if publish["result"]["status"] == STATUS_UNKNOWN:
            after = history[history.index(publish) + 1:]
            lists = [h for h in after if h["name"] == "list_recent_posts"]
            if any(h["result"]["posts"] for h in lists):
                return None                        # 查到了，帖子在
            if len(lists) >= budget:
                return None                        # 预算用完，不重发
            return act_list()
        return None                                # rejected / error 没发出去，收工

    return planner


def planner_impatient(budget: int = 1) -> Planner:
    """不耐烦：发完只查一次，查不到就重发 —— 这正是重复帖的成因。"""

    def planner(history: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not history:
            return act_publish()

        last = history[-1]
        if last["name"] == "publish":
            if last["result"]["status"] == STATUS_OK:
                return None
            return act_list()                      # 不确定 → 只查一次
        if last["name"] == "list_recent_posts":
            if last["result"]["posts"]:
                return None
            return act_publish()                   # 查不到 → 直接重发
        return None

    return planner


# --------------------------------------------------------------------------- 主循环


def run_agent(
    adapter: TiebaAdapter,
    task: str,
    planner: Planner,
    *,
    max_steps: int = 16,
    verbose: bool = True,
) -> list[dict[str, Any]]:
    """这就是 agent loop：模型出动作 -> 工具执行 -> 观测回灌。"""
    history: list[dict[str, Any]] = []
    if verbose:
        print(f"[system] {SYSTEM_PROMPT}")
        print(f"[user]   {task}\n")

    for step in range(1, max_steps + 1):
        action = planner(history)
        if action is None:
            if verbose:
                print(f"[{step}] assistant 结束（不再调用工具）")
            return history

        name, arguments = action["name"], action["arguments"]
        # 与 OpenAI 的 tool_call 同形，便于直接喂给模型
        tool_call = {
            "id": f"call_{step}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }
        result, observation = adapter.call(name, arguments)
        history.append(
            {"step": step, "name": name, "arguments": arguments,
             "result": result, "observation": observation}
        )

        if verbose:
            print(f"[{step}] assistant -> {name}({json.dumps(arguments, ensure_ascii=False)})")
            print(f"    tool    <- {observation}")

    if verbose:
        print(f"\n！达到 max_steps={max_steps} 仍未收敛")
    return history


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟 agent 调用 publish")
    ap.add_argument("--state", default="unknown_delayed", choices=tuple(STATES))
    ap.add_argument("--budget", type=int, default=4, help="谨慎策略的回查预算")
    args = ap.parse_args()

    task = DEFAULT_TASK_TEMPLATE.format(kw=KW)
    print("═" * 70)
    print(f"场景：{args.state}　工具数：{len(TOOL_SCHEMAS)}（{', '.join(t['function']['name'] for t in TOOL_SCHEMAS)}）")
    print("═" * 70)

    rows = []
    for label, planner in (
        ("谨慎（回查到底）", planner_careful(args.budget)),
        ("不耐烦（查一次就重发）", planner_impatient()),
    ):
        print(f"\n{'─' * 70}\n▶ {label}\n{'─' * 70}")
        env = MockTiebaEnv(candidates=["热梗1", "热梗2"], delay_k=2)
        env.reset(fault_plan=STATES[args.state])
        adapter = TiebaAdapter(MockBackend(env))
        adapter.reset_log()

        history = run_agent(adapter, task, planner)
        truth = env.truth()
        rows.append((label, len(history), truth))

        print(f"\n  环境真值（模型看不到）：帖子 {truth['post_count']} 篇，"
              f"发布尝试 {truth['publish_count']} 次，"
              f"同内容最多 {truth['max_same_content']} 篇，查询 {truth['list_calls']} 次")

    print(f"\n{'═' * 70}\n对照\n{'═' * 70}")
    for label, calls, truth in rows:
        print(f"  {label:22s} 调用 {calls} 次 | 帖子 {truth['post_count']} 篇 | "
              f"发布 {truth['publish_count']} 次 | 重复帖 {'是' if truth['max_same_content'] > 1 else '否'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
