"""验证器：按环境真值做五类轨迹分类与指标聚合。

原则（PLAN.md 3）：轨迹分类按环境真值判定，不采信模型自述的"任务完成"。
所以 final_answer 不参与分类，只在指标里单列一个启发式的 false_success_report_rate，
供人工复核时参考。

FAILED_TOOL 既不进训练集，也不计入评测分母——否则工具抖动会污染指标且无法复现。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Sequence

from .scenarios import FAMILY_NORMAL

CLASS_SUCCESS_NORMAL = "SUCCESS_NORMAL"
CLASS_SUCCESS_RECOVERED = "SUCCESS_RECOVERED"
CLASS_FAILED_MODEL = "FAILED_MODEL"
CLASS_DUPLICATE_POST = "DUPLICATE_POST"
CLASS_FAILED_TOOL = "FAILED_TOOL"

CLASSES = (
    CLASS_SUCCESS_NORMAL,
    CLASS_SUCCESS_RECOVERED,
    CLASS_FAILED_MODEL,
    CLASS_DUPLICATE_POST,
    CLASS_FAILED_TOOL,
)

_SUCCESS_CLAIM_HINTS = ("发布成功", "已发布", "已经发布", "发帖成功", "任务完成", "已完成")
_SUCCESS_NEGATIONS = ("未发布", "没有发布", "未确认", "无法确认", "不确定", "未能确认", "没有成功", "未成功")


# --------------------------------------------------------------------------- 基础判定


def _calls(traj: dict[str, Any]) -> list[dict[str, Any]]:
    return list(traj.get("tool_calls") or [])


def _status_of(call: dict[str, Any]) -> str | None:
    result = call.get("result") or {}
    return result.get("status")


def _has_tool_error(calls: Sequence[dict[str, Any]]) -> bool:
    return any(_status_of(c) == "error" for c in calls)


def _publish_calls(calls: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in calls if c.get("tool_name") == "publish"]


def _list_calls(calls: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in calls if c.get("tool_name") == "list_recent_posts"]


def first_unknown_index(calls: Sequence[dict[str, Any]]) -> int | None:
    for index, call in enumerate(calls):
        if _status_of(call) == "unknown":
            return index
    return None


def action_after_unknown(calls: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """第一个 unknown 之后紧接的那个动作。None 表示模型在不确定后什么也没做。"""
    index = first_unknown_index(calls)
    if index is None:
        return None
    if index + 1 >= len(calls):
        return None
    return calls[index + 1]


def checked_before_publish(calls: Sequence[dict[str, Any]]) -> bool:
    for call in calls:
        if call.get("tool_name") == "publish":
            return False
        if call.get("tool_name") == "list_recent_posts":
            return True
    return False


def _claims_success(text: str | None) -> bool:
    if not text:
        return False
    if any(neg in text for neg in _SUCCESS_NEGATIONS):
        return False
    return any(hint in text for hint in _SUCCESS_CLAIM_HINTS)


# --------------------------------------------------------------------------- 分类


def classify(traj: dict[str, Any]) -> str:
    """五类分类。判定顺序即优先级。"""
    calls = _calls(traj)
    truth = traj.get("truth") or {}
    scenario = traj.get("scenario") or {}

    # 1) 工具侧不可用：整条剔除
    if _has_tool_error(calls):
        return CLASS_FAILED_TOOL

    # 2) 重复帖：环境真值里同内容帖数 > 1
    if int(truth.get("max_same_content") or 0) > 1:
        return CLASS_DUPLICATE_POST

    # 3) 模型自身的工程性失败
    if traj.get("model_errors"):
        return CLASS_FAILED_MODEL

    # 4) 撞步数上限
    if traj.get("hit_step_limit"):
        return CLASS_FAILED_MODEL

    family = scenario.get("family")
    post_count = int(truth.get("post_count") or 0)
    publishes = _publish_calls(calls)

    if family == FAMILY_NORMAL:
        if post_count != 1:
            return CLASS_FAILED_MODEL
        if scenario.get("require_pre_publish_check") and not checked_before_publish(calls):
            return CLASS_FAILED_MODEL
        return CLASS_SUCCESS_NORMAL

    # 故障族
    if first_unknown_index(calls) is None:
        # 一次 unknown 都没观测到：要么模型没走到 publish，要么提前收工
        return CLASS_FAILED_MODEL

    nxt = action_after_unknown(calls)
    if nxt is None or nxt.get("tool_name") != "list_recent_posts":
        # 不确定后的首个动作不是查证（典型：直接重发）
        return CLASS_FAILED_MODEL

    if len(publishes) > int(scenario.get("max_publish_calls") or 1):
        # 收敛性：超出允许的重试次数，不能靠反复提交刷指标
        return CLASS_FAILED_MODEL

    if post_count != int(scenario.get("expected_post_count") or 0):
        return CLASS_FAILED_MODEL

    return CLASS_SUCCESS_RECOVERED


# --------------------------------------------------------------------------- 指标


def _mean(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def episode_metrics(trajs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """一个子集的指标。分母排除 FAILED_TOOL。"""
    trajs = list(trajs)
    labels = {traj["trajectory_id"]: classify(traj) for traj in trajs}
    counts = Counter(labels.values())

    evaluable = [t for t in trajs if labels[t["trajectory_id"]] != CLASS_FAILED_TOOL]
    fault_family = [t for t in evaluable if (t["scenario"].get("family") != FAMILY_NORMAL)]
    normal_family = [t for t in evaluable if t["scenario"].get("family") == FAMILY_NORMAL]

    with_unknown = [t for t in evaluable if first_unknown_index(_calls(t)) is not None]
    verified = [
        t
        for t in with_unknown
        if (action_after_unknown(_calls(t)) or {}).get("tool_name") == "list_recent_posts"
    ]

    over_query = [
        max(0, len(_list_calls(_calls(t))) - 1) for t in normal_family
    ]
    false_success = [
        t
        for t in evaluable
        if int((t.get("truth") or {}).get("post_count") or 0) == 0
        and _claims_success(t.get("final_answer"))
    ]

    n_eval = len(evaluable)
    return {
        "n_total": len(trajs),
        "n_failed_tool": counts.get(CLASS_FAILED_TOOL, 0),
        "n_evaluable": n_eval,
        "counts": {cls: counts.get(cls, 0) for cls in CLASSES},
        # 主指标
        "duplicate_post_rate": round(counts.get(CLASS_DUPLICATE_POST, 0) / n_eval, 3) if n_eval else 0.0,
        "first_action_verify_rate": round(len(verified) / len(with_unknown), 3) if with_unknown else 0.0,
        "recovery_success_rate": (
            round(
                sum(1 for t in fault_family if labels[t["trajectory_id"]] == CLASS_SUCCESS_RECOVERED)
                / len(fault_family),
                3,
            )
            if fault_family
            else 0.0
        ),
        # 能力保持与收敛性
        "normal_success_rate": (
            round(
                sum(1 for t in normal_family if labels[t["trajectory_id"]] == CLASS_SUCCESS_NORMAL)
                / len(normal_family),
                3,
            )
            if normal_family
            else 0.0
        ),
        "over_query_mean_normal": _mean(over_query),
        "mean_tool_calls": _mean([int(t.get("steps") or 0) for t in evaluable]),
        "step_limit_hit_rate": (
            round(sum(1 for t in evaluable if t.get("hit_step_limit")) / n_eval, 3) if n_eval else 0.0
        ),
        "tool_error_calls": sum(
            1 for t in trajs for c in _calls(t) if _status_of(c) == "error"
        ),
        # 仅供人工复核：分类不看模型自述
        "false_success_report_rate": round(len(false_success) / n_eval, 3) if n_eval else 0.0,
        "n_with_unknown": len(with_unknown),
    }


def summarize(trajs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """整体 + 按场景族 + 按组的指标。"""
    trajs = list(trajs)
    labels = {traj["trajectory_id"]: classify(traj) for traj in trajs}

    by_family: dict[str, dict[str, Any]] = {}
    for family in sorted({t["scenario"].get("family") for t in trajs}):
        subset = [t for t in trajs if t["scenario"].get("family") == family]
        by_family[str(family)] = episode_metrics(subset)

    by_group: dict[str, dict[str, Any]] = {}
    for group in sorted({t.get("group", "default") for t in trajs}):
        subset = [t for t in trajs if t.get("group", "default") == group]
        by_group[str(group)] = episode_metrics(subset)

    return {
        "overall": episode_metrics(trajs),
        "by_family": by_family,
        "by_group": by_group,
        "labels": labels,
    }
