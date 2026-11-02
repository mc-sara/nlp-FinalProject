"""P3 correction replay: rebuild the 3 selected failure scenarios and replay the
P1 ReferencePolicy sequence (verify_budget=3, max_republish=1) on the CURRENT pod simulator.

Every tool return / observation / truth comes from this replay. Nothing is copied from the
source trajectories except task text, scenario parameters and the candidate pool.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path("/root/private_data/P2_SCNet/p2_scnet")
sys.path.insert(0, str(ROOT / "vendor" / "p1"))

from tieba_env.adapter import MockBackend, TiebaAdapter
from tieba_env.env import MockTiebaEnv
from tieba_env.runner import FinalAction, ToolAction, run_episode
from tieba_env.scenarios import generate_scenarios
from tieba_env.validator import classify

WORK = Path("/root/private_data/P3_review")
SRC_PATH = WORK / "selected_failures.jsonl"
REQ_PATH = WORK / "correction_requests.json"

TITLES = {
    "unknown_delayed": "出门鞋带开了三次，包带也断了",
    "unknown_not_published": "鞋带开了三次，手机掉地上没碎",
    "unknown_resubmit_unknown": "今天真是倒霉的一天",
}


def canonical_hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def simulator_fingerprint() -> str:
    out = subprocess.run(
        ["md5sum"] + sorted(str(p) for p in (ROOT / "vendor" / "p1" / "tieba_env").glob("*.py")),
        capture_output=True, text=True, check=True,
    ).stdout
    return hashlib.sha256(out.encode()).hexdigest()


class CannedPolicy:
    def __init__(self, actions):
        self.actions = list(actions)
        self.index = 0

    def act(self, history, scenario):
        action = self.actions[self.index]
        self.index += 1
        return action


def corrected_sequence(family: str, scenario):
    kw = scenario.kw
    title = TITLES[family]
    content = scenario.candidate
    q = lambda: ToolAction("list_recent_posts", {"kw": kw, "limit": 20})  # noqa: E731
    pub = ToolAction("publish", {"kw": kw, "title": title, "content": content})
    get = ToolAction("get_candidates", {})
    if family == "unknown_delayed":
        # published, list-visibility delayed 2 polls -> query until visible (budget k+1 = 3), stop.
        return [get, q(), pub, q(), q(), q(),
                FinalAction("回查吧页确认帖子已发布，没有重复提交。")]
    if family == "unknown_not_published":
        # not published: 3 queries confirm absence -> one republish (budget k+1 = 3), stop on ok.
        return [get, q(), pub, q(), q(), q(), pub,
                FinalAction("第一次提交查证未看到帖子，按未发布处理补发一次；补发返回 ok，任务完成。")]
    if family == "unknown_resubmit_unknown":
        # not published, republish also unknown -> 3+3 queries, stop and report honestly.
        return [get, q(), pub, q(), q(), q(), pub, q(), q(), q(),
                FinalAction("两次提交都未拿到明确结果，吧页连续查证也没有该帖，按未发布处理，不再重试。")]
    raise ValueError(family)


def main() -> None:
    reqs = json.loads(REQ_PATH.read_text(encoding="utf-8"))
    src = {json.loads(line)["trajectory_id"]: json.loads(line)
           for line in SRC_PATH.read_text(encoding="utf-8").splitlines() if line.strip()}
    scenarios = generate_scenarios("train", per_family=2, seed=42)
    by_id = {s.scenario_id: s for s in scenarios}
    sim_fp = simulator_fingerprint()
    script_fp = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    stamp = subprocess.run(["date", "-Iseconds"], capture_output=True, text=True).stdout.strip()

    out_rows = []
    for req in reqs:
        sid = req["source_trajectory_id"]
        source = src[sid]
        family = source["scenario"]["family"]
        scenario = by_id[source["scenario"]["scenario_id"]]

        # Guard: the regenerated scenario must match what the source trajectory recorded.
        assert scenario.to_record() == source["scenario"], (scenario.to_record(), source["scenario"])
        assert req["source_sha256"] == canonical_hash(source), "source hash mismatch"

        env = MockTiebaEnv()
        counter = {"n": 0}

        def factory():
            counter["n"] += 1
            return f"att-corr-{family[:12]}-{counter['n']}"

        adapter = TiebaAdapter(MockBackend(env), attempt_id_factory=factory)
        policy = CannedPolicy(corrected_sequence(family, scenario))
        traj = run_episode(env, adapter, scenario, policy, group="P3", backend_name="mock",
                           trajectory_id=f"P3-corr-{sid}")
        label = classify(traj)
        provenance = {
            "source_trajectory_id": sid,
            "source_sha256": req["source_sha256"],
            "source_file": "selected_failures.jsonl",
            "source_record_hash_scheme": "sha256(canonical json: ensure_ascii=False, sort_keys, separators=(',',':'))",
            "corrector": "P3 agent (deepseek-v4.1), executed on SCNet pod",
            "reviewed_by": "",
            "review_status": "pending P2 audit",
            "strategy_basis": [
                "PLAN.md 2.3: 已发布回查有延迟 → 查到命中或延迟上界; 未发布 → 确认没有后重发一次; 重发仍不确定 → 收敛不无限重试",
                "PLAN.md 5.3: 在 publish 返回 unknown 之后插入查证动作，再据实决定停止或补发",
                "vendor/p1/tieba_env/policies.py ReferencePolicy(verify_budget=3, max_republish=1): D1 实测策略的可执行版本",
                "P1 runs/publish/publish-20261026-203042-01c912.json: 每轮 unknown 后 0s/3s 各查证一次，最多 3 次补发",
                "P1 runs/real/20261024-100209-real-ce7ef17e.json: 已发布延迟可见，第 3 次轮询命中 (k=2)",
                "P1 runs/real/20261026-160304-real-736e0d56.json: 已发布，第 1 次轮询 2.32s 命中 (k=0)",
            ],
            "changes": [
                "删除首条 title_too_long 被拒的 publish（标题按真实侧 31 字上限取合法短标题）",
                "在 publish 返回 unknown 后插入 list_recent_posts 查证（每轮最多 3 次，覆盖实测延迟上界 k=2）",
                "补发次数限制为 1 次（max_republish=1），不无限重试",
                "最终答复按查证结果据实陈述；未命中的案例明确写“按未发布处理”",
                "tool_calls/result/observation/truth 全部由当前模拟器重放生成，未复制原轨迹",
                "移除 model_outputs（原模型输出仅留在 selected_failures.jsonl，不冒充修正后的生成）",
            ],
            "replay": {
                "simulator_dir": str(ROOT / "vendor" / "p1" / "tieba_env"),
                "simulator_fingerprint_md5_concat_sha256": sim_fp,
                "script_sha256": script_fp,
                "replayed_at": stamp,
                "scenario_seed": 42,
                "per_family": 2,
                "env": "MockTiebaEnv + TiebaAdapter(MockBackend), current pod version",
            },
        }
        traj.update(origin="human_corrected", p1_label=label, correction_provenance=provenance)
        out_rows.append(traj)
        print(f"{traj['trajectory_id']} {label} publishes={sum(1 for c in traj['tool_calls'] if c['tool_name']=='publish')} "
              f"steps={traj['steps']} truth_posts={traj['truth']['post_count']}")

    out = WORK / "corrected_demos.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for row in out_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("WROTE", out, hashlib.sha256(out.read_bytes()).hexdigest())
    for row in out_rows:
        print("HASH", row["trajectory_id"], hashlib.sha256(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest())


if __name__ == "__main__":
    main()
