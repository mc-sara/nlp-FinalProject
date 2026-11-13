"""几种可执行策略，用来给模拟器自检和给 A 组基线做对照。

这些策略的存在是为了证明模拟器能区分好坏行为（而不是为了当模型用）：
  - ReferencePolicy          D1 实测的人工最优策略的可执行版本
  - QueryOncePolicy          查一次就下结论 —— 长延迟下会误判
  - ResubmitImmediatelyPolicy 不确定就重发 —— 产生重复帖的典型失败

注意 ReferencePolicy 用的是"已知回查延迟上界"的查证预算，它扮演的是人类专家，
不是模型。模型必须自己学会这个预算，这正是 LoRA 要学的东西。
"""

from __future__ import annotations

import random
from typing import Any, Sequence

from .runner import FinalAction, ToolAction
from .scenarios import Scenario, _make_title
from .types import STATUS_ERROR, STATUS_OK, STATUS_REJECTED, STATUS_UNKNOWN

# 收尾措辞池：语义与真值一致，只换说法；按 scenario.seed 选，保证可复现。
OK_PHRASES = (
    "已在「{kw}」吧发布，post_id={post_id}。",
    "发帖完成，吧页上已经能看到这条，帖子 ID {post_id}。",
    "发布成功，吧里最新一条就是它（ID {post_id}）。",
    "已经发出去了，post_id={post_id}，刷新吧页即可看到。",
    "搞定，{post_id} 号帖子已发到「{kw}」吧。",
    "发布完成，post_id 为 {post_id}。",
)
FAIL_PHRASES = (
    "发布未成功，状态 {status}，原因 {reason}。",
    "这次没发出去：{status}／{reason}，需要换条件再试。",
    "帖子被挡住了（{reason}），没有发布成功。",
    "提交被拒，状态 {status}，原因是 {reason}。",
)
FOUND_PHRASES = (
    "回查吧页确认帖子已发布，没有重复提交。",
    "查过了，吧页上已经有这条帖子，就不再重复发。",
    "刷新列表看到了目标帖子，确认发布成功，停止操作。",
    "已核实：帖子在吧页上，避免重复所以不再提交。",
    "翻了吧页，确实已经发出去了，不做重复提交。",
)
UNKNOWN_PHRASES = (
    "多次提交都没有拿到明确结果，吧页也未查到该帖，按未发布处理。",
    "试了几次都只拿到不确定的返回，吧页里也没有，按没发出去处理。",
    "两次提交都没有明确回执，列表也查不到，视作未发布。",
    "反复提交仍无确定结果，吧页也查不到，按未发布收录。",
)


def _phrase(options: Sequence[str], scenario: Scenario, **fields: Any) -> str:
    return random.Random(scenario.seed).choice(options).format(**fields)


def _target(scenario: Scenario) -> tuple[str, str]:
    """按场景 seed 从候选池挑一条发：标题跟着正文走，避开"永远发第一条"的位置捷径。"""
    pool = list(scenario.candidates_pool or [scenario.candidate])
    if len(pool) <= 1:
        return scenario.title, scenario.candidate
    content = pool[random.Random(scenario.seed * 31 + 7).randrange(len(pool))]
    return _make_title(content), content



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
    title, content = _target(scenario)
    return ToolAction("publish", {"kw": scenario.kw, "title": title, "content": content})


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
            return FinalAction(_phrase(OK_PHRASES, scenario, kw=scenario.kw, post_id=post_id))
        if status in (STATUS_REJECTED, STATUS_ERROR):
            reason = (last.get("result") or {}).get("reason")
            return FinalAction(_phrase(FAIL_PHRASES, scenario, status=status, reason=reason))

        content = last["arguments"]["content"]
        if _found_after(queries, last, content, last["arguments"].get("title")):
            return FinalAction(_phrase(FOUND_PHRASES, scenario))

        attempts_after = len([q for q in queries if q["step"] > last["step"]])
        if attempts_after < self.verify_budget:
            return _query_action(scenario)

        if len(publishes) <= self.max_republish:
            return _publish_action(scenario)

        return FinalAction(_phrase(UNKNOWN_PHRASES, scenario))


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
