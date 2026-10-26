"""TiebaAdapter —— 把 mock / real 两个后端归一到同一套观测。

为什么要这一层（PLAN.md 7.2）：同一条轨迹脚本分别跑模拟后端和真实后端（预览模式），
逐字节比对模型可见的观测字符串必须完全一致。这是迁移性的前提。
所以模型看到的 JSON 只在这里拼装一次，序列化参数也只有一份（dumps_observation）。

职责边界：
  - adapter 负责：参数校验、attempt_id 生成（客户端生成，只进本地日志）、
    观测字符串拼装、本地日志；
  - backend 负责：真正把动作执行掉，返回"工具原始返回"（不含 attempt_id）。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from .env import MockTiebaEnv
from .tools import TOOL_NAMES
from .types import (
    ERROR_REASONS,
    REJECTED_REASONS,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    UNKNOWN_REASONS,
)


class ModelToolCallError(RuntimeError):
    """模型侧的工具调用错误（工具名不存在、参数缺失或类型不可用）。

    刻意与"工具侧不可用"（login_expired / selector_missing）区分开：
    前者算 FAILED_MODEL，后者算 FAILED_TOOL。
    """


class BackendContractError(RuntimeError):
    """后端返回不符合四状态契约，属于工程问题，必须立刻暴露而不是当成模型错误。"""


def dumps_observation(obj: Any) -> str:
    """模型可见观测的唯一序列化入口。

    两个后端都经过这里，才能保证逐字节一致：不加空格、不转义中文、键序固定
    （键序由拼装时的插入顺序保证，调用方不要用 dict 推导式重排）。
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


# --------------------------------------------------------------------------- 后端


class Backend(Protocol):
    """后端的唯一契约：三个动作，返回扁平 dict。publish 的返回**不含** attempt_id。"""

    def get_candidates(self) -> dict[str, Any]: ...

    def list_recent_posts(self, kw: str | None, limit: int | None) -> dict[str, Any]: ...

    def publish(self, kw: str, title: str, content: str) -> dict[str, Any]: ...


@dataclass
class MockBackend:
    """内存模拟后端。env 的 observation 就是工具原始返回。"""

    env: MockTiebaEnv

    def get_candidates(self) -> dict[str, Any]:
        return self.env.get_candidates()

    def list_recent_posts(self, kw: str | None, limit: int | None) -> dict[str, Any]:
        return self.env.list_recent_posts(kw, limit)

    def publish(self, kw: str, title: str, content: str) -> dict[str, Any]:
        # 注意：不传 attempt_id，模拟"服务端拿不到客户端 ID"这一点
        return self.env.publish(kw, title, content).observation


Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass
class RealBackend:
    """真实后端：transport 把 (工具名, 参数) 映射到 browser_poster.py 的一次调用。

    transport 可注入，便于在无浏览器环境下做同构性测试（见 tests）。
    D1 需要把这里接到实际命令上；返回 JSON 的字段必须与 mock 完全一致。
    """

    transport: Transport

    def get_candidates(self) -> dict[str, Any]:
        return self.transport("get_candidates", {})

    def list_recent_posts(self, kw: str | None, limit: int | None) -> dict[str, Any]:
        args: dict[str, Any] = {"kw": kw}
        if limit is not None:
            args["limit"] = limit
        return self.transport("list_recent_posts", args)

    def publish(self, kw: str, title: str, content: str) -> dict[str, Any]:
        return self.transport("publish", {"kw": kw, "title": title, "content": content})


# 真实命令映射与 stdout 解析不在这里，见 realcli.py。
# adapter 保持为两侧共用的同构基准：它只认四状态，不认任何具体脚本的 CLI。


