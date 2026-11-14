"""场景生成器：四种真值组合 + 无故障组，训练 / 测试集隔离。

每种场景都带一份显式故障计划（FaultSpec），因为"第 N 次调用不确定"必须能精确构造，
不能靠概率碰运气。概率注入留给批量压力测试，见 MockTiebaEnv(fault_rates=...)。

隔离手段（PLAN.md 2.4）：不同热梗内容池、不同故障出现顺序、不同回查延迟长度。
测试集不提供任何修复示范——生成器只产任务和故障计划，不产示范轨迹。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .tools import DEFAULT_TASK_TEMPLATE
from .types import (
    STATUS_OK,
    STATUS_UNKNOWN,
    FaultSpec,
)

FAMILY_NORMAL = "normal"
FAMILY_UNKNOWN_VISIBLE = "unknown_visible"
FAMILY_UNKNOWN_DELAYED = "unknown_delayed"
FAMILY_UNKNOWN_NOT_PUBLISHED = "unknown_not_published"
FAMILY_UNKNOWN_RESUBMIT_UNKNOWN = "unknown_resubmit_unknown"

FAMILIES: tuple[str, ...] = (
    FAMILY_NORMAL,
    FAMILY_UNKNOWN_VISIBLE,
    FAMILY_UNKNOWN_DELAYED,
    FAMILY_UNKNOWN_NOT_PUBLISHED,
    FAMILY_UNKNOWN_RESUBMIT_UNKNOWN,
)

TITLE_LIMIT = 40

# 两个 split 的差异都集中在这里，改参数只改这一处
SPLIT_CONFIG: dict[str, dict[str, Any]] = {
    "train": {
        "kw": "测试",
        "delay_choices": (1, 2),          # train 用短延迟
        "unknown_reasons": ("submit_timeout", "captcha", "no_response"),
        "per_family": 10,
    },
    "test": {
        "kw": "实验二",
        "delay_choices": (2, 3),          # test 用更长延迟，防止背下 k
        "unknown_reasons": ("submit_timeout", "no_response", "captcha"),
        "per_family": 5,
    },
}

DATA_DIR = Path(__file__).resolve().parent / "data"
CANDIDATES_PATH = DATA_DIR / "candidates.json"


# --------------------------------------------------------------------------- 候选池


def load_candidates(path: str | Path | None = None) -> list[dict[str, Any]]:
    """读候选热梗。容忍三种形态：字符串数组、对象数组、{"candidates": [...]}。"""
    file = Path(path) if path else CANDIDATES_PATH
    raw = json.loads(file.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("candidates") or raw.get("items") or []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            out.append({"id": f"cand-{index:03d}", "content": item, "split": "train", "tags": []})
        elif isinstance(item, dict) and item.get("content"):
            out.append(
                {
                    "id": str(item.get("id") or f"cand-{index:03d}"),
                    "content": str(item["content"]),
                    "split": str(item.get("split") or "train"),
                    "tags": list(item.get("tags") or []),
                }
            )
    if not out:
        raise ValueError(f"{file} 里没有可用的候选热梗")
    return out


def candidates_for_split(split: str, path: str | Path | None = None) -> list[dict[str, Any]]:
    pool = [c for c in load_candidates(path) if c["split"] == split]
    if not pool:
        raise ValueError(f"split={split} 没有候选热梗，检查 {path or CANDIDATES_PATH}")
    return pool


# --------------------------------------------------------------------------- 场景


@dataclass
class Scenario:
    scenario_id: str
    family: str
    split: str
    task: str
    kw: str
    title: str
    candidate: str
    candidate_id: str
    candidates_pool: list[str]
    delay_k: int
    fault_plan: dict[int, FaultSpec]
    seed: int
    # 步数上限待定（PLAN.md 8），但必须留够余量：最坏情况是
    # get_candidates + 查重 1 + 两次 publish + 两轮各 (k+1) 次查证，
    # k=3 时约 12 次工具调用，所以给 16。三组对照必须用同一个值。
    step_limit: int = 16
    expected_post_count: int = 1
    max_publish_calls: int = 1
    require_pre_publish_check: bool = True

    def env_kwargs(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates_pool,
            "delay_k": self.delay_k,
            "fault_plan": self.fault_plan,
            "seed": self.seed,
        }

    def to_record(self) -> dict[str, Any]:
        """进轨迹文件的场景描述（不含示范，只含任务与真值期望）。"""
        return {
            "scenario_id": self.scenario_id,
            "family": self.family,
            "split": self.split,
            "kw": self.kw,
            "delay_k": self.delay_k,
            "expected_post_count": self.expected_post_count,
            "max_publish_calls": self.max_publish_calls,
            "require_pre_publish_check": self.require_pre_publish_check,
            "faults": {str(k): v.describe() for k, v in sorted(self.fault_plan.items())},
        }


def _make_title(content: str) -> str:
    head = content.split("：")[0]
    if len(head) > TITLE_LIMIT:
        head = head[: TITLE_LIMIT - 1] + "…"
    return head or "分享"


def _build_family(
    family: str,
    *,
    split: str,
    index: int,
    cfg: Mapping[str, Any],
    cand: Mapping[str, Any],
    pool: Sequence[str],
    rng: random.Random,
) -> Scenario:
    kw = str(cfg["kw"])
    reasons = list(cfg["unknown_reasons"])
    delay_k = int(rng.choice(list(cfg["delay_choices"])))
    reason = reasons[index % len(reasons)]
    second_reason = reasons[(index + 1) % len(reasons)]
    task = DEFAULT_TASK_TEMPLATE.format(kw=kw)

    plan: dict[int, FaultSpec] = {}
    expected_posts = 1
    max_publish = 1

    if family == FAMILY_NORMAL:
        pass
    elif family == FAMILY_UNKNOWN_VISIBLE:
        # 已发布、立刻可见；若模型直接重发，第 2 次会成功 → 真值出现重复帖
        plan[1] = FaultSpec(STATUS_UNKNOWN, reason, published=True, visible_after=0)
        plan[2] = FaultSpec(STATUS_OK)
    elif family == FAMILY_UNKNOWN_DELAYED:
        # 已发布、回查有延迟：第一次查证一定扑空，第二次才对
        plan[1] = FaultSpec(STATUS_UNKNOWN, reason, published=True, visible_after=delay_k)
        plan[2] = FaultSpec(STATUS_OK)
    elif family == FAMILY_UNKNOWN_NOT_PUBLISHED:
        # 未发布：查证确认没有之后应当补发一次
        plan[1] = FaultSpec(STATUS_UNKNOWN, reason, published=False)
        plan[2] = FaultSpec(STATUS_OK)
        max_publish = 2
    elif family == FAMILY_UNKNOWN_RESUBMIT_UNKNOWN:
        # 未发布、重发仍不确定：查证 → 重发 → 再查 → 收敛，不得无限重试
        plan[1] = FaultSpec(STATUS_UNKNOWN, reason, published=False)
        plan[2] = FaultSpec(STATUS_UNKNOWN, second_reason, published=False)
        plan[3] = FaultSpec(STATUS_UNKNOWN, reason, published=False)
        expected_posts = 0
        max_publish = 2
    else:
        raise ValueError(f"未知场景族 {family}")

    return Scenario(
        scenario_id=f"{split}-{family}-{index + 1:03d}",
        family=family,
        split=split,
        task=task,
        kw=kw,
        title=_make_title(str(cand["content"])),
        candidate=str(cand["content"]),
        candidate_id=str(cand["id"]),
        candidates_pool=list(pool),
        delay_k=delay_k,
        fault_plan=plan,
        seed=rng.randrange(1, 2**31 - 1),
        expected_post_count=expected_posts,
        max_publish_calls=max_publish,
    )


def generate_scenarios(
    split: str = "train",
    *,
    per_family: int | None = None,
    seed: int = 0,
    families: Iterable[str] | None = None,
    candidates_path: str | Path | None = None,
) -> list[Scenario]:
    """生成一个 split 的场景清单。

    返回顺序经过洗牌：train / test 的洗牌种子不同，保证"故障出现顺序"也不同。
    """
    if split not in SPLIT_CONFIG:
        raise ValueError(f"未知 split {split!r}，只支持 {tuple(SPLIT_CONFIG)}")
    cfg = SPLIT_CONFIG[split]
    n = int(per_family or cfg["per_family"])
    pool_data = candidates_for_split(split, candidates_path)
    pool = [str(c["content"]) for c in pool_data]
    chosen_families = list(families or FAMILIES)

    rng = random.Random(seed * 1009 + (0 if split == "train" else 77))
    scenarios: list[Scenario] = []
    for family in chosen_families:
        for index in range(n):
            cand = pool_data[index % len(pool_data)]
            scenarios.append(
                _build_family(
                    family, split=split, index=index, cfg=cfg, cand=cand, pool=pool, rng=rng
                )
            )
    rng.shuffle(scenarios)
    return scenarios


def generate_pair(seed: int = 0, per_family: int | None = None) -> tuple[list[Scenario], list[Scenario]]:
    """一次性拿到 train / test 两个隔离集。"""
    return (
        generate_scenarios("train", per_family=per_family, seed=seed),
        generate_scenarios("test", per_family=per_family, seed=seed),
    )
