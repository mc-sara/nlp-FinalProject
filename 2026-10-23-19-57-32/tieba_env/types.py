"""工具契约与模拟器共用的数据结构。

设计约束（来自 PLAN.md 2.1）：
- publish 只允许四种 status，reason 取值封闭，新增取值必须同步改这里和文档；
- `unknown` 不带可信的 post_id —— 这一条由 PublishResult 的字段语义与适配层共同保证；
- `attempt_id` 由客户端生成，只进本地日志，不发给贴吧（所以它不在 env 产出的观测里，
  由适配层在拼装模型可见 JSON 时补上）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

STATUS_OK = "ok"
STATUS_UNKNOWN = "unknown"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"

ALL_STATUSES = (STATUS_OK, STATUS_UNKNOWN, STATUS_REJECTED, STATUS_ERROR)

UNKNOWN_REASONS = ("submit_timeout", "captcha", "no_response")
ERROR_REASONS = ("login_expired", "selector_missing")

# 真实侧扩展：贴吧会返回 PLAN 没列出的拒绝原因（例如「1 内容不合法」「10 发帖过于频繁」）。
# 为了让真实轨迹不被 adapter 当成契约违规丢掉，这里多开两个取值；mock 永远不产生它们
# （tests 里有断言）。改这里等于改工具契约，要同步 PLAN 2.1 与 README 的映射表。
REJECTED_REASONS = ("title_too_long", "duplicate", "level_limit", "content_invalid", "rate_limited")
REJECTED_REASONS_MOCK_ONLY = ("title_too_long", "duplicate", "level_limit")

_REASONS_BY_STATUS: dict[str, tuple[str, ...]] = {
    STATUS_UNKNOWN: UNKNOWN_REASONS,
    STATUS_REJECTED: REJECTED_REASONS,
    STATUS_ERROR: ERROR_REASONS,
}


@dataclass(frozen=True)
class FaultSpec:
    """一次 publish 调用的故障规格：既描述工具返回状态，也描述帖子库真值。

    published         这次尝试是否真的把帖子写进了帖子库（环境真值，模型看不到）
    visible_after     若已发布，前多少次 list_recent_posts 查不到它
    withhold_post_id  已发布但工具拿不到帖子 ID。真实工具确实会这样：发帖弹层关了、
                      判定为成功，但页面没给出 /p/<tid> 链接。模拟器必须能表达这个状态，
                      否则"成功但无 ID"这种情况在训练和评测里根本不会出现。
    """

    status: str = STATUS_OK
    reason: str | None = None
    published: bool = False
    visible_after: int = 0
    withhold_post_id: bool = False

    def __post_init__(self) -> None:
        if self.status not in ALL_STATUSES:
            raise ValueError(f"未知 status: {self.status!r}，只允许 {ALL_STATUSES}")

        if self.status == STATUS_OK:
            # status=ok 表示"这次调用表现正常"，真值必然是已发布且立即可见
            object.__setattr__(self, "reason", None)
            object.__setattr__(self, "published", True)
            object.__setattr__(self, "visible_after", 0)
            return

        allowed = _REASONS_BY_STATUS[self.status]
        if self.reason not in allowed:
            raise ValueError(f"status={self.status} 的 reason 必须是 {allowed} 之一，收到 {self.reason!r}")

        if self.status in (STATUS_REJECTED, STATUS_ERROR):
            object.__setattr__(self, "published", False)
        if self.visible_after < 0:
            raise ValueError("visible_after 不能为负")
        if not self.published:
            object.__setattr__(self, "visible_after", 0)

    @property
    def is_unknown(self) -> bool:
        return self.status == STATUS_UNKNOWN

    def describe(self) -> str:
        parts = [self.status]
        if self.reason:
            parts.append(self.reason)
        parts.append("已发布" if self.published else "未发布")
        if self.published and self.visible_after:
            parts.append(f"回查延迟 {self.visible_after} 次")
        return " / ".join(parts)


@dataclass
class FaultRates:
    """概率式故障注入。

    与"显式计划"互斥：显式计划命中的调用不再走概率采样。真实采样（D1）拿到的
    状态占比填这里，用于构造接近真实分布的批量场景。
    """

    error: float = 0.0
    rejected: float = 0.0
    unknown: float = 0.0
    # unknown 内部真值分布：真的发出去了吗 / 发出去了会不会有回查延迟
    p_published_when_unknown: float = 0.5
    p_delayed_when_published: float = 0.5
    delay_k: int = 2

    def __post_init__(self) -> None:
        for name in ("error", "rejected", "unknown", "p_published_when_unknown", "p_delayed_when_published"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} 必须在 [0, 1] 内，收到 {value}")
        if self.error + self.rejected + self.unknown > 1.0:
            raise ValueError("error + rejected + unknown 不能超过 1")


@dataclass
class Post:
    """帖子库里的一条真实记录（环境真值）。"""

    post_id: str
    kw: str
    title: str
    content: str
    seq: int                      # 写入顺序，用于列表排序
    published_at_query: int       # 发布时的查询计数
    miss_budget: int              # 还能"看不见"多少次 list 查询
    attempt_id: str | None = None
    first_visible_query: int | None = None

    @property
    def visible(self) -> bool:
        return self.miss_budget <= 0

    def to_public(self, *, include_post_id: bool, include_content: bool = True) -> dict[str, Any]:
        """吧页列表里模型能看到的字段。"""
        item: dict[str, Any] = {"title": self.title}
        if include_content:
            item["content"] = self.content
        if include_post_id:
            item["post_id"] = self.post_id
        return item

    def to_truth(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id,
            "kw": self.kw,
            "title": self.title,
            "content": self.content,
            "seq": self.seq,
            "published_at_query": self.published_at_query,
            "first_visible_query": self.first_visible_query,
            "attempt_id": self.attempt_id,
        }


@dataclass
class PublishRecord:
    """一次 publish 调用的审计记录（环境侧，含真值）。"""

    seq: int
    kw: str
    title: str
    content: str
    status: str
    reason: str | None
    published: bool
    post_id: str | None
    attempt_id: str | None = None
    injected: bool = False          # 是否由故障注入产生（区别于站点硬规则拒绝）

    def to_truth(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kw": self.kw,
            "title": self.title,
            "content": self.content,
            "status": self.status,
            "reason": self.reason,
            "published": self.published,
            "post_id": self.post_id,
            "attempt_id": self.attempt_id,
            "injected": self.injected,
        }


@dataclass
class PublishResult:
    """env.publish 的返回：观测 + 真值分离。

    observation 是"工具会返回给模型的 JSON"，故意不含 attempt_id 和真值；
    published / post_id 是真值，只供验证器使用，绝不进 observation。
    """

    observation: dict[str, Any]
    status: str
    reason: str | None = None
    published: bool = False
    post_id: str | None = None
    fault: FaultSpec | None = None

    @property
    def is_error(self) -> bool:
        return self.status == STATUS_ERROR


def empty_observation() -> dict[str, Any]:
    return {}
