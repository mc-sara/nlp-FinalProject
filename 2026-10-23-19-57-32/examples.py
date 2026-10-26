"""五种调用方式，按"从底层到上层"排列。

    python examples.py minimal   # 1. 只用模拟器，手工喂动作
    python examples.py adapter   # 2. 走适配层（推荐入口，保证观测与真实后端一致）
    python examples.py episode   # 3. 跑一条完整轨迹并分类
    python examples.py batch     # 4. 批量跑一个 split 并汇总指标
    python examples.py real      # 5. 接真实后端（需 D1 对齐 CLI 后才能跑通）
    python examples.py all       # 全部
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tieba_env import (
    FAMILY_UNKNOWN_DELAYED,
    MockBackend,
    MockTiebaEnv,
    RealBackend,
    TiebaAdapter,
    classify,
    generate_scenarios,
    summarize,
)
from tieba_env.env import delay_k_from_measurement

ROOT = Path(__file__).resolve().parent
from tieba_env.policies import ReferencePolicy
from tieba_env.runner import run_episode, trajectory_to_sharegpt
from tieba_env.types import STATUS_UNKNOWN, FaultSpec


def example_minimal() -> None:
    """1. 模拟器本体：自己调三个方法，自己看真值。"""
    env = MockTiebaEnv(candidates=["热梗1", "热梗2"], delay_k=2)
    env.reset(
        fault_plan={1: FaultSpec(STATUS_UNKNOWN, "submit_timeout", published=True, visible_after=2)}
    )

    print("候选:", env.get_candidates())
    result = env.publish("测试", "分享", "热梗1")
    print("工具返回:", result.observation)
    print("环境真值 → 已发布:", result.published, "| post_id:", result.post_id, "（模型看不到）")

    for i in range(1, 4):
        posts = env.list_recent_posts("测试")["posts"]
        print(f"  第 {i} 次查吧页: {posts}")
    print("帖子库真值:", env.truth()["post_count"], "篇 | 实际延迟:", env.visibility_report()[0]["observed_delay"], "次")
    print("D1 换算：实测 5 秒可见、每 2 秒轮询一次 → k =", delay_k_from_measurement(5.0, 2.0))


def example_adapter() -> None:
    """2. 适配层：模型只认字符串，attempt_id 只在本地日志里。"""
    env = MockTiebaEnv(candidates=["热梗1", "热梗2"])
    env.reset()
    adapter = TiebaAdapter(MockBackend(env))

    for name, args in [
        ("get_candidates", {}),
        ("list_recent_posts", {"kw": "测试", "limit": 20}),
        ("publish", {"kw": "测试", "title": "分享", "content": "热梗1"}),
    ]:
        result, observation = adapter.call(name, args)
        print(f"{name:20s} -> {observation}")

    print("本地日志里的 attempt_id:", [item["attempt_id"] for item in adapter.local_log])
    print("注意 publish 的 ok 才有 post_id，unknown 只有 reason")


def example_episode() -> None:
    """3. 一条完整轨迹：场景 → 策略 → 轨迹 → 分类。"""
    scenario = generate_scenarios(
        "test", families=[FAMILY_UNKNOWN_DELAYED], per_family=1, seed=3
    )[0]
    print("任务:", scenario.task)
    print("故障计划:", scenario.to_record()["faults"])

    env = MockTiebaEnv()
    adapter = TiebaAdapter(MockBackend(env))
    policy = ReferencePolicy(verify_budget=scenario.delay_k + 1)
    traj = run_episode(env, adapter, scenario, policy, group="reference")

    for call in traj["tool_calls"]:
        print(f"  {call['step']}. {call['tool_name']:20s} {call['observation']}")
    print("最终回答:", traj["final_answer"])
    print("分类:", classify(traj))
    print("真值:", traj["truth"]["post_count"], "篇，发布", traj["truth"]["publish_count"], "次")

    print("\n转 ShareGPT 后的轮次:", [m["from"] for m in trajectory_to_sharegpt(traj)["conversations"]])


def example_batch() -> None:
    """4. 批量：一个 split 全部跑完，出指标。"""
    scenarios = generate_scenarios("test", per_family=3, seed=0)
    budget = max(s.delay_k for s in scenarios) + 1
    trajectories = []
    for scenario in scenarios:
        env = MockTiebaEnv()
        adapter = TiebaAdapter(MockBackend(env))
        trajectories.append(
            run_episode(env, adapter, scenario, ReferencePolicy(verify_budget=budget))
        )

    result = summarize(trajectories)
    overall = result["overall"]
    print("可评轨迹:", overall["n_evaluable"], "| 剔除的工具失败:", overall["n_failed_tool"])
    for key in (
        "duplicate_post_rate",
        "first_action_verify_rate",
        "recovery_success_rate",
        "normal_success_rate",
        "over_query_mean_normal",
        "mean_tool_calls",
    ):
        print(f"  {key:26s} {overall[key]}")
    print("按场景族:", list(result["by_family"]))


def example_real() -> None:
    """5. 真实后端：与模拟后端共用同一个 adapter，观测必须逐字节一致。"""
    from tieba_env.realcli import RealCliConfig, build_transport

    cfg = RealCliConfig.load(ROOT / "real" / "config.json")
    transport = build_transport(cfg)
    adapter = TiebaAdapter(RealBackend(transport))
    try:
        observation = adapter.call("list_recent_posts", {"kw": cfg.kw, "limit": 5})[1]
        print("真实返回:", observation[:160], "...")
        print("提示：完整流程用 `python real_run.py health` / `verify` / `publish`")
    except Exception as exc:  # noqa: BLE001 - 示例里直接把原因打出来
        print("真实后端未接通:", type(exc).__name__)
        print("  ", str(exc)[:200])
        print("  检查 real/config.json 里的 poster_dir / python / probe_script")


def main() -> int:
    table = {
        "minimal": example_minimal,
        "adapter": example_adapter,
        "episode": example_episode,
        "batch": example_batch,
        "real": example_real,
    }
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    if target == "all":
        for name, fn in table.items():
            print(f"\n{'=' * 20} {name} {'=' * 20}")
            fn()
    elif target in table:
        table[target]()
    else:
        print("可选:", ", ".join(table), "或 all")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
