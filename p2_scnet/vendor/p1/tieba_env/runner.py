"""轨迹采集循环 + 轨迹结构 + ShareGPT 导出。

轨迹字段是 P2 的 Runner 与 P3 的验证器之间唯一的契约（PLAN.md 4.2 硬依赖）：
    trajectory_id / group / scenario / task / tool_calls / final_answer
    steps / hit_step_limit / model_errors / truth / local_attempt_log

现在只实现 ScriptedPolicy 驱动的版本，够跑单元测试与自检。
接 vLLM 的模型策略属于 D3 的 P2 工作，实现时只需满足 Policy 协议。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from .adapter import ModelToolCallError, TiebaAdapter
from .env import MockTiebaEnv
from .scenarios import Scenario
from .tools import SYSTEM_PROMPT


@dataclass
class ToolAction:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class FinalAction:
    text: str


Action = ToolAction | FinalAction


class Policy(Protocol):
    """策略只看到历史观测，看不到环境真值。"""

    def act(self, history: Sequence[dict[str, Any]], scenario: Scenario) -> Action: ...


def run_episode(
    env: MockTiebaEnv,
    adapter: TiebaAdapter,
    scenario: Scenario,
    policy: Policy,
    *,
    group: str = "default",
    backend_name: str = "mock",
    trajectory_id: str | None = None,
) -> dict[str, Any]:
    """跑一条轨迹。每条轨迹开始前重置环境，保证轨迹独立、可复现。"""
    env.reset(**scenario.env_kwargs())
    adapter.reset_log()

    history: list[dict[str, Any]] = []
    model_errors: list[dict[str, Any]] = []
    final_answer: str | None = None
    hit_step_limit = False

    for step in range(1, scenario.step_limit + 1):
        action = policy.act(history, scenario)

        if isinstance(action, FinalAction):
            final_answer = action.text
            break

        if not isinstance(action, ToolAction):
            model_errors.append({"step": step, "error": f"非法动作 {action!r}"})
            break

        try:
            result, observation = adapter.call(action.name, action.arguments)
        except ModelToolCallError as exc:
            # 工具名不存在 / 参数不合法：算模型错误，不算工具不可用
            model_errors.append(
                {"step": step, "error": str(exc), "tool_name": action.name, "arguments": action.arguments}
            )
            break

        history.append(
            {
                "step": step,
                "tool_name": action.name,
                "arguments": action.arguments,
                "result": result,
                "observation": observation,
            }
        )
    else:
        hit_step_limit = True

    return {
        "trajectory_id": trajectory_id or f"traj-{uuid.uuid4().hex[:12]}",
        "group": group,
        "backend": backend_name,
        "system_prompt": SYSTEM_PROMPT,
        "task": scenario.task,
        "scenario": scenario.to_record(),
        "tool_calls": history,
        "final_answer": final_answer,
        "steps": len(history),
        "hit_step_limit": hit_step_limit,
        "model_errors": model_errors,
        "truth": env.truth(),
        "local_attempt_log": adapter.local_log,
    }


def trajectory_to_sharegpt(traj: dict[str, Any], system_prompt: str | None = None) -> dict[str, Any]:
    """转 LLaMA-Factory 的 ShareGPT 多轮格式。

    注意：只有验证通过的轨迹才应该进这里。含错误工具调用的失败轨迹必须先人工
    修正，否则模型会学会重发和错误重试（PLAN.md 5.3）。
    """
    conversations: list[dict[str, str]] = [
        {"from": "system", "value": system_prompt or traj.get("system_prompt") or SYSTEM_PROMPT},
        {"from": "human", "value": traj["task"]},
    ]
    for call in traj["tool_calls"]:
        payload = json.dumps(
            {"name": call["tool_name"], "arguments": call["arguments"]}, ensure_ascii=False
        )
        conversations.append({"from": "gpt", "value": f"<tool_call>{payload}</tool_call>"})
        conversations.append(
            {"from": "tool", "value": json.dumps(call["result"], ensure_ascii=False)}
        )
    conversations.append({"from": "gpt", "value": traj.get("final_answer") or ""})
    return {"conversations": conversations}
