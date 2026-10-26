"""同构性检查（PLAN 7.2）：同一条动作在真实后端与模拟后端上，模型可见的观测必须一致。

真实侧没有 JSON 输出，所以流程是：
    真实 stdout → parse_poster_output → 四状态 → 同一个 TiebaAdapter → 观测字符串
                                    模拟 env → 四状态 → 同一个 TiebaAdapter → 观测字符串

比较前把 post_id 掩成 <PID>（真实 tid 与 mock 序号本来就不会相同），
但 post_id 的"形状"（str / null）必须一致 —— 这正是 unknown 不能带 ID 的那条契约。

    python check_isomorphy.py
    python check_isomorphy.py --capture real/captures/d1.jsonl

D1 采到真实原始输出后，按 format_fixtures.jsonl 的格式追加行即可；
把 stdout 换成真实抓到的原文，其余字段照抄。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from tieba_env.adapter import MockBackend, RealBackend, TiebaAdapter
from tieba_env.env import MockTiebaEnv
from tieba_env.realcli import parse_poster_output
from tieba_env.types import STATUS_OK, FaultSpec

ROOT = Path(__file__).resolve().parent
DEFAULT_CAPTURE = ROOT / "real" / "captures" / "format_fixtures.jsonl"

_POST_ID_RE = re.compile(r'"post_id":"[^"]*"')


def mask(observation: str) -> str:
    return _POST_ID_RE.sub('"post_id":"<PID>"', observation)


@dataclass
class StubBackend:
    """把已解析好的四状态原样交回，用来隔离"解析"与"归一"两件事。"""

    payload: dict[str, Any]
    posts: list[dict[str, Any]] = field(default_factory=list)

    def get_candidates(self) -> dict[str, Any]:
        return {"candidates": ["热梗1", "热梗2"]}

    def list_recent_posts(self, kw: Any, limit: Any) -> dict[str, Any]:
        return {"posts": self.posts}

    def publish(self, kw: str, title: str, content: str) -> dict[str, Any]:
        return self.payload


def _fault(spec: dict[str, Any] | None) -> dict[int, FaultSpec]:
    if not spec:
        return {}
    known = set(FaultSpec.__dataclass_fields__)
    payload = {k: v for k, v in spec.items() if k in known and k != "published"}
    payload.setdefault("published", spec.get("status") == STATUS_OK)
    return {1: FaultSpec(**payload)}


def build_observation(side: str, case: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """返回 (观测字符串, 归一后的 result)。"""
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"att-{counter['n']}"

    if side == "real":
        if case["tool"] == "publish":
            parsed, _ = parse_poster_output(case.get("stdout", ""), case.get("returncode", 0))
            backend: Any = StubBackend({k: v for k, v in parsed.items() if not k.startswith("_")})
        else:
            backend = StubBackend({}, posts=case.get("expected", {}).get("posts", []))
    else:
        env = MockTiebaEnv(candidates=["热梗1", "热梗2"])
        env.reset(fault_plan=_fault((case.get("mock") or {}).get("fault")))
        backend = MockBackend(env)

    adapter = TiebaAdapter(backend, attempt_id_factory=factory)
    result, observation = adapter.call(case["tool"], case.get("arguments") or {})
    return observation, result


def check_case(case: dict[str, Any]) -> tuple[bool, str]:
    real_obs, real_result = build_observation("real", case)
    expected = case.get("expected") or {}

    for key in ("status", "reason"):
        if key in expected and real_result.get(key) != expected[key]:
            return False, f"解析结果不符：{key} 期望 {expected[key]!r}，得到 {real_result.get(key)!r}"

    if "post_id_shape" in expected:
        shape = "null" if real_result.get("post_id") is None else "str"
        if shape != expected["post_id_shape"]:
            return False, f"post_id 形状不符：期望 {expected['post_id_shape']}，得到 {shape}"

    if expected.get("posts") is not None and real_result.get("posts") != expected["posts"]:
        return False, f"列表不符：{real_result.get('posts')!r}"

    if case["tool"] != "publish":
        return True, "两侧观测一致（只读工具）"

    mock_obs, _ = build_observation("mock", case)
    if mask(real_obs) != mask(mock_obs):
        return False, f"观测不一致\n      真实: {mask(real_obs)}\n      模拟: {mask(mock_obs)}"
    return True, f"两侧观测一致　{mask(real_obs)}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="适配层同构性检查")
    ap.add_argument("--capture", default=str(DEFAULT_CAPTURE))
    args = ap.parse_args(argv)

    path = Path(args.capture)
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"用例来源：{path}（{len(cases)} 条）\n")

    failed = 0
    for case in cases:
        ok, message = check_case(case)
        flag = "一致" if ok else "不一致"
        print(f"[{flag}] {case['case']}")
        print(f"        {message}")
        if not ok:
            failed += 1

    print(f"\n合计 {len(cases)} 条，{len(cases) - failed} 条一致，{failed} 条不一致")
    if failed:
        print("不一致说明真实返回的字段形状与 mock 契约有偏差，先修 realcli.parse_poster_output，")
        print("不要在 adapter 里加分支 —— adapter 是两侧共用的同构基准。")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
