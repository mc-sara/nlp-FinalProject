"""MockTiebaEnv —— 贴吧发帖工具的内存模拟器。

只模拟三件事（PLAN.md 2.2）：
  1. 三个工具返回的 JSON 格式；
  2. 帖子库的真实状态（环境真值，验证器直接读）；
  3. 吧页列表的回查延迟。

不模拟贴吧界面、推荐算法、审核机制和真实用户评论。

最重要的一个细节是回查延迟：发帖后前 k 次 list_recent_posts 查不到该帖。
如果省掉它，查询会立刻返回结果，模型就会学到"查一次，查不到就重发"——
而这正是真实环境里产生重复帖的原因。k 由 D1 实测决定（见 README 的待填参数表）。
"""

from __future__ import annotations

import random
from collections import Counter
from typing import Any, Mapping, Sequence

from .types import (
    ERROR_REASONS,
    REJECTED_REASONS,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    UNKNOWN_REASONS,
    FaultRates,
    FaultSpec,
    Post,
    PublishRecord,
    PublishResult,
)


_UNSET = object()


def delay_k_from_measurement(seconds: float, poll_interval: float = 2.0) -> int:
    """把 D1 实测的"帖子第几秒可见"换算成模拟器用的 k（查询次数）。

    现有工具的吧页回查是"轮询 7 次、每次间隔 2 秒"，所以一次 list_recent_posts
    调用对应一次轮询。实测 5 秒可见、间隔 2 秒 → 需要第 3 次轮询才命中 → k=2。
    """
    if seconds < 0:
        raise ValueError("seconds 不能为负")
    if poll_interval <= 0:
        raise ValueError("poll_interval 必须为正")
    return max(0, int(-(-seconds // poll_interval)) - 1)


class MockTiebaEnv:
    """一个吧（或多吧）的内存帖子库 + 故障注入 + 回查延迟。

    用法：

        env = MockTiebaEnv(candidates=["热梗1", "热梗2"], delay_k=2)
        env.reset(fault_plan={1: FaultSpec(STATUS_UNKNOWN, "submit_timeout",
                                           published=True, visible_after=2)})
        env.publish("测试", "分享", "热梗1")   # -> unknown，真值：已发布但查不到
        env.list_recent_posts("测试")          # -> 空
        env.list_recent_posts("测试")          # -> 空
        env.list_recent_posts("测试")          # -> 命中
        env.truth()["post_count"]              # -> 1
    """

    def __init__(
        self,
        candidates: Sequence[str] | None = None,
        *,
        delay_k: int = 2,
        duplicate_guard: bool = False,
        title_limit: int = 40,
        account_level: int = 6,
        required_level: int = 4,
        list_default_limit: int = 20,
        list_includes_post_id: bool = False,
        list_includes_content: bool = True,
        fault_plan: Mapping[int, FaultSpec] | None = None,
        fault_rates: FaultRates | None = None,
        seed: int = 0,
        post_id_base: int = 1,
        clock_seconds_per_poll: float = 2.0,
    ) -> None:
        if delay_k < 0:
            raise ValueError("delay_k 不能为负")
        if list_default_limit <= 0:
            raise ValueError("list_default_limit 必须为正")

        self.candidates: list[str] = list(candidates or [])
        self.delay_k = delay_k
        # 真实贴吧对完全同内容的重复帖不一定拒（常常静默成功），所以默认放行：
        # 若默认开启，主指标"重复帖产生率"永远为 0，实验就失去意义。
        self.duplicate_guard = duplicate_guard
        self.title_limit = title_limit
        self.account_level = account_level
        self.required_level = required_level
        self.list_default_limit = list_default_limit
        self.list_includes_post_id = list_includes_post_id
        # 真实吧页列表只给标题、不给正文（实测 /mo/q/m 只有 ti_title）。
        # 想让模拟环境与真实侧逐字段一致就设 False；保留 True 则是为了二期语义查重。
        self.list_includes_content = list_includes_content
        self.post_id_base = post_id_base
        self.clock_seconds_per_poll = clock_seconds_per_poll

        self._fault_plan: dict[int, FaultSpec] = dict(fault_plan or {})
        self._fault_rates = fault_rates
        self._seed = seed
        self._rng = random.Random(seed)

        self.posts: list[Post] = []
        self.records: list[PublishRecord] = []
        self.query_count = 0
        self.publish_count = 0

    # ------------------------------------------------------------------ 生命周期

    def reset(
        self,
        *,
        seed: int | None = None,
        fault_plan: Any = _UNSET,
        fault_rates: Any = _UNSET,
        candidates: Sequence[str] | None = None,
        delay_k: int | None = None,
    ) -> None:
        """每条轨迹开始前调用：清空帖子库、计数器和审计记录。

        故障配置只在显式传入时才替换（传 `{}` 表示清空），省略则沿用构造时的设置。
        这样"构造时配一次、逐条轨迹重置"不会意外把故障注入关掉。
        """
        if seed is not None:
            self._seed = seed
        self._rng = random.Random(self._seed)
        if fault_plan is not _UNSET:
            self._fault_plan = dict(fault_plan or {})
        if fault_rates is not _UNSET:
            self._fault_rates = fault_rates
        if candidates is not None:
            self.candidates = list(candidates)
        if delay_k is not None:
            self.delay_k = delay_k
        self.posts.clear()
        self.records.clear()
        self.query_count = 0
        self.publish_count = 0

    # ------------------------------------------------------------------ 工具：get_candidates

    def get_candidates(self) -> dict[str, Any]:
        return {"candidates": list(self.candidates)}

    # ------------------------------------------------------------------ 工具：list_recent_posts

    def list_recent_posts(self, kw: str | None = None, limit: int | None = None) -> dict[str, Any]:
        """吧页列表。既是查重依据，也是唯一的状态查证手段。

        每次调用算一次轮询，会消耗目标吧下未可见帖子的 miss_budget。
        可见性判定发生在扣减之前，所以 k=1 表示"第 1 次查不到、第 2 次能查到"。
        """
        self.query_count += 1
        take = self.list_default_limit if limit is None else max(1, int(limit))

        candidates = [p for p in self.posts if kw is None or p.kw == kw]
        visible: list[Post] = []
        for post in candidates:
            if post.miss_budget > 0:
                post.miss_budget -= 1
                continue
            if post.first_visible_query is None:
                post.first_visible_query = self.query_count
            visible.append(post)

        visible.sort(key=lambda p: p.seq, reverse=True)  # 最新在前
        return {
            "posts": [
                p.to_public(
                    include_post_id=self.list_includes_post_id,
                    include_content=self.list_includes_content,
                )
                for p in visible[:take]
            ]
        }

    # ------------------------------------------------------------------ 工具：publish

    def publish(
        self,
        kw: str,
        title: str,
        content: str,
        *,
        attempt_id: str | None = None,
    ) -> PublishResult:
        """提交发帖。返回四种状态之一，并同步更新帖子库真值。

        attempt_id 只用于环境侧审计，不会被写进 observation。
        """
        for name, value in (("kw", kw), ("title", title), ("content", content)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"publish 的 {name} 必须是非空字符串")

        self.publish_count += 1
        seq = self.publish_count

        # 1) 站点硬规则（真实侧由服务端校验，这里等价复现）
        hard_reject = self._check_site_rules(kw, title, content)
        if hard_reject is not None:
            reason, injected = hard_reject
            return self._record(
                seq, kw, title, content, attempt_id,
                status=STATUS_REJECTED, reason=reason, published=False,
                post_id=None, injected=injected,
            )

        # 2) 故障注入
        fault = self._resolve_fault(seq)

        if fault is None or fault.status == STATUS_OK:
            post = self._write_post(kw, title, content, attempt_id=attempt_id, visible_after=0)
            # 成功但拿不到帖子链接：观测里 post_id 为 null，真值里仍然有这篇
            visible_post_id = None if (fault is not None and fault.withhold_post_id) else post.post_id
            return self._record(
                seq, kw, title, content, attempt_id,
                status=STATUS_OK, reason=None, published=True,
                post_id=visible_post_id, injected=False,
                truth_post_id=post.post_id, fault=fault,
            )

        if fault.status == STATUS_UNKNOWN:
            post = None
            if fault.published:
                post = self._write_post(
                    kw, title, content, attempt_id=attempt_id, visible_after=fault.visible_after
                )
            return self._record(
                seq, kw, title, content, attempt_id,
                status=STATUS_UNKNOWN, reason=fault.reason, published=post is not None,
                post_id=None,  # unknown 刻意不带 post_id
                injected=True, truth_post_id=post.post_id if post else None, fault=fault,
            )

        # rejected / error：都不写帖子
        return self._record(
            seq, kw, title, content, attempt_id,
            status=fault.status, reason=fault.reason, published=False,
            post_id=None, injected=True, fault=fault,
        )

    # ------------------------------------------------------------------ 真值读取

    def truth(self) -> dict[str, Any]:
        """环境真值。验证器只读这里，不采信模型自述。"""
        by_content = Counter(p.content for p in self.posts)
        return {
            "post_count": len(self.posts),
            "max_same_content": max(by_content.values()) if by_content else 0,
            "duplicate_content": sorted([c for c, n in by_content.items() if n > 1]),
            "posts": [p.to_truth() for p in self.posts],
            "publish_attempts": [r.to_truth() for r in self.records],
            "publish_count": self.publish_count,
            "list_calls": self.query_count,
            "candidates": list(self.candidates),
        }

    def posts_in(self, kw: str) -> list[Post]:
        return [p for p in self.posts if p.kw == kw]

    def visibility_report(self) -> list[dict[str, Any]]:
        """每条已发布帖子的实际回查延迟，用于对照 D1 实测值。

        observed_delay = 首次可见时的查询序号 - 发布时的查询序号 - 1
        （发布后第 1 次查询是 published_at_query + 1）
        """
        report = []
        for post in self.posts:
            observed = None
            if post.first_visible_query is not None:
                observed = post.first_visible_query - post.published_at_query - 1
            report.append(
                {
                    "post_id": post.post_id,
                    "content": post.content,
                    "published_at_query": post.published_at_query,
                    "first_visible_query": post.first_visible_query,
                    "observed_delay": observed,
                }
            )
        return report

    # ------------------------------------------------------------------ 内部

    def _check_site_rules(self, kw: str, title: str, content: str) -> tuple[str, bool] | None:
        if self.title_limit and len(title) > self.title_limit:
            return "title_too_long", False
        if self.account_level < self.required_level:
            return "level_limit", False
        if self.duplicate_guard and any(
            p.kw == kw and p.title == title and p.content == content for p in self.posts
        ):
            return "duplicate", False
        return None

    def _resolve_fault(self, seq: int) -> FaultSpec | None:
        if seq in self._fault_plan:
            return self._fault_plan[seq]
        if self._fault_rates is None:
            return None
        return self._sample_fault()

    def _sample_fault(self) -> FaultSpec | None:
        rates = self._fault_rates
        assert rates is not None
        rng = self._rng

        roll = rng.random()
        if roll < rates.error:
            return FaultSpec(STATUS_ERROR, rng.choice(ERROR_REASONS))
        roll -= rates.error
        if roll < rates.rejected:
            return FaultSpec(STATUS_REJECTED, rng.choice(REJECTED_REASONS))
        roll -= rates.rejected
        if roll < rates.unknown:
            published = rng.random() < rates.p_published_when_unknown
            visible_after = 0
            if published and rng.random() < rates.p_delayed_when_published:
                visible_after = max(1, rates.delay_k)
            return FaultSpec(
                STATUS_UNKNOWN,
                rng.choice(UNKNOWN_REASONS),
                published=published,
                visible_after=visible_after,
            )
        return None

    def _write_post(self, kw: str, title: str, content: str, *, attempt_id: str | None, visible_after: int) -> Post:
        post = Post(
            post_id=str(self.post_id_base + len(self.posts)),
            kw=kw,
            title=title,
            content=content,
            seq=len(self.posts) + 1,
            published_at_query=self.query_count,
            miss_budget=max(0, visible_after),
            attempt_id=attempt_id,
        )
        self.posts.append(post)
        return post

    def _record(
        self,
        seq: int,
        kw: str,
        title: str,
        content: str,
        attempt_id: str | None,
        *,
        status: str,
        reason: str | None,
        published: bool,
        post_id: str | None,
        injected: bool,
        truth_post_id: str | None = None,
        fault: FaultSpec | None = None,
    ) -> PublishResult:
        if status == STATUS_OK:
            observation: dict[str, Any] = {"status": STATUS_OK, "post_id": post_id}
        elif status == STATUS_UNKNOWN:
            observation = {"status": STATUS_UNKNOWN, "reason": reason}
        else:
            observation = {"status": status, "reason": reason}

        self.records.append(
            PublishRecord(
                seq=seq,
                kw=kw,
                title=title,
                content=content,
                status=status,
                reason=reason,
                published=published,
                post_id=truth_post_id if truth_post_id is not None else post_id,
                attempt_id=attempt_id,
                injected=injected,
            )
        )
        return PublishResult(
            observation=observation,
            status=status,
            reason=reason,
            published=published,
            post_id=(truth_post_id if truth_post_id is not None else post_id) if published else None,
            fault=fault,
        )
