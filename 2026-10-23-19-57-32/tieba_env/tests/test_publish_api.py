"""publish_api 的离线测试：不启动浏览器、不真实发帖。

两个重点：
  1. 传参直达 —— title / content / kw 必须原样落到 `publish_one` 的实参上；
  2. 输出形态 —— 严格是四状态 JSON，且与模拟器观测逐字节同构。
"""

from __future__ import annotations

import unittest
from typing import Any

from publish_api import CONTENT_LIMIT, TITLE_LIMIT, PublishResult, Poster
from tieba_env.adapter import MockBackend, TiebaAdapter, dumps_observation
from tieba_env.env import MockTiebaEnv
from tieba_env.realcli import classify_verdict
from tieba_env.types import STATUS_ERROR, STATUS_OK, STATUS_REJECTED, STATUS_UNKNOWN


class FakePosterModule:
    """替身：记录 publish_one 收到的每一个实参。"""

    def __init__(self, result: tuple[bool, str] = (True, "https://tieba.baidu.com/p/987654")):
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def publish_one(self, edge, kw, title, content, max_len=2000):  # noqa: ANN001
        self.calls.append(
            {"edge": edge, "kw": kw, "title": title, "content": content, "max_len": max_len}
        )
        return self.result


def make_poster(fake: FakePosterModule) -> Poster:
    poster = Poster(poster_dir="ignored")
    poster._bp = fake          # 跳过 _import_poster
    poster._edge = object()    # 跳过 _open_browser
    return poster


def run_once(result: tuple[bool, str]) -> dict[str, Any]:
    return make_poster(FakePosterModule(result)).publish("我的吧", "标题", "正文")  # type: ignore[return-value]


class TestArgumentsReachScript(unittest.TestCase):
    def test_three_params_pass_through_verbatim(self):
        fake = FakePosterModule()
        poster = make_poster(fake)
        title = "这个标题不会被改"
        content = "第一段\n第二段\n带 emoji 😀 和标点，。！"

        poster.publish("我的测试吧", title, content)

        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["kw"], "我的测试吧")
        self.assertEqual(call["title"], title, "标题必须原样传给脚本")
        self.assertEqual(call["content"], content, "正文必须原样传给脚本")

    def test_long_title_is_truncated_before_calling(self):
        fake = FakePosterModule()
        poster = make_poster(fake)
        title = "字" * 50

        result = poster.publish("吧", title, "正文", detailed=True)
        assert isinstance(result, PublishResult)

        self.assertEqual(fake.calls[0]["title"], "字" * TITLE_LIMIT)
        self.assertTrue(result.title_truncated)
        self.assertEqual(result.title_sent, "字" * TITLE_LIMIT)

    def test_long_content_is_truncated(self):
        fake = FakePosterModule()
        poster = make_poster(fake)
        content = "正" * (CONTENT_LIMIT + 500)

        result = poster.publish("吧", "标题", content, detailed=True)
        assert isinstance(result, PublishResult)

        self.assertEqual(len(fake.calls[0]["content"]), CONTENT_LIMIT)
        self.assertTrue(result.content_truncated)
        self.assertEqual(result.content_chars, CONTENT_LIMIT)

    def test_whitespace_is_stripped(self):
        fake = FakePosterModule()
        poster = make_poster(fake)
        poster.publish("  吧  ", "  标题  ", "  正文  ")
        call = fake.calls[0]
        self.assertEqual((call["kw"], call["title"], call["content"]), ("吧", "标题", "正文"))

    def test_empty_fields_are_rejected_before_browser(self):
        fake = FakePosterModule()
        poster = make_poster(fake)
        for kw, title, content in [("", "t", "c"), ("k", "", "c"), ("k", "t", "")]:
            with self.assertRaises(ValueError):
                poster.publish(kw, title, content)
        self.assertEqual(fake.calls, [], "参数不合法时不该调用脚本")


