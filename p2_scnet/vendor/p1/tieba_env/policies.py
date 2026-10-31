"""几种可执行策略，用来给模拟器自检和给 A 组基线做对照。

这些策略的存在是为了证明模拟器能区分好坏行为（而不是为了当模型用）：
  - ReferencePolicy          D1 实测的人工最优策略的可执行版本
  - QueryOncePolicy          查一次就下结论 —— 长延迟下会误判
  - ResubmitImmediatelyPolicy 不确定就重发 —— 产生重复帖的典型失败

注意 ReferencePolicy 用的是"已知回查延迟上界"的查证预算，它扮演的是人类专家，
不是模型。模型必须自己学会这个预算，这正是 LoRA 要学的东西。
"""

from __future__ import annotations

from typing import Any, Sequence

from .runner import FinalAction, ToolAction
from .scenarios import Scenario
from .types import STATUS_ERROR, STATUS_OK, STATUS_REJECTED, STATUS_UNKNOWN


def _publishes(history: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in history if c.get("tool_name") == "publish"]


def _queries(history: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in history if c.get("tool_name") == "list_recent_posts"]


def _status(call: dict[str, Any]) -> str | None:
    return (call.get("result") or {}).get("status")


def _found_after(
    queries: Sequence[dict[str, Any]],
    publish: dict[str, Any],
    content: str,
    title: str | None = None,
) -> bool:
    """按正文或标题匹配。

    真实吧页只有标题（没有正文），所以查证必须能退回标题比对，否则策略在真实侧永远"查不到"。
    """
    after = [q for q in queries if q["step"] > publish["step"]]
    return any(
        post.get("content") == content or (title is not None and post.get("title") == title)
        for q in after
        for post in (q.get("result") or {}).get("posts", [])
    )


def _publish_action(scenario: Scenario) -> ToolAction:
    return ToolAction(
        "publish",
        {"kw": scenario.kw, "title": scenario.title, "content": scenario.candidate},
    )


def _query_action(scenario: Scenario) -> ToolAction:
    return ToolAction("list_recent_posts", {"kw": scenario.kw, "limit": 20})


class ReferencePolicy:
    """不确定 → 查证（必要时连续查，直到超过回查延迟上界）→ 据实决定。"""

    def __init__(self, *, verify_budget: int = 3, max_republish: int = 1) -> None:
        self.verify_budget = verify_budget
        self.max_republish = max_republish

    def act(self, history: Sequence[dict[str, Any]], scenario: Scenario) -> Any:
        if not any(c.get("tool_name") == "get_candidates" for c in history):
            return ToolAction("get_candidates", {})

        publishes = _publishes(history)
        queries = _queries(history)

        if not publishes:
            if not queries:
                return _query_action(scenario)
            return _publish_action(scenario)

        last = publishes[-1]
        status = _status(last)

        if status == STATUS_OK:
            post_id = (last.get("result") or {}).get("post_id")
            return FinalAction(f"已在「{scenario.kw}」吧发布，post_id={post_id}。")
        if status in (STATUS_REJECTED, STATUS_ERROR):
            reason = (last.get("result") or {}).get("reason")
            return FinalAction(f"发布未成功，状态 {status}，原因 {reason}。")

        content = last["arguments"]["content"]
        if _found_after(queries, last, content, scenario.title):
            return FinalAction("回查吧页确认帖子已发布，没有重复提交。")

        attempts_after = len([q for q in queries if q["step"] > last["step"]])
        if attempts_after < self.verify_budget:
            return _query_action(scenario)

        if len(publishes) <= self.max_republish:
            return _publish_action(scenario)

        return FinalAction("多次提交都没有拿到明确结果，吧页也未查到该帖，按未发布处理。")


class QueryOncePolicy(ReferencePolicy):
    """只查一次：短延迟够用，长延迟会把"已发布"误判成"未发布"。"""

    def __init__(self) -> None:
        super().__init__(verify_budget=1, max_republish=1)


class ResubmitImmediatelyPolicy:
    """不确定就直接重发，连查都不查。"""

    def __init__(self, *, max_publish: int = 3) -> None:
        self.max_publish = max_publish

    def act(self, history: Sequence[dict[str, Any]], scenario: Scenario) -> Any:
        if not any(c.get("tool_name") == "get_candidates" for c in history):
            return ToolAction("get_candidates", {})

        publishes = _publishes(history)
        queries = _queries(history)

        if not publishes:
            if not queries:
                return _query_action(scenario)
            return _publish_action(scenario)

        last = publishes[-1]
        status = _status(last)
        if status == STATUS_OK:
            post_id = (last.get("result") or {}).get("post_id")
            return FinalAction(f"已在「{scenario.kw}」吧发布，post_id={post_id}。")
        if status in (STATUS_REJECTED, STATUS_ERROR):
            return FinalAction(f"发布失败，原因 {(last.get('result') or {}).get('reason')}。")
        if status == STATUS_UNKNOWN and len(publishes) < self.max_publish:
            return _publish_action(scenario)
        return FinalAction("提交后没有拿到明确结果，已再次提交。")
