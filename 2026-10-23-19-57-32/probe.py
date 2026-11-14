"""探针脚本：判断模型是"读观察再决定"，还是"按背下来的固定节拍复读"。

为什么要单独一个脚本（而不是看 test 集分数）：
  test 集和训练集同分布（同 5 族、同 k=1、同故障模板），把题背下来的模型照样满分。
  所以"学会没学会"和"是不是在读观察"必须分开测：
    test20   分布内冒烟，只回答"学会没学会"（loss / 格式 / 有没有出现 list）
    openloop 反事实探针：把 list 的返回换成恒空，动作该不该变
    kscan    延迟扫描：delayed 族 k=0..3，list 次数该 ≈ k+1 而不是恒定

三个模式共用同一套"跑一条轨迹并记录动作"的代码，不碰 env / scenarios / policies。
集群上放在 p2.py 同目录（复用它的 QwenPolicy），训练完/训练前都能跑。

用法：
  python probe.py --mode test20   --model .../Qwen3.5-9B --adapter .../adapters/C38
  python probe.py --mode openloop --model ... --adapter ... --per-family 6
  python probe.py --mode kscan    --model ... --adapter ... --per-family 8
  # 没有 GPU 时先验证管道（用脚本专家策略，不需要模型）：
  python probe.py --mode test20 --policy reference --per-family 2
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor' / 'p1'))
sys.path.insert(0, str(ROOT))

from tieba_env.adapter import MockBackend, TiebaAdapter          # noqa: E402
from tieba_env.env import MockTiebaEnv                           # noqa: E402
from tieba_env.runner import run_episode                         # noqa: E402
from tieba_env.scenarios import (                                # noqa: E402
    FAMILIES,
    FAMILY_UNKNOWN_DELAYED,
    generate_scenarios,
)

EMPTY_POSTS = {'posts': []}


class EmptyListBackend:
    """把所有 list_recent_posts 的返回换成恒空——反事实探针用。

    注意：这是"合法语义的空列表"（吧页上确实什么都没有），不是乱码。
    乱码属于工具失败鲁棒性，会走 tool_error 分支，不能拿来判"读不读观察"。
    """

    def __init__(self, inner: MockBackend) -> None:
        self.inner = inner

    def get_candidates(self) -> dict[str, Any]:
        return self.inner.get_candidates()

    def list_recent_posts(self, kw: str | None, limit: int | None) -> dict[str, Any]:
        return dict(EMPTY_POSTS)

    def publish(self, kw: str, title: str, content: str) -> dict[str, Any]:
        return self.inner.publish(kw, title, content)


class FixedSequencePolicy:
    """对照组（复读机）：完全不看观察，永远按 get → list → publish → 收尾 输出。

    它的存在是为了校准探针本身——如果探针对它也报“读观察”，那说明判据写错了。
    """

    def act(self, history, scenario):
        from tieba_env.runner import FinalAction, ToolAction
        n = len(history)
        if n == 0:
            return ToolAction('get_candidates', {})
        if n == 1:
            return ToolAction('list_recent_posts', {'kw': scenario.kw, 'limit': 20})
        if n == 2:
            return ToolAction('publish', {'kw': scenario.kw,
                                          'title': scenario.title,
                                          'content': scenario.candidate})
        return FinalAction(f'已在「{scenario.kw}」吧发布。')


def build_policy(args):
    if args.policy == 'reference':
        from tieba_env.policies import ReferencePolicy
        return ReferencePolicy(verify_budget=3, max_republish=1)
    if args.policy == 'replay':
        return FixedSequencePolicy()
    from p2 import QwenPolicy  # 集群工程根，和本脚本同目录
    return QwenPolicy(args.model, args.adapter, args.context, args.max_new)


def first_unknown_index(calls: list[dict[str, Any]]) -> int | None:
    for i, call in enumerate(calls):
        if (call.get('result') or {}).get('status') == 'unknown':
            return i
    return None


def describe(traj: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    calls = traj.get('tool_calls') or []
    idx = first_unknown_index(calls)
    after = None
    if idx is not None and idx + 1 < len(calls):
        after = calls[idx + 1].get('tool_name')
    row = dict(
        scenario_id=traj['scenario']['scenario_id'],
        family=traj['scenario']['family'],
        k=traj['scenario']['delay_k'],
        steps=len(calls),
        actions=[c.get('tool_name') for c in calls],
        action_after_unknown=after,
        publishes=sum(1 for c in calls if c.get('tool_name') == 'publish'),
        lists=sum(1 for c in calls if c.get('tool_name') == 'list_recent_posts'),
        post_count=(traj.get('truth') or {}).get('post_count'),
        final_answer=traj.get('final_answer'),
        model_errors=traj.get('model_errors') or [],
        hit_step_limit=bool(traj.get('hit_step_limit')),
    )
    if extra:
        row.update(extra)
    return row


def run_one(policy, scenario, *, empty_list: bool = False) -> dict[str, Any]:
    env = MockTiebaEnv()
    backend = MockBackend(env)
    if empty_list:
        backend = EmptyListBackend(backend)
    adapter = TiebaAdapter(backend, attempt_id_factory=lambda: 'att-probe')
    return run_episode(env, adapter, scenario, policy,
                       group='probe', trajectory_id=f"probe-{scenario.scenario_id}")


def scenarios_for(args, families=None) -> list:
    if isinstance(families, str):
        families = [families]
    return generate_scenarios(args.split, per_family=args.per_family, seed=args.seed,
                              families=list(families) if families else None)


def _row_key(row: dict[str, Any]) -> str:
    """续跑用的唯一键：test20/openloop 用场景 id，kscan 再加 k。"""
    return f"{row['scenario_id']}|k={row['probe_k']}" if 'probe_k' in row else row['scenario_id']


def load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            done.add(_row_key(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return done


def mode_test20(args, policy, sink=None, done=frozenset()) -> list[dict[str, Any]]:
    rows = []
    for scenario in scenarios_for(args):
        if scenario.scenario_id in done:
            continue
        row = describe(run_one(policy, scenario))
        rows.append(row)
        if sink:
            sink(row)
    return rows


def mode_openloop(args, policy, sink=None, done=frozenset()) -> list[dict[str, Any]]:
    rows = []
    # 只有"帖子真值会出现在吧页"的两族才有反事实可言：
    # visible 立刻可见、delayed k 次后可见；而 not_published/resubmit 的真值列表本来就是空的，
    # 换成恒空与真实完全等价，放进来只会把"读者"误判成"复读机"。
    observable = ('unknown_visible', 'unknown_delayed')
    for scenario in scenarios_for(args, observable):
        if scenario.scenario_id in done:
            continue
        real = describe(run_one(policy, scenario))
        empty = describe(run_one(policy, scenario, empty_list=True))
        row = dict(scenario_id=real['scenario_id'], family=real['family'],
                   real=real, empty=empty, identical=real['actions'] == empty['actions'])
        rows.append(row)
        if sink:
            sink(row)
    return rows


def with_delay(scenario, k: int):
    plan = dict(scenario.fault_plan)
    if 1 in plan:
        plan[1] = dataclasses.replace(plan[1], visible_after=k)
    return dataclasses.replace(scenario, delay_k=k, fault_plan=plan)


def mode_kscan(args, policy, sink=None, done=frozenset()) -> list[dict[str, Any]]:
    rows = []
    base = scenarios_for(args, FAMILY_UNKNOWN_DELAYED)
    for k in args.k_values:
        for scenario in base:
            if f'{scenario.scenario_id}|k={k}' in done:
                continue
            traj = run_one(policy, with_delay(scenario, k))
            row = describe(traj, extra=dict(probe_k=k))
            rows.append(row)
            if sink:
                sink(row)
    return rows


def summarize(args, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if args.mode == 'openloop':
        identical = sum(1 for r in rows if r['identical'])
        return {
            'mode': args.mode, 'cases': len(rows), 'identical_pairs': identical,
            'changed_pairs': len(rows) - identical,
            'verdict': ('动作随观察变化，倾向读观察' if identical * 2 < len(rows)
                        else '动作与观察无关，倾向按固定节拍复读'),
        }
    if args.mode == 'kscan':
        by_k: dict[int, Any] = {}
        for r in rows:
            by_k.setdefault(r['probe_k'], []).append(r)
        table = {}
        for k, group in sorted(by_k.items()):
            lists = [g['lists'] for g in group]
            table[k] = dict(n=len(group), mean_lists=round(sum(lists) / len(lists), 2),
                            mean_steps=round(sum(g['steps'] for g in group) / len(group), 2))
        means = [table[k]['mean_lists'] for k in sorted(table)]
        grew = bool(means) and (max(means) - means[0]) >= 1.5
        saturated = bool(means) and means[-1] == means[-2]
        return {'mode': args.mode, 'by_k': table,
                'verdict': ('list 次数随 k 增加（k 大时饱和于预算上限），倾向"查到即停"' if grew and saturated
                            else 'list 次数随 k 增加，倾向"查到即停"' if grew
                            else 'list 次数不随 k 变化，倾向复读')}
    after = [r['action_after_unknown'] for r in rows if r['action_after_unknown']]
    return {
        'mode': args.mode, 'cases': len(rows),
        'after_unknown_list': sum(1 for a in after if a == 'list_recent_posts'),
        'after_unknown_other': sum(1 for a in after if a != 'list_recent_posts'),
        'mean_steps': round(sum(r['steps'] for r in rows) / len(rows), 2),
        'step_limit_hits': sum(1 for r in rows if r['hit_step_limit']),
        'parse_errors': sum(1 for r in rows for e in r['model_errors']),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='读观察 vs 复读：三个探针')
    parser.add_argument('--mode', required=True, choices=('test20', 'openloop', 'kscan'))
    parser.add_argument('--policy', default='qwen', choices=('qwen', 'reference', 'replay'))
    parser.add_argument('--model', default='/public/home/scnsjwf2qh/models/Qwen3.5-9B')
    parser.add_argument('--adapter', default=None)
    parser.add_argument('--split', default='test', choices=('train', 'test'))
    parser.add_argument('--per-family', type=int, default=4,
                        help='每族条数：test20 默认 4（共 20 条）')
    parser.add_argument('--seed', type=int, default=1042)
    parser.add_argument('--context', type=int, default=8192)
    parser.add_argument('--max-new', type=int, default=512)
    parser.add_argument('--k-values', type=int, nargs='+', default=[0, 1, 2, 3])
    parser.add_argument('--out', default=None, help='结果 JSONL；默认 artifacts/runs/probe_<mode>.jsonl')
    args = parser.parse_args()

    policy = build_policy(args)
    runner = {'test20': mode_test20, 'openloop': mode_openloop, 'kscan': mode_kscan}[args.mode]

    out = Path(args.out or f'artifacts/runs/probe_{args.mode}.jsonl')
    out.parent.mkdir(parents=True, exist_ok=True)
    done = load_done(out)
    if done:
        print(f'[resume] 已完成 {len(done)} 条，续跑剩余', flush=True)
    with out.open('a', encoding='utf-8') as fh:
        def sink(row: dict[str, Any]) -> None:
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')
            fh.flush()
            print(f"  done {_row_key(row)}", flush=True)
        runner(args, policy, sink, done)

    rows = [json.loads(line) for line in out.read_text(encoding='utf-8').splitlines() if line.strip()]
    summary = summarize(args, rows)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f'→ {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