class TestPayloadShapes(unittest.TestCase):
    """四种形态的键必须精确匹配，不多不少。"""

    def test_ok_has_post_id_and_attempt_id(self):
        payload = run_once((True, "https://tieba.baidu.com/p/123456"))
        self.assertEqual(list(payload), ["status", "post_id", "attempt_id"])
        self.assertEqual(payload["status"], STATUS_OK)
        self.assertEqual(payload["post_id"], "123456")
        self.assertTrue(payload["attempt_id"].startswith("att-"))

    def test_ok_without_tid_is_resolved_by_recheck(self):
        """脚本只给 UI 信号时（拿不到链接），按标题回查把 post_id 补上。"""
        fake = FakePosterModule((True, "发帖弹层已关闭，判断为发送成功（拿不到帖子链接）"))
        poster = make_poster(fake)
        poster._recheck_post_id = lambda kw, title: "555"  # type: ignore[method-assign]

        payload = poster.publish("吧", "标题", "正文")

        self.assertEqual(list(payload), ["status", "post_id", "attempt_id"])
        self.assertEqual(payload["status"], STATUS_OK)
        self.assertEqual(payload["post_id"], "555")

    def test_ok_without_tid_degrades_to_unknown_when_recheck_fails(self):
        """回查也找不到 → 不认成功。空的 post_id 会让下游对账错位。"""
        fake = FakePosterModule((True, "发帖弹层已关闭，判断为发送成功（拿不到帖子链接）"))
        poster = make_poster(fake)
        poster._recheck_post_id = lambda kw, title: None  # type: ignore[method-assign]

        payload = poster.publish("吧", "标题", "正文")

        self.assertEqual(list(payload), ["status", "reason", "attempt_id"])
        self.assertEqual(payload["status"], STATUS_UNKNOWN)
        self.assertEqual(payload["reason"], "no_response")
        self.assertNotIn("post_id", payload)

    def test_recheck_can_be_disabled(self):
        fake = FakePosterModule((True, "发帖弹层已关闭，判断为发送成功（拿不到帖子链接）"))
        poster = make_poster(fake)
        poster._recheck_post_id = lambda kw, title: "should-not-be-called"  # type: ignore[method-assign]

        payload = poster.publish("吧", "标题", "正文", recheck=False)

        self.assertEqual(payload["status"], STATUS_UNKNOWN)
        self.assertNotIn("post_id", payload)

    def test_unknown_has_reason_and_attempt_id_but_no_post_id(self):
        payload = run_once((False, "无法确认：接口没给出明确结果，页面上也没看到新帖或报错。"))
        self.assertEqual(list(payload), ["status", "reason", "attempt_id"])
        self.assertEqual(payload["status"], STATUS_UNKNOWN)
        self.assertEqual(payload["reason"], "no_response")
        self.assertNotIn("post_id", payload, "unknown 不许带 post_id")

    def test_rejected_has_no_attempt_id(self):
        """rejected 明确知道没发出去，不需要客户端 ID 对账。"""
        payload = run_once((False, "[273] 等级不足"))
        self.assertEqual(list(payload), ["status", "reason"])
        self.assertEqual(payload["status"], STATUS_REJECTED)
        self.assertEqual(payload["reason"], "level_limit")
        self.assertNotIn("attempt_id", payload)

    def test_error_has_no_attempt_id(self):
        payload = run_once((False, "[101] 未登录"))
        self.assertEqual(list(payload), ["status", "reason"])
        self.assertEqual(payload["status"], STATUS_ERROR)
        self.assertEqual(payload["reason"], "login_expired")
        self.assertNotIn("attempt_id", payload)

    def test_crash_is_error_selector_missing(self):
        fake = FakePosterModule()
        fake.publish_one = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("CDP 断了"))  # type: ignore[method-assign]
        payload = make_poster(fake).publish("吧", "标题", "正文")
        self.assertEqual(payload, {"status": STATUS_ERROR, "reason": "selector_missing"})

    def test_detail_mode_returns_audit_object(self):
        fake = FakePosterModule((True, "https://tieba.baidu.com/p/777"))
        result = make_poster(fake).publish("吧", "标题", "正文", detailed=True)
        assert isinstance(result, PublishResult)
        self.assertEqual(result.post_id, "777")
        self.assertEqual(result.post_url, "https://tieba.baidu.com/p/777")
        self.assertEqual(result.json, dumps_observation(result.payload))
        self.assertTrue(result.json.startswith('{"status":"ok"'))


