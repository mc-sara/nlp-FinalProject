"""真实侧支撑层的单元测试（不需要网络、不启动浏览器）。

运行：
    python -m unittest tieba_env.tests.test_real -v
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tieba_env.env import MockTiebaEnv
from tieba_env.realcli import (
    ConfigError,
    GuardError,
    PublishGuard,
    RealCliConfig,
    _check_level,
    _check_list,
    _check_login,
    health_check,
    parse_poster_output,
    poll_for_title,
)
from tieba_env.types import (
    REJECTED_REASONS,
    REJECTED_REASONS_MOCK_ONLY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    FaultSpec,
)

BASE_CONFIG = {
    "poster_dir": "C:/tmp/tieba-poster",
    "python": "C:/tmp/tieba-poster/.venv/Scripts/python.exe",
    "poster_script": "C:/tmp/tieba-poster/browser_poster.py",
    "probe_script": "C:/tmp/real/real_probe.py",
    "tieba_config": "config.json",
    "kw": "自建吧",
}


def make_cfg(**overrides) -> RealCliConfig:
    return RealCliConfig.from_dict({**BASE_CONFIG, **overrides})


# --------------------------------------------------------------------------- 输出解析


class TestParsePosterOutput(unittest.TestCase):
    def test_ok_with_tid(self):
        out = "[10:31:02] [1/1] 吧名：python  标题：x\n    成功（9.2s）https://tieba.baidu.com/p/11127594809\n"
        payload, _ = parse_poster_output(out, 0)
        self.assertEqual(payload["status"], STATUS_OK)
        self.assertEqual(payload["post_id"], "11127594809")

    def test_ok_without_tid(self):
        payload, _ = parse_poster_output("    成功（8.4s）发帖弹层已关闭，判断为发送成功（拿不到帖子链接）\n")
        self.assertEqual(payload["status"], STATUS_OK)
        self.assertIsNone(payload["post_id"], "拿不到链接时不得凭空造 ID")

    def test_cannot_confirm_is_unknown(self):
        payload, _ = parse_poster_output("    未成功（10.1s）：无法确认：接口没给出明确结果，页面上也没看到新帖\n")
        self.assertEqual(payload["status"], STATUS_UNKNOWN)
        self.assertEqual(payload["reason"], "no_response")

    def test_captcha_is_unknown(self):
        # 验证码意味着"可能发出去了也可能没有"，所以是 unknown 而不是 rejected
        payload, _ = parse_poster_output("    未成功（8.0s）：[4] 需要验证码\n")
        self.assertEqual(payload["status"], STATUS_UNKNOWN)
        self.assertEqual(payload["reason"], "captcha")

    def test_level_rejection(self):
        for text in ("[273] 被拒", "[1011] 未加入该吧", "[34] 该吧禁止发帖"):
            with self.subTest(text=text):
                payload, _ = parse_poster_output(f"    未成功（11.7s）：{text}\n")
                self.assertEqual(payload["status"], STATUS_REJECTED)
                self.assertEqual(payload["reason"], "level_limit")

    def test_content_and_rate_limit(self):
        payload, _ = parse_poster_output("    未成功（9.0s）：[1] 内容不合法\n")
        self.assertEqual(payload["reason"], "content_invalid")
        payload, _ = parse_poster_output("    未成功（9.5s）：[10] 发帖过于频繁\n")
        self.assertEqual(payload["reason"], "rate_limited")

    def test_login_expired_is_tool_error(self):
        payload, _ = parse_poster_output("    未成功（3.1s）：[101] 未登录\n")
        self.assertEqual(payload["status"], STATUS_ERROR)
        self.assertEqual(payload["reason"], "login_expired")

    def test_unmapped_failure_stays_unknown(self):
        payload, note = parse_poster_output("    未成功（9.9s）：某种没见过的报错\n")
        self.assertEqual(payload["status"], STATUS_UNKNOWN, "看不懂就必须说不确定，不能猜")
        self.assertIn("未映射", note)

    def test_crash_without_verdict_is_tool_error(self):
        payload, _ = parse_poster_output("Traceback (most recent call last): ...", 1)
        self.assertEqual(payload["status"], STATUS_ERROR)
        self.assertEqual(payload["reason"], "selector_missing")

    def test_clean_exit_without_verdict_is_unknown(self):
        payload, _ = parse_poster_output("启动浏览器 ...\n已注入 12 条 Cookie\n", 0)
        self.assertEqual(payload["status"], STATUS_UNKNOWN)
        self.assertEqual(payload["reason"], "no_response")


# --------------------------------------------------------------------------- 配置


class TestConfig(unittest.TestCase):
    def test_missing_required_field(self):
        broken = dict(BASE_CONFIG)
        broken.pop("probe_script")
        with self.assertRaises(ConfigError) as ctx:
            RealCliConfig.from_dict(broken)
        self.assertIn("probe_script", str(ctx.exception))

    def test_resolve_relative_to_poster_dir(self):
        cfg = make_cfg()
        self.assertEqual(Path(cfg.resolve("config.json")), Path("C:/tmp/tieba-poster/config.json"))
        self.assertEqual(Path(cfg.resolve("D:/abs/x.json")), Path("D:/abs/x.json"))

    def test_publish_whitelist_defaults_empty(self):
        cfg = make_cfg()
        self.assertEqual(cfg.allowed_publish_kw, [], "默认不允许实发，必须先显式加白名单")


# --------------------------------------------------------------------------- 健康检查


def stub_transport(**overrides):
    payloads = {
        "login": {"ok": True, "user": "u", "tbs_ok": True},
        "level": {"kw": "自建吧", "level": 6, "liked": True},
        "editor": {"editor_ready": True, "detail": "全命中"},
        "list": {"posts": [{"title": "t", "post_id": "1"}]},
    }
    payloads.update(overrides)

    def transport(name, args):
        value = payloads[name]
        if isinstance(value, Exception):
            raise value
        return value

    return transport


class TestHealthCheck(unittest.TestCase):
    def test_all_pass(self):
        report = health_check(stub_transport(), make_cfg())
        self.assertTrue(report["ok"])
        self.assertEqual(set(report["checks"]), {"login", "level", "editor", "list"})

    def test_login_expired_fails_whole(self):
        report = health_check(stub_transport(login={"ok": False}), make_cfg())
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["login"]["ok"])

    def test_exception_is_failure_not_crash(self):
        report = health_check(stub_transport(login=RuntimeError("网络炸了")), make_cfg())
        self.assertFalse(report["checks"]["login"]["ok"])
        self.assertIn("网络炸了", report["checks"]["login"]["detail"])

    def test_empty_list_fails_unless_allowed(self):
        empty = {"posts": []}
        self.assertFalse(health_check(stub_transport(list=empty), make_cfg())["checks"]["list"]["ok"])
        allowed = health_check(stub_transport(list=empty), make_cfg(allow_empty_forum=True))
        self.assertTrue(allowed["checks"]["list"]["ok"])

    def test_editor_skip_is_marked_skipped(self):
        report = health_check(stub_transport(), make_cfg(editor_check=False))
        self.assertTrue(report["checks"]["editor"]["skipped"])
        self.assertTrue(report["ok"])

    def test_level_threshold_only_when_configured(self):
        payload = {"kw": "x", "level": None, "liked": False}
        self.assertTrue(_check_level(payload, make_cfg())["ok"], "不设阈值时只记录，不误杀")
        self.assertFalse(_check_level(payload, make_cfg(require_min_level=4))["ok"])

    def test_field_missing_is_failure(self):
        self.assertFalse(_check_login({}, make_cfg())["ok"])
        self.assertFalse(_check_list({"posts": "oops"}, make_cfg())["ok"])


# --------------------------------------------------------------------------- 回查轮询


class TestPollForTitle(unittest.TestCase):
    def _seq(self, visibility: int):
        state = {"n": 0}

        def list_fn():
            state["n"] += 1
            posts = [{"title": "目标标题", "post_id": "9"}] if state["n"] >= visibility else []
            return {"posts": posts}

        return list_fn, state

    def test_found_at_expected_poll(self):
        list_fn, state = self._seq(visibility=3)
        result = poll_for_title(list_fn, "目标标题", polls=7, interval=2.0, sleep=lambda s: None)
        self.assertTrue(result["found"])
        self.assertEqual(result["matched_at_poll"], 3)
        self.assertEqual(result["measured_delay_k"] if "measured_delay_k" in result else 2, 2)

    def test_not_found_exhausts_polls(self):
        list_fn, state = self._seq(visibility=99)
        result = poll_for_title(list_fn, "目标标题", polls=4, interval=2.0, sleep=lambda s: None)
        self.assertFalse(result["found"])
        self.assertIsNone(result["matched_at_poll"])
        self.assertEqual(state["n"], 4, "只轮询约定次数，不无限重试")

    def test_whitespace_is_ignored(self):
        list_fn = lambda: {"posts": [{"title": "目标 标题"}]}  # noqa: E731
        self.assertTrue(poll_for_title(list_fn, "目标标题", polls=1, sleep=lambda s: None)["found"])

    def test_sleep_count(self):
        slept: list[float] = []
        list_fn, _ = self._seq(visibility=99)
        poll_for_title(list_fn, "x", polls=3, interval=2.0, sleep=slept.append)
        self.assertEqual(slept, [2.0, 2.0], "最后一次查完不该再等")


# --------------------------------------------------------------------------- 限额守卫


class TestPublishGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.now = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)
        self.cfg = make_cfg(state_path=str(self.state), publish_interval_seconds=600, max_publishes_per_day=2)

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, now=None):
        return PublishGuard(self.cfg, now_fn=lambda: now or self.now)

    def test_first_publish_allowed(self):
        self.guard().check()

    def test_interval_enforced(self):
        guard = self.guard()
        guard.record_publish()
        with self.assertRaises(GuardError) as ctx:
            self.guard(self.now + timedelta(seconds=120)).check()
        self.assertIn("还需等", str(ctx.exception))
        self.guard(self.now + timedelta(seconds=601)).check()

    def test_daily_cap(self):
        guard = self.guard()
        guard.record_publish()
        self.guard(self.now + timedelta(minutes=15)).record_publish()
        with self.assertRaises(GuardError) as ctx:
            self.guard(self.now + timedelta(minutes=30)).check()
        self.assertIn("上限", str(ctx.exception))

    def test_captcha_cooldown(self):
        guard = self.guard()
        guard.record_captcha()
        with self.assertRaises(GuardError) as ctx:
            self.guard(self.now + timedelta(hours=1)).check()
        self.assertIn("冷却", str(ctx.exception))
        self.guard(self.now + timedelta(hours=25)).check()


# --------------------------------------------------------------------------- 吧页解析


MOBILE_HTML = """
<ul class="threads_list" id="frslistcontent">
<li class="tl_top scale-1px-bottom" data-tid="8641225222" data-floor="0"><a href="/p/8641225222?lp=5028" tid="8641225222"><div class="ti_title">
<span class="ti_title_icon ti_icon_zhiding">置顶</span> <span>视为广告帖以及交易帖的一些机制</span></div></a></li>
<li class="tl_top" data-tid="8651017931"><a href="/p/8651017931"><div class="ti_title">
<span class="ti_title_icon ti_icon_zhiding">置顶</span> <span>资源导航</span></div></a></li>
<li class="scale-1px-bottom" data-tid="11132324188"><a href="/p/11132324188?lp=5028" class="j_common ti_item" data-tid="11132324188">
<div class="ti_title"> <span>for循环</span></div> <div class="medias_wrap"></div></a></li>
<li class="scale-1px-bottom" data-tid="11090300469"><a href="/p/11090300469"><div class="ti_title">
<span>求问这是哪错了 &amp; 怎么改</span></div></a></li>
</ul>
<div class="ad"><a href="https://tieba.baidu.com/ad/xxx">广告位</a></div>
"""


class TestForumListParser(unittest.TestCase):
    def setUp(self):
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "real" / "real_probe.py"
        spec = importlib.util.spec_from_file_location("real_probe", path)
        self.module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(self.module)

    def test_parses_threads_and_flags_pinned(self):
        posts = self.module.parse_threads(MOBILE_HTML, limit=10)
        self.assertEqual(len(posts), 4, "广告位没有 tid，必须被过滤掉")
        self.assertEqual(posts[0]["post_id"], "8641225222")
        self.assertEqual(posts[0]["title"], "视为广告帖以及交易帖的一些机制", "图标文字不能混进标题")
        self.assertTrue(posts[0]["pinned"])
        self.assertFalse(posts[2]["pinned"])
        self.assertEqual(posts[3]["title"], "求问这是哪错了 & 怎么改", "HTML 实体要解码")

    def test_limit_respected(self):
        self.assertEqual(len(self.module.parse_threads(MOBILE_HTML, limit=2)), 2)

    def test_empty_page(self):
        self.assertEqual(self.module.parse_threads("<html></html>"), [])


# --------------------------------------------------------------------------- 契约一致性


class TestReasonContract(unittest.TestCase):
    def test_mock_only_emits_documented_reasons(self):
        env = MockTiebaEnv(candidates=["a"], title_limit=5)
        env.reset()
        reasons = set()
        reasons.add(env.publish("测试", "标题超长了吧", "a").reason)
        env.account_level = 1
        reasons.add(env.publish("测试", "短", "a").reason)
        env.duplicate_guard = True
        env.account_level = 9
        env.publish("测试", "短", "a")
        reasons.add(env.publish("测试", "短", "a").reason)
        self.assertEqual(reasons, set(REJECTED_REASONS_MOCK_ONLY))
        self.assertTrue(set(REJECTED_REASONS_MOCK_ONLY) <= set(REJECTED_REASONS))

    def test_ok_without_post_id_is_representable(self):
        env = MockTiebaEnv(candidates=["a"])
        env.reset(fault_plan={1: FaultSpec(STATUS_OK, withhold_post_id=True)})
        result = env.publish("测试", "标题", "正文")
        self.assertEqual(result.status, STATUS_OK)
        self.assertIsNone(result.observation["post_id"], "模型看不到 ID")
        self.assertEqual(env.truth()["post_count"], 1, "但真值里确实发出去了")
        self.assertEqual(env.truth()["posts"][0]["post_id"], "1", "真值仍然记录 tid")


class TestListShapeMatchesReal(unittest.TestCase):
    """真实吧页只有标题没有正文，模拟器必须能切成同样的字段形态。"""

    def test_title_only_shape(self):
        from tieba_env.adapter import MockBackend, TiebaAdapter

        env = MockTiebaEnv(list_includes_content=False)
        env.reset()
        env.publish("测试", "标题", "正文")
        observation = TiebaAdapter(MockBackend(env)).call("list_recent_posts", {"kw": "测试"})[1]
        self.assertEqual(observation, '{"posts":[{"title":"标题"}]}')

    def test_reference_policy_recovers_with_title_only(self):
        from tieba_env.adapter import MockBackend, TiebaAdapter
        from tieba_env.policies import ReferencePolicy
        from tieba_env.runner import run_episode
        from tieba_env.scenarios import FAMILY_UNKNOWN_DELAYED, generate_scenarios
        from tieba_env.validator import CLASS_SUCCESS_RECOVERED, classify

        scenario = generate_scenarios("test", families=[FAMILY_UNKNOWN_DELAYED], per_family=1, seed=3)[0]
        env = MockTiebaEnv(list_includes_content=False)
        traj = run_episode(env, TiebaAdapter(MockBackend(env)), scenario, ReferencePolicy(verify_budget=6))
        self.assertEqual(
            classify(traj), CLASS_SUCCESS_RECOVERED,
            "查证退回标题比对后仍应恢复成功，否则训出来的策略在真实侧永远查不到",
        )


if __name__ == "__main__":
    unittest.main()
