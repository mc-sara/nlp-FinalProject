"""跑一遍全流程，验证模拟器能区分好坏行为。

    python demo.py                 # 默认：test 集，每族 5 条，三组策略对照
    python demo.py --split train --per-family 10

输出：控制台指标表 + runs/demo_metrics.json + 一条样例轨迹与 ShareGPT 样例。
这里的策略是脚本策略，不是模型；目的只是证明"重复帖""首动作不合规""过度查证"
这些差异能被环境稳定地观测到并分类出来。
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from pathlib import Path
from typing import Any, Sequence

from tieba_env.adapter import MockBackend, TiebaAdapter
from tieba_env.env import MockTiebaEnv
from tieba_env.policies import QueryOncePolicy, ReferencePolicy, ResubmitImmediatelyPolicy
from tieba_env.runner import run_episode, trajectory_to_sharegpt
from tieba_env.scenarios import FAMILIES, generate_scenarios
from tieba_env.validator import classify, summarize


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def run_group(name: str, scenarios: Sequence, policy_factory, verify_budget: int) -> list[dict[str, Any]]:
    trajectories = []
    for scenario in scenarios:
        env = MockTiebaEnv()
        adapter = TiebaAdapter(MockBackend(env))
        policy = policy_factory(verify_budget, scenario)
        trajectories.append(run_episode(env, adapter, scenario, policy, group=name))
    return trajectories


def print_table(rows: list[tuple[str, str, dict[str, Any]]]) -> None:
    header = ("策略", "场景族", "n", "重复帖率", "首动作查证率", "恢复成功率", "正常成功率", "平均调用")
    widths = [26, 28, 4, 10, 14, 12, 12, 10]
    line = "  ".join(pad(h, w) for h, w in zip(header, widths))
    print(line)
    print("-" * display_width(line))
    for group, family, metrics in rows:
        cells = (
            group,
            family,
            str(metrics["n_evaluable"]),
            f"{metrics['duplicate_post_rate']:.2f}",
            f"{metrics['first_action_verify_rate']:.2f}",
            f"{metrics['recovery_success_rate']:.2f}",
            f"{metrics['normal_success_rate']:.2f}",
            f"{metrics['mean_tool_calls']:.1f}",
        )
        print("  ".join(pad(c, w) for c, w in zip(cells, widths)))


BUILD_SEEDS = {"train": 20261109, "test": 777001}


def build(per_family: int, out_dir: Path, *, families: list[str] | None = None,
          splits: tuple[str, ...] = ("train", "test"), rotate_pool: bool = False,
          shuffle_pool: bool = False) -> int:
    """用参考策略批量跑轨迹并转 ShareGPT；这是训练数据的唯一生成入口。

    families 只生成指定族；splits 只生成指定 split；rotate_pool 让每条轨迹看到的
    候选列表顺序不同（B 组 300 条 normal 去重用）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for split, seed in BUILD_SEEDS.items():
        if split not in splits:
            continue
        scenarios = generate_scenarios(split, per_family=per_family, seed=seed,
                                       families=families, rotate_pool=rotate_pool,
                                       shuffle_pool=shuffle_pool)
        trajs = []
        for scenario in scenarios:
            env = MockTiebaEnv()
            traj = run_episode(env, TiebaAdapter(MockBackend(env)), scenario,
                               ReferencePolicy(verify_budget=3, max_republish=1))
            traj["class"] = classify(traj)
            # 来源如实标注：这些是脚本专家（ReferencePolicy）的 rollout，不是模型自己跑的
            traj["origin"] = "reference_policy"
            trajs.append(traj)

        summary = summarize(trajs)
        lines = "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in trajs)
        (out_dir / f"{split}.jsonl").write_text(lines, encoding="utf-8")
        share = "".join(json.dumps(trajectory_to_sharegpt(t), ensure_ascii=False) + "\n" for t in trajs)
        (out_dir / f"{split}_sharegpt.jsonl").write_text(share, encoding="utf-8")
        (out_dir / f"{split}_metrics.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        counts = summary["overall"]["counts"]
        print(f"{split}: {len(trajs)} 条（正常 {counts['SUCCESS_NORMAL']} / 恢复 "
              f"{counts['SUCCESS_RECOVERED']} / 重复帖 {counts['DUPLICATE_POST']} / "
              f"模型失败 {counts['FAILED_MODEL']}）→ {out_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="贴吧发帖模拟器自检")
    parser.add_argument("--split", default="test", choices=("train", "test"))
    parser.add_argument("--per-family", type=int, default=5)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--build", action="store_true",
                        help="生成训练集：runs/sim/{train,test}.jsonl + *_sharegpt.jsonl")
    parser.add_argument("--out", default="runs/sim", help="--build 的输出目录")
    parser.add_argument("--families", default="",
                        help="--build 只生成指定族，逗号分隔（如 normal）")
    parser.add_argument("--splits", default="train,test",
                        help="--build 只生成指定 split，逗号分隔")
    parser.add_argument("--rotate-pool", action="store_true",
                        help="--build 时按 index 轮换候选列表顺序（循环位移只有 60 种）")
    parser.add_argument("--shuffle-pool", action="store_true",
                        help="--build 时按 index 随机排列候选列表（每条轨迹唯一）")
    args = parser.parse_args()

    if args.build:
        return build(args.per_family, Path(args.out),
                     families=[f for f in args.families.split(",") if f] or None,
                     splits=tuple(s for s in args.splits.split(",") if s),
                     rotate_pool=args.rotate_pool, shuffle_pool=args.shuffle_pool)

    scenarios = generate_scenarios(args.split, per_family=args.per_family, seed=args.seed)
    verify_budget = max([s.delay_k for s in scenarios] + [1]) + 1

    groups = [
        ("参考策略(查证到延迟上界)", lambda b, s: ReferencePolicy(verify_budget=b), verify_budget),
        ("只查一次(QA)", lambda b, s: QueryOncePolicy(), 1),
        ("不确定就重发", lambda b, s: ResubmitImmediatelyPolicy(), 1),
    ]

    all_trajs: list[dict[str, Any]] = []
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for name, factory, budget in groups:
        trajs = run_group(name, scenarios, factory, budget)
        all_trajs.extend(trajs)
        summary = summarize(trajs)
        for family in FAMILIES:
            rows.append((name, family, summary["by_family"][family]))

    print(f"\n场景集: {args.split}，{len(scenarios)} 条/组，回查延迟上界 k={verify_budget - 1}\n")
    print_table(rows)

    overall = summarize(all_trajs)
    print("\n总体：")
    for name, _, _ in groups:
        metrics = overall["by_group"][name]
        counts = metrics["counts"]
        print(
            f"  {name}: 可评 {metrics['n_evaluable']} 条 | "
            f"成功 {counts['SUCCESS_NORMAL'] + counts['SUCCESS_RECOVERED']} | "
            f"重复帖 {counts['DUPLICATE_POST']} | 模型失败 {counts['FAILED_MODEL']} | "
            f"工具失败(已剔除) {metrics['n_failed_tool']}"
        )

    out_dir = Path(__file__).resolve().parent / "runs"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "demo_metrics.json").write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    sample = next(t for t in all_trajs if t["scenario"]["family"] == "unknown_delayed")
    (out_dir / "sample_trajectory.json").write_text(
        json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "sample_sharegpt.json").write_text(
        json.dumps(trajectory_to_sharegpt(sample), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n样例轨迹分类: {classify(sample)}")
    print(f"产物目录: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