class TestIsomorphicWithAdapter(unittest.TestCase):
    """同一个动作，真实直调与模拟器给出的观测必须逐字节相同。"""

    def _adapter_case(self, *, require_tid: bool) -> tuple[dict[str, Any], str]:
        env = MockTiebaEnv(candidates=["热梗1"])
        env.reset()
        adapter = TiebaAdapter(MockBackend(env), attempt_id_factory=lambda: "att-fixed")
        result, observation = adapter.call(
            "publish", {"kw": "测试", "title": "分享", "content": "热梗1"}
        )
        return result, observation

    def test_ok_observation_matches(self):
        _, observation = self._adapter_case(require_tid=True)
        payload = make_poster(FakePosterModule((True, "https://tieba.baidu.com/p/1"))).publish(
            "测试", "分享", "热梗1"
        )
        # post_id 与 attempt_id 不同，所以只比对键序与字符化方式：
        # adapter 用 dumps_observation，我们也是同一个函数 → 结构必须一致
        self.assertEqual(
            dumps_observation(payload).replace(payload["attempt_id"], "att-fixed").replace(
                str(payload["post_id"]), "1"
            ),
            observation,
        )

    def test_unknown_observation_matches(self):
        from tieba_env.types import FaultSpec

        env = MockTiebaEnv(candidates=["热梗1"])
        env.reset(fault_plan={1: FaultSpec(STATUS_UNKNOWN, "no_response")})
        adapter = TiebaAdapter(MockBackend(env), attempt_id_factory=lambda: "att-fixed")
        _, observation = adapter.call(
            "publish", {"kw": "测试", "title": "分享", "content": "热梗1"}
        )
        payload = make_poster(
            FakePosterModule((False, "无法确认：接口没给出明确结果"))
        ).publish("测试", "分享", "热梗1")
        self.assertEqual(
            dumps_observation(payload).replace(payload["attempt_id"], "att-fixed"),
            observation,
        )

    def test_rejected_observation_matches(self):
        env = MockTiebaEnv(candidates=["热梗1"], title_limit=2)
        env.reset()
        adapter = TiebaAdapter(MockBackend(env), attempt_id_factory=lambda: "att-fixed")
        # 3 字 > title_limit=2 → mock 记 rejected/title_too_long
        _, observation = adapter.call(
            "publish", {"kw": "测试", "title": "太长了", "content": "热梗1"}
        )
        payload = make_poster(FakePosterModule((False, "标题填写失败"))).publish(
            "测试", "太长了", "热梗1"
        )
        self.assertEqual(dumps_observation(payload), observation)

    def test_error_observation_matches(self):
        from tieba_env.adapter import merge_publish

        payload = merge_publish({"status": STATUS_ERROR, "reason": "login_expired"}, "att-fixed")
        self.assertEqual(dumps_observation(payload), '{"status":"error","reason":"login_expired"}')

    def test_all_states_share_the_same_key_order(self):
        """键序：status → post_id/reason → attempt_id（后两者只有 ok/unknown 才有）。"""
        from tieba_env.adapter import merge_publish

        cases = [
            ({"status": STATUS_OK, "post_id": "1"}, ["status", "post_id", "attempt_id"]),
            ({"status": STATUS_UNKNOWN, "reason": "captcha"}, ["status", "reason", "attempt_id"]),
            ({"status": STATUS_REJECTED, "reason": "duplicate"}, ["status", "reason"]),
            ({"status": STATUS_ERROR, "reason": "selector_missing"}, ["status", "reason"]),
        ]
        for raw, expected_keys in cases:
            self.assertEqual(list(merge_publish(raw, "att-x")), expected_keys)


class TestClassifyVerdict(unittest.TestCase):
    def test_shared_rules(self):
        self.assertEqual(classify_verdict(True, "https://tieba.baidu.com/p/1")["post_id"], "1")
        self.assertEqual(classify_verdict(False, "无法确认：xxx")["status"], STATUS_UNKNOWN)
        self.assertEqual(classify_verdict(False, "[273] 等级不足")["status"], STATUS_REJECTED)
        self.assertEqual(classify_verdict(False, "[101] 未登录")["reason"], "login_expired")
        self.assertEqual(classify_verdict(False, "标题填写失败")["status"], STATUS_REJECTED)


if __name__ == "__main__":
    unittest.main()