def derive_status_from_evidence(
    submit_response: dict[str, Any] | None = None,
    list_probe: dict[str, Any] | None = None,
    page_hint: str | None = None,
) -> dict[str, Any]:
    """复现现有工具的三级判据，三级都不确定才记 unknown。

    1) 抓到了发帖接口的响应 → 能拿到 tid 就是 ok；
    2) 抓不到就回查吧页列表，标题/正文出现在列表里 → ok；
    3) 最后才看页面提示语：提示成功但拿不到帖子 ID → unknown(no_response)，
       命中验证码 → unknown(captcha)，其余 → unknown(submit_timeout)。
    """
    if submit_response:
        tid = submit_response.get("post_id") or submit_response.get("tid")
        if submit_response.get("ok") and tid:
            return {"status": STATUS_OK, "post_id": str(tid)}
        reason = submit_response.get("reason")
        if reason in REJECTED_REASONS:
            return {"status": STATUS_REJECTED, "reason": reason}
        if reason in ERROR_REASONS:
            return {"status": STATUS_ERROR, "reason": reason}

    if list_probe:
        for item in list_probe.get("posts", []):
            if item.get("matched"):
                tid = item.get("post_id") or item.get("tid")
                # 列表能确认"发出去了"，但可能拿不到 tid：post_id 允许为 None
                return {"status": STATUS_OK, "post_id": str(tid) if tid else None}

    hint = (page_hint or "").lower()
    if "captcha" in hint or "验证码" in hint:
        return {"status": STATUS_UNKNOWN, "reason": "captcha"}
    if hint:
        # 页面说成功也拿不到可信 ID，只能记 no_response，不能凭空造 post_id
        return {"status": STATUS_UNKNOWN, "reason": "no_response"}
    return {"status": STATUS_UNKNOWN, "reason": "submit_timeout"}


# --------------------------------------------------------------------------- 适配层


@dataclass
class TiebaAdapter:
    """模型 ↔ 后端之间的唯一通道。

    call() 返回 (result, observation)：
      result       结构化 dict，用于轨迹记录与 ShareGPT 导出
      observation  模型真正读到的字符串，由 dumps_observation 生成
    """

    backend: Backend
    attempt_id_factory: Callable[[], str] | None = None
    local_log: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    def __post_init__(self) -> None:
        if self.attempt_id_factory is None:
            self.attempt_id_factory = lambda: f"att-{uuid.uuid4().hex[:12]}"

    # -- 生命周期 ---------------------------------------------------------

    def reset_log(self) -> None:
        """每条轨迹开始前清空本地日志（attempt_id 只存在这里和模型可见的 ok/unknown 里）。"""
        self.local_log.clear()
        self._seq = 0

    # -- 唯一入口 ---------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
        args = self._normalize_arguments(name, arguments or {})
        self._seq += 1

        if name == "get_candidates":
            result = self._normalize_candidates(self.backend.get_candidates())
            attempt_id = None
        elif name == "list_recent_posts":
            result = self._normalize_posts(
                self.backend.list_recent_posts(args.get("kw"), args.get("limit"))
            )
            attempt_id = None
        elif name == "publish":
            attempt_id = self.attempt_id_factory()
            raw = self.backend.publish(args["kw"], args["title"], args["content"])
            result = merge_publish(raw, attempt_id)
        else:  # pragma: no cover - 已由 _normalize_arguments 拦住
            raise ModelToolCallError(f"未知工具 {name}")

        observation = dumps_observation(result)
        self.local_log.append(
            {
                "seq": self._seq,
                "tool": name,
                "arguments": args,
                "attempt_id": attempt_id,
                "result": result,
                "observation": observation,
                "ts": time.time(),
            }
        )
        return result, observation

    def available_tools(self) -> Sequence[str]:
        return TOOL_NAMES

    # -- 参数校验（模型错误在这里暴露，不当成工具错误） ---------------------

    def _normalize_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in TOOL_NAMES:
            raise ModelToolCallError(f"未知工具 {name!r}，可用工具：{TOOL_NAMES}")
        if not isinstance(arguments, dict):
            raise ModelToolCallError("arguments 必须是 JSON 对象")

        if name == "get_candidates":
            return {}

        if name == "list_recent_posts":
            kw = arguments.get("kw")
            if not isinstance(kw, str) or not kw.strip():
                raise ModelToolCallError("list_recent_posts 缺少非空参数 kw")
            limit = arguments.get("limit")
            if limit is not None:
                try:
                    limit = int(limit)
                except (TypeError, ValueError) as exc:
                    raise ModelToolCallError("limit 必须是整数") from exc
            return {"kw": kw.strip(), "limit": limit}

        # publish
        out: dict[str, Any] = {}
        for key in ("kw", "title", "content"):
            value = arguments.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ModelToolCallError(f"publish 缺少非空参数 {key}")
            out[key] = value.strip() if key != "content" else value
        return out

    # -- 观测归一 ---------------------------------------------------------

    def _normalize_candidates(self, raw: dict[str, Any]) -> dict[str, Any]:
        items = raw.get("candidates")
        if not isinstance(items, list):
            raise BackendContractError(f"get_candidates 返回缺少 candidates 数组: {raw!r}")
        return {"candidates": [str(x) for x in items]}

    def _normalize_posts(self, raw: dict[str, Any]) -> dict[str, Any]:
        items = raw.get("posts")
        if not isinstance(items, list):
            raise BackendContractError(f"list_recent_posts 返回缺少 posts 数组: {raw!r}")
        posts = []
        for item in items:
            if not isinstance(item, dict):
                raise BackendContractError(f"posts 元素必须是对象: {item!r}")
            # 键序由 adapter 统一规定（title → content → post_id），
            # 这样后端返回的 dict 顺序不同也能逐字节一致。
            # 真实吧页没有正文，只有 title —— 缺 content 时不补空串，保持两侧字段一致。
            entry: dict[str, Any] = {"title": item.get("title", "")}
            if "content" in item:
                entry["content"] = item.get("content", "")
            if "post_id" in item:
                entry["post_id"] = item["post_id"]
            posts.append(entry)
        return {"posts": posts}

