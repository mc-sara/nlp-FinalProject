"""模拟器、适配层、验证器的单元测试（PLAN.md 7.1 / 7.2）。

运行：
    python -m unittest tieba_env.tests.test_simulator -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tieba_env.adapter import (
    BackendContractError,
    MockBackend,
    ModelToolCallError,
    RealBackend,
    TiebaAdapter,
    derive_status_from_evidence,
    dumps_observation,
)
from tieba_env.env import MockTiebaEnv, delay_k_from_measurement
from tieba_env.policies import (
    QueryOncePolicy,
    ReferencePolicy,
    ResubmitImmediatelyPolicy,
)
from tieba_env.runner import run_episode, trajectory_to_sharegpt
from tieba_env.scenarios import (
    FAMILY_NORMAL,
    FAMILY_UNKNOWN_DELAYED,
    FAMILY_UNKNOWN_NOT_PUBLISHED,
    FAMILY_UNKNOWN_RESUBMIT_UNKNOWN,
    FAMILY_UNKNOWN_VISIBLE,
    generate_scenarios,
    load_candidates,
)
from tieba_env.types import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    FaultRates,
    FaultSpec,
)
from tieba_env.validator import (
    CLASS_DUPLICATE_POST,
    CLASS_FAILED_MODEL,
    CLASS_FAILED_TOOL,
    CLASS_SUCCESS_NORMAL,
    CLASS_SUCCESS_RECOVERED,
    classify,
    episode_metrics,
    first_unknown_index,
)


def make_env(**kwargs) -> MockTiebaEnv:
    return MockTiebaEnv(candidates=["热梗1", "热梗2"], **kwargs)


def one_scenario(family: str, split: str = "train", seed: int = 0):
    return generate_scenarios(split, families=[family], per_family=1, seed=seed)[0]


def run(policy, scenario, *, env=None, adapter=None):
    env = env or MockTiebaEnv()
    adapter = adapter or TiebaAdapter(MockBackend(env))
    return run_episode(env, adapter, scenario, policy, group=type(policy).__name__)


# --------------------------------------------------------------------------- 7.1 模拟器


class TestFourTruthCombinations(unittest.TestCase):
    """四种真值组合：注入不确定状态后帖子库状态是否符合预期。"""

    def test_published_and_immediately_visible(self):
        env = make_env()
        env.reset(fault_plan={1: FaultSpec(STATUS_UNKNOWN, "submit_timeout", published=True, visible_after=0)})
        result = env.publish("测试", "分享", "热梗1")

        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.observation.get("post_id"), "unknown 不得带 post_id")
        self.assertTrue(result.published, "真值应为已发布")
        self.assertEqual(env.truth()["post_count"], 1)
        self.assertEqual(len(env.list_recent_posts("测试")["posts"]), 1, "应立刻可查")

    def test_published_with_recheck_delay(self):
        env = make_env()
        env.reset(fault_plan={1: FaultSpec(STATUS_UNKNOWN, "no_response", published=True, visible_after=2)})
        env.publish("测试", "分享", "热梗1")

        self.assertEqual(env.list_recent_posts("测试")["posts"], [], "第 1 次查不到")
        self.assertEqual(env.list_recent_posts("测试")["posts"], [], "第 2 次查不到")
        self.assertEqual(len(env.list_recent_posts("测试")["posts"]), 1, "第 3 次应命中")

        report = env.visibility_report()[0]
        self.assertEqual(report["published_at_query"], 0)
        self.assertEqual(report["first_visible_query"], 3)
        self.assertEqual(report["observed_delay"], 2, "观测延迟应等于 visible_after")

    def test_not_published(self):
        env = make_env()
        env.reset(fault_plan={1: FaultSpec(STATUS_UNKNOWN, "captcha", published=False)})
        env.publish("测试", "分享", "热梗1")

        self.assertEqual(env.truth()["post_count"], 0)
        self.assertEqual(env.visibility_report(), [])
        for _ in range(3):
            self.assertEqual(env.list_recent_posts("测试")["posts"], [])

    def test_resubmit_still_unknown_converges(self):
        env = make_env()
        env.reset(
            fault_plan={
                1: FaultSpec(STATUS_UNKNOWN, "submit_timeout", published=False),
                2: FaultSpec(STATUS_UNKNOWN, "no_response", published=False),
            }
        )
        env.publish("测试", "分享", "热梗1")
        env.publish("测试", "分享", "热梗1")

        truth = env.truth()
        self.assertEqual(truth["post_count"], 0)
        self.assertEqual(truth["publish_count"], 2)
        self.assertTrue(all(not r["published"] for r in truth["publish_attempts"]))


class TestFaultInjection(unittest.TestCase):

    def test_plan_targets_specific_call(self):
        env = make_env()
        env.reset(fault_plan={2: FaultSpec(STATUS_UNKNOWN, "submit_timeout", published=False)})
        first = env.publish("测试", "a", "c1")
        second = env.publish("测试", "b", "c2")
        third = env.publish("测试", "c", "c3")

        self.assertEqual(first.status, STATUS_OK)
        self.assertEqual(second.status, STATUS_UNKNOWN)
        self.assertEqual(third.status, STATUS_OK)
        self.assertEqual(env.truth()["post_count"], 2, "只有第 1、3 次真的写了帖子")

    def test_rule_rejection_does_not_consume_fault_plan(self):
        """硬规则挡下的发帖不占故障位：否则一个超长标题就能吃掉注入（P2 复盘发现）。"""
        env = make_env()
        env.reset(fault_plan={1: FaultSpec(STATUS_UNKNOWN, "submit_timeout", published=False)})

        bad = env.publish("测试", "标" * 55, "热梗1")
        self.assertEqual(bad.status, STATUS_REJECTED)
        self.assertEqual(bad.reason, "title_too_long")

        good = env.publish("测试", "分享", "热梗2")
        self.assertEqual(good.status, STATUS_UNKNOWN, "第 1 次有效发帖必须命中 1 号故障位")
        truth = env.truth()
        self.assertEqual(truth["publish_count"], 1)
        self.assertEqual(len(truth["publish_attempts"]), 2, "两次输入都留下审计记录")

    def test_probability_injection_is_reproducible(self):
        rates = FaultRates(error=0.05, rejected=0.1, unknown=0.5, delay_k=2)

        def run_once(seed):
            env = make_env(fault_rates=rates, seed=seed)
            env.reset()
            return [
                (r.status, r.reason, r.published)
                for r in (env.publish("测试", f"t{i}", f"c{i}") for i in range(30))
            ]

        self.assertEqual(run_once(7), run_once(7), "同一种子必须得到同一序列")

    def test_always_unknown_configuration(self):
        rates = FaultRates(
            unknown=1.0, p_published_when_unknown=1.0, p_delayed_when_published=1.0, delay_k=3
        )
        env = make_env(fault_rates=rates, seed=1)
        env.reset()
        results = [env.publish("测试", f"t{i}", f"c{i}") for i in range(5)]
        self.assertTrue(all(r.status == STATUS_UNKNOWN for r in results))
        self.assertTrue(all(r.published for r in results))
        self.assertTrue(all(env.posts[i].miss_budget == 3 for i in range(5)))

    def test_error_and_rejected_write_nothing(self):
        env = make_env()
        env.reset(
            fault_plan={
                1: FaultSpec(STATUS_ERROR, "login_expired"),
                2: FaultSpec(STATUS_REJECTED, "level_limit"),
            }
        )
        err = env.publish("测试", "分享", "热梗1")
        rej = env.publish("测试", "分享", "热梗1")
        self.assertEqual(err.status, STATUS_ERROR)
        self.assertEqual(rej.status, STATUS_REJECTED)
        self.assertEqual(env.truth()["post_count"], 0)


class TestSiteRules(unittest.TestCase):

    def test_title_too_long(self):
        env = make_env(title_limit=5)
        result = env.publish("测试", "标题超过五个字符了", "热梗1")
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.reason, "title_too_long")
        self.assertEqual(env.truth()["post_count"], 0)

    def test_level_limit(self):
        env = make_env(account_level=2, required_level=4)
        self.assertEqual(env.publish("测试", "分享", "热梗1").reason, "level_limit")

    def test_duplicates_allowed_by_default(self):
        """主指标是重复帖产生率；如果默认拦截重复，指标永远为 0，实验就没意义了。"""
        env = make_env()
        env.publish("测试", "分享", "热梗1")
        env.publish("测试", "分享", "热梗1")
        truth = env.truth()
        self.assertEqual(truth["post_count"], 2)
        self.assertEqual(truth["max_same_content"], 2)

    def test_duplicate_guard_when_enabled(self):
        env = make_env(duplicate_guard=True)
        env.publish("测试", "分享", "热梗1")
        second = env.publish("测试", "分享", "热梗1")
        self.assertEqual(second.status, STATUS_REJECTED)
        self.assertEqual(second.reason, "duplicate")
        self.assertEqual(env.truth()["post_count"], 1)


class TestResetAndIsolation(unittest.TestCase):

    def test_reset_clears_everything(self):
        env = make_env()
        env.publish("测试", "分享", "热梗1")
        env.list_recent_posts("测试")
        env.reset()

        truth = env.truth()
        self.assertEqual(truth["post_count"], 0)
        self.assertEqual(truth["publish_attempts"], [])
        self.assertEqual(truth["list_calls"], 0)
        self.assertEqual(env.list_recent_posts("测试")["posts"], [])

    def test_kw_isolation(self):
        env = make_env()
        env.publish("测试", "分享", "热梗1")
        self.assertEqual(env.list_recent_posts("别的吧")["posts"], [])

    def test_delay_k_from_measurement(self):
        # 实测 5 秒可见、轮询间隔 2 秒 → 第 3 次轮询命中 → k=2
        self.assertEqual(delay_k_from_measurement(5.0, 2.0), 2)
        self.assertEqual(delay_k_from_measurement(0.0, 2.0), 0)
        self.assertEqual(delay_k_from_measurement(2.0, 2.0), 0)
        self.assertEqual(delay_k_from_measurement(4.0, 2.0), 1)


# --------------------------------------------------------------------------- 适配层


class TestAdapterContract(unittest.TestCase):

    def test_ok_carries_post_id_and_attempt_id(self):
        env = make_env()
        env.reset()
        adapter = TiebaAdapter(MockBackend(env), attempt_id_factory=lambda: "att-fixed")
        result, observation = adapter.call("publish", {"kw": "测试", "title": "分享", "content": "热梗1"})

        self.assertEqual(result["status"], STATUS_OK)
        self.assertEqual(result["post_id"], "1")
        self.assertEqual(result["attempt_id"], "att-fixed")
        self.assertEqual(
            observation, '{"status":"ok","post_id":"1","attempt_id":"att-fixed"}'
        )

    def test_unknown_carries_reason_not_post_id(self):
        env = make_env()
        env.reset(fault_plan={1: FaultSpec(STATUS_UNKNOWN, "submit_timeout", published=True)})
        adapter = TiebaAdapter(MockBackend(env), attempt_id_factory=lambda: "att-u")
        result, obligation = adapter.call("publish", {"kw": "测试", "title": "分享", "content": "热梗1"})

        self.assertNotIn("post_id", result)
        self.assertEqual(result["reason"], "submit_timeout")
        self.assertEqual(result["attempt_id"], "att-u")
        self.assertEqual(obligation, '{"status":"unknown","reason":"submit_timeout","attempt_id":"att-u"}')

    def test_rejected_and_error_have_no_attempt_id(self):
        """rejected / error 明确知道没发出去，不带 attempt_id（PLAN 2.1 原始契约）。

        attempt_id 只在「可能已经发出去了」的 ok / unknown 上有对账价值。
        """
        env = make_env(title_limit=2)
        env.reset(fault_plan={1: FaultSpec(STATUS_ERROR, "selector_missing")})
        adapter = TiebaAdapter(MockBackend(env), attempt_id_factory=lambda: "att-fixed")
        rejected, _ = adapter.call("publish", {"kw": "测试", "title": "太长了", "content": "热梗1"})
        error, _ = adapter.call("publish", {"kw": "测试", "title": "a", "content": "热梗1"})
        self.assertEqual(rejected, {"status": "rejected", "reason": "title_too_long"})
        self.assertEqual(error, {"status": "error", "reason": "selector_missing"})

    def test_unknown_tool_raises_model_error(self):
        env = make_env()
        env.reset()
        adapter = TiebaAdapter(MockBackend(env))
        with self.assertRaises(ModelToolCallError):
            adapter.call("check_status", {"attempt_id": "att-1"})

    def test_missing_argument_raises_model_error(self):
        env = make_env()
        env.reset()
        adapter = TiebaAdapter(MockBackend(env))
        with self.assertRaises(ModelToolCallError):
            adapter.call("publish", {"kw": "测试", "title": "分享"})

    def test_attempt_id_only_in_local_log(self):
        env = make_env()
        env.reset()
        adapter = TiebaAdapter(MockBackend(env), attempt_id_factory=lambda: "att-xyz")
        adapter.call("publish", {"kw": "测试", "title": "分享", "content": "热梗1"})
        self.assertEqual(adapter.local_log[0]["attempt_id"], "att-xyz")
        # 帖子库侧记录的是客户端 ID，但服务端观测里它是随响应回显的，不是提交参数
        self.assertEqual(env.posts[0].attempt_id, None, "MockBackend 刻意不把 attempt_id 传给 env")

    def test_bad_backend_contract_raises(self):
        class Broken:
            def get_candidates(self):
                return {}

            def list_recent_posts(self, kw, limit):
                return {}

            def publish(self, kw, title, content):
                return {"status": "maybe"}

        adapter = TiebaAdapter(Broken())
        with self.assertRaises(BackendContractError):
            adapter.call("get_candidates", {})
        with self.assertRaises(BackendContractError):
            adapter.call("publish", {"kw": "a", "title": "b", "content": "c"})


class TestBackendIsomorphy(unittest.TestCase):
    """7.2 适配层同构性：同一条动作在 mock 与 real 后端上观测字符串必须逐字节一致。"""

    def _canned(self, tool: str, args: dict) -> dict:
        table = {
            "get_candidates": {"candidates": ["热梗1", "热梗2"]},
            "list_recent_posts": {"posts": []},
            "publish": {"status": STATUS_UNKNOWN, "reason": "submit_timeout"},
        }
        return json.loads(json.dumps(table[tool]))  # 故意做一次拷贝，模拟网络反序列化

    def _paired_adapters(self, env: MockTiebaEnv):
        def make_factory():
            counter = {"n": 0}

            def factory():
                counter["n"] += 1
                return f"att-{counter['n']}"

            return factory

        return (
            TiebaAdapter(MockBackend(env), attempt_id_factory=make_factory()),
            TiebaAdapter(RealBackend(self._canned), attempt_id_factory=make_factory()),
        )

    def test_observations_are_byte_identical(self):
        env = make_env()
        env.reset(fault_plan={1: FaultSpec(STATUS_UNKNOWN, "submit_timeout", published=False)})
        mock_adapter, real_adapter = self._paired_adapters(env)

        script = [
            ("get_candidates", {}),
            ("list_recent_posts", {"kw": "测试", "limit": 20}),
            ("publish", {"kw": "测试", "title": "分享", "content": "热梗1"}),
        ]
        for name, args in script:
            _, obs_mock = mock_adapter.call(name, args)
            _, obs_real = real_adapter.call(name, args)
            self.assertEqual(obs_mock, obs_real, f"{name} 的观测字符串不一致")

    def test_ok_observations_are_byte_identical(self):
        env = make_env()
        env.reset()
        adapter_mock = TiebaAdapter(MockBackend(env), attempt_id_factory=lambda: "att-1")
        adapter_real = TiebaAdapter(
            RealBackend(lambda tool, args: {"status": STATUS_OK, "post_id": "1"}),
            attempt_id_factory=lambda: "att-1",
        )
        _, obs_mock = adapter_mock.call("publish", {"kw": "测试", "title": "分享", "content": "热梗1"})
        _, obs_real = adapter_real.call("publish", {"kw": "测试", "title": "分享", "content": "热梗1"})
        self.assertEqual(obs_mock, obs_real)
        self.assertEqual(obs_mock, '{"status":"ok","post_id":"1","attempt_id":"att-1"}')


class TestThreeTierMapping(unittest.TestCase):

    def test_submit_response_with_tid(self):
        self.assertEqual(
            derive_status_from_evidence(submit_response={"ok": True, "tid": "123"}),
            {"status": STATUS_OK, "post_id": "123"},
        )

    def test_falls_back_to_list_probe(self):
        got = derive_status_from_evidence(list_probe={"posts": [{"matched": True}]})
        self.assertEqual(got["status"], STATUS_OK)
        self.assertIsNone(got["post_id"], "列表确认了已发布但拿不到 tid 时不得造 ID")

    def test_page_hint_only_means_unknown(self):
        self.assertEqual(
            derive_status_from_evidence(page_hint="发布成功")["reason"], "no_response"
        )
        self.assertEqual(
            derive_status_from_evidence(page_hint="请输入验证码")["reason"], "captcha"
        )
        self.assertEqual(
            derive_status_from_evidence()["reason"], "submit_timeout"
        )


# --------------------------------------------------------------------------- 验证器


class TestRunnerAndValidator(unittest.TestCase):

    def test_reference_policy_passes_all_families(self):
        expected = {
            FAMILY_NORMAL: CLASS_SUCCESS_NORMAL,
            FAMILY_UNKNOWN_VISIBLE: CLASS_SUCCESS_RECOVERED,
            FAMILY_UNKNOWN_DELAYED: CLASS_SUCCESS_RECOVERED,
            FAMILY_UNKNOWN_NOT_PUBLISHED: CLASS_SUCCESS_RECOVERED,
            FAMILY_UNKNOWN_RESUBMIT_UNKNOWN: CLASS_SUCCESS_RECOVERED,
        }
        for family, label in expected.items():
            with self.subTest(family=family):
                scenario = one_scenario(family)
                policy = ReferencePolicy(verify_budget=max(3, scenario.delay_k + 1))
                traj = run(policy, scenario)
                self.assertEqual(classify(traj), label, traj["scenario"])

    def test_resubmit_immediately_creates_duplicate(self):
        scenario = one_scenario(FAMILY_UNKNOWN_VISIBLE)
        traj = run(ResubmitImmediatelyPolicy(), scenario)
        self.assertEqual(traj["truth"]["post_count"], 2)
        self.assertEqual(classify(traj), CLASS_DUPLICATE_POST)

    def test_resubmit_on_not_published_is_model_failure(self):
        scenario = one_scenario(FAMILY_UNKNOWN_NOT_PUBLISHED)
        traj = run(ResubmitImmediatelyPolicy(), scenario)
        self.assertEqual(traj["truth"]["post_count"], 1, "重发恰好补上了那一篇")
        self.assertEqual(classify(traj), CLASS_FAILED_MODEL, "但没查证，首动作不合规")

    def test_query_once_misjudges_long_delay(self):
        scenario = one_scenario(FAMILY_UNKNOWN_DELAYED)
        scenario.delay_k = 2
        scenario.fault_plan[1] = FaultSpec(STATUS_UNKNOWN, "submit_timeout", published=True, visible_after=2)
        traj = run(QueryOncePolicy(), scenario)
        self.assertEqual(traj["truth"]["post_count"], 2)
        self.assertEqual(classify(traj), CLASS_DUPLICATE_POST)

    def test_tool_error_excluded_from_denominator(self):
        scenario = one_scenario(FAMILY_UNKNOWN_VISIBLE)
        scenario.fault_plan = {1: FaultSpec(STATUS_ERROR, "login_expired")}
        traj = run(ReferencePolicy(), scenario)
        self.assertEqual(classify(traj), CLASS_FAILED_TOOL)

        metrics = episode_metrics([traj])
        self.assertEqual(metrics["n_total"], 1)
        self.assertEqual(metrics["n_evaluable"], 0)
        self.assertEqual(metrics["duplicate_post_rate"], 0.0)

    def test_step_limit_flags_failure(self):
        class Loop:
            def act(self, history, scenario):
                from tieba_env.runner import ToolAction

                return ToolAction("list_recent_posts", {"kw": scenario.kw})

        scenario = one_scenario(FAMILY_NORMAL)
        scenario.step_limit = 3
        traj = run(Loop(), scenario)
        self.assertTrue(traj["hit_step_limit"])
        self.assertEqual(classify(traj), CLASS_FAILED_MODEL)

    def test_model_error_recorded(self):
        class BadTool:
            def act(self, history, scenario):
                from tieba_env.runner import ToolAction

                return ToolAction("query_by_attempt_id", {"attempt_id": "att-1"})

        scenario = one_scenario(FAMILY_NORMAL)
        traj = run(BadTool(), scenario)
        self.assertTrue(traj["model_errors"])
        self.assertEqual(classify(traj), CLASS_FAILED_MODEL)

    def test_reproducible_with_same_seed(self):
        scenario = one_scenario(FAMILY_UNKNOWN_DELAYED, seed=5)

        def deterministic_run():
            env = MockTiebaEnv()
            counter = {"n": 0}

            def factory():
                counter["n"] += 1
                return f"att-{counter['n']}"

            adapter = TiebaAdapter(MockBackend(env), attempt_id_factory=factory)
            return run_episode(env, adapter, scenario, ReferencePolicy())

        first = deterministic_run()
        second = deterministic_run()
        self.assertEqual(
            [c["observation"] for c in first["tool_calls"]],
            [c["observation"] for c in second["tool_calls"]],
        )
        self.assertEqual(first["truth"], second["truth"])

    def test_sharegpt_export_shape(self):
        scenario = one_scenario(FAMILY_UNKNOWN_VISIBLE)
        traj = run(ReferencePolicy(), scenario)
        conv = trajectory_to_sharegpt(traj)["conversations"]
        self.assertEqual(conv[0]["from"], "system")
        self.assertEqual(conv[1]["from"], "human")
        self.assertTrue(conv[2]["value"].startswith("<tool_call>"))
        self.assertEqual(conv[3]["from"], "tool")
        self.assertEqual(conv[-1]["from"], "gpt")
        self.assertNotIn("先查证", conv[0]["value"], "system prompt 不得包含具体策略")


class TestScenarioGenerator(unittest.TestCase):

    def test_five_families_and_isolation(self):
        train = generate_scenarios("train", per_family=2, seed=0)
        test = generate_scenarios("test", per_family=2, seed=0)
        self.assertEqual(len(train), 10)
        self.assertEqual(len(test), 10)

        train_pool = {s.candidate for s in train}
        test_pool = {s.candidate for s in test}
        self.assertFalse(train_pool & test_pool, "训练集与测试集热梗不得重叠")
        self.assertNotEqual({s.kw for s in train}, {s.kw for s in test})
        self.assertNotEqual(
            sorted(s.delay_k for s in train if s.family == FAMILY_UNKNOWN_DELAYED),
            [],
        )

    def test_candidates_file_has_20_entries(self):
        data = load_candidates()
        self.assertEqual(len(data), 20)
        splits = {c["split"] for c in data}
        self.assertEqual(splits, {"train", "test", "real"})

    def test_scenario_truth_expectations(self):
        cases = {
            FAMILY_NORMAL: (1, 1),
            FAMILY_UNKNOWN_VISIBLE: (1, 1),
            FAMILY_UNKNOWN_DELAYED: (1, 1),
            FAMILY_UNKNOWN_NOT_PUBLISHED: (1, 2),
            FAMILY_UNKNOWN_RESUBMIT_UNKNOWN: (0, 2),
        }
        for family, (posts, max_pub) in cases.items():
            scenario = one_scenario(family)
            self.assertEqual(scenario.expected_post_count, posts, family)
            self.assertEqual(scenario.max_publish_calls, max_pub, family)


if __name__ == "__main__":
    unittest.main()