# 四状态契约的唯一拼装点。adapter 和 publish_api 都走这里，
# 保证「模型可见观测」与「直调脚本的返回值」是同一份格式，不是两套。
#
# attempt_id 只出现在 ok / unknown 上：这两种状态下帖子可能已经发出去了，
# 需要客户端 ID 去对账；rejected / error 明确知道没发出去，不需要对账，故不带。
# 与 PLAN 2.1 的原始契约一致。
def merge_publish(raw: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    """四状态契约的唯一拼装点。键序 = 模型看到的键序。"""
    status = raw.get("status")
    if status == STATUS_OK:
        # post_id 允许为 None：真实工具会出现"发帖弹层关了、判定成功、但拿不到链接"。
        # 模拟器用 FaultSpec(withhold_post_id=True) 表达同一形态，训练时模型必须见过它。
        # 「ok 必须有 post_id」是 publish_api 对外交付的约束，在那一层补（回查或降级），
        # 不在这里强行 raise —— 否则 mock 就无法表达这个真实状态。
        return {"status": STATUS_OK, "post_id": raw.get("post_id"), "attempt_id": attempt_id}
    if status == STATUS_UNKNOWN:
        reason = raw.get("reason")
        if reason not in UNKNOWN_REASONS:
            raise BackendContractError(f"unknown 的 reason 不合法: {reason!r}")
        # unknown 刻意不带 post_id：真实工具这一刻确实不知道
        return {"status": STATUS_UNKNOWN, "reason": reason, "attempt_id": attempt_id}
    if status == STATUS_REJECTED:
        reason = raw.get("reason")
        if reason not in REJECTED_REASONS:
            raise BackendContractError(f"rejected 的 reason 不合法: {reason!r}")
        # rejected / error 明确知道没发出去，不需要客户端 ID 去对账 —— 不带 attempt_id。
        return {"status": STATUS_REJECTED, "reason": reason}
    if status == STATUS_ERROR:
        reason = raw.get("reason")
        if reason not in ERROR_REASONS:
            raise BackendContractError(f"error 的 reason 不合法: {reason!r}")
        return {"status": STATUS_ERROR, "reason": reason}
    raise BackendContractError(f"publish 返回了未知 status: {status!r}（{raw!r}）")
