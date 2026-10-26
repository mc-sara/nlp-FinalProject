"""直接调用真实发帖脚本的内联接口：`publish(kw, title, content)`。

为什么能内联传参：`browser_poster.py` 的 CLI 只吃帖子文件，
但它内部真正干活的是 `publish_one(e, kw, title, content)` —— 三个字符串参数。
这个模块直接调那个函数，所以标题和正文就是你传进来的东西，不经过任何
临时 .md 文件、不做 front matter 序列化，改参数立刻改发出去的内容。

返回的就是四状态 JSON，与模型在模拟环境里看到的逐字节同构：

    {"status": "ok",       "post_id": "...", "attempt_id": "..."}
    {"status": "unknown",  "reason": "submit_timeout|captcha|no_response", "attempt_id": "..."}
    {"status": "rejected", "reason": "title_too_long|duplicate|level_limit"}
    {"status": "error",    "reason": "login_expired|selector_missing"}

    from publish_api import publish

    r = publish("我的吧", "今天的热梗", "正文第一段\n正文第二段")
    if r["status"] == "ok":
        print(r["post_id"], r["attempt_id"])

    # 批量发（浏览器只启动一次，省掉每条约 5-8 秒的启动开销）
    with Poster() as poster:
        for item in items:
            print(poster.publish("我的吧", item.title, item.content))

两个只读查询也能直接调（都不开浏览器）：

    from publish_api import list_recent_posts, get_candidates

    list_recent_posts("我的吧", 20)   # [{"post_id","title","pinned"}, ...] 查证/查重同源
    get_candidates()                  # [{"id","title","content","split","tags"}, ...] 候选热梗

命令行两种发布方式：

    # 1) 自己给内容
    python publish_api.py --kw agenttest --title "我的标题" --content-file body.txt --yes
    # 2) 用候选热梗（标题取候选里的 title，缺了才从正文首段推）
    python publish_api.py --kw agenttest --candidate 2 --yes
    python publish_api.py --list-candidates          # 先看有哪些
    python publish_api.py --list-posts agenttest     # 只读查吧页

每次运行结束都会落一条记录到 `runs/publish/publish-<时间戳>-<随机>.json`，
里面含 cookie 原文、kw、标题（实际发出的与原始的）、正文全文、最终状态、
结局分类（一次成功 / 先 unknown 后成功 / 重发用尽 / 直接 rejected / 直接 error）
以及每一轮的发送与查证结果。成功也写，成功记录用于对账。

四个字段规则：
  · `ok` / `unknown` 带 `attempt_id` —— 这两种状态下帖子**可能已发出**，
    需要用客户端 ID 去对账。
  · `rejected` / `error` **不带** `attempt_id` —— 明确知道没发出去，不需要对账。
    （`detailed=True` 时本地审计对象里仍有该值，那是本地日志，不进 JSON。）
  · `ok` 的 `post_id` **不会为空**。脚本有时只能靠 UI 信号判成功
    （弹层关了），拿不到链接；这时会自动回查吧页按标题补上，
    补不到就**降级成 unknown** —— 宁可不认成功，也不给一个空的帖子 ID。
  · `unknown` 没有 `post_id`，也没有编造的 ID。
  · 拼装走 `adapter.merge_publish`，与模拟器是同一份代码，不是两套规则。

查证与重发（默认开启，最多重发 3 次，**只有 unknown 才会重发**）：

    发送 ──ok──► 结束
      │
      ├─ rejected / error ──► 记录原因 ──► 结束（确定性结论，不重发）
      │
      └─ unknown ──► 查证（立刻）
                       ├─ 命中 ──► 改判 ok，结束
                       └─ 未命中 ──► 等 3s 再查证
                                       ├─ 命中 ──► 改判 ok，结束
                                       └─ 未命中 ──► 记录查证结果 ──► 重发

  `payload` 永远是标准四状态 JSON（与模拟器观测同构，不掺审计字段）；
  每一轮的「发送结果 + 查证证据」放在 `result.rounds` 里，`detailed=True` 可取。
  `Poster.publish` 仍是**单次发送**的底层能力（测试与同构检查依赖它），
  带查证的入口是 `Poster.publish_verified` / 模块级 `publish()`。

两条从源码读出来的硬限制（不是我猜的）：
  · 标题 > 31 字 → `publish_one` 内部静默截断。我在调用前先截，并可通过
    `detailed=True` 取到 `title_sent` —— 查证要靠标题比对，必须知道实际发的是哪 31 个字。
  · 正文 > 2000 字 → 同样截断，`content_chars` 返回实际发送字数。

浏览器与 Cookie：Edge 每次启动约 5 秒，`Poster` 会复用它。
Cookie 复用 `real/config.json` 里指向的 `config.json`，与 CLI 完全同一份。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tieba_env.adapter import dumps_observation, merge_publish  # noqa: E402
from tieba_env.realcli import classify_verdict  # noqa: E402
from tieba_env.types import (  # noqa: E402
    ERROR_REASONS,
    REJECTED_REASONS,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    UNKNOWN_REASONS,
)

DEFAULT_POSTER_DIR = Path(r"C:/Users/15547/WorkBuddy/2026-10-22-17-21-40/tieba-poster")
REAL_CONFIG = ROOT / "real" / "config.json"
CANDIDATES_PATH = ROOT / "tieba_env" / "data" / "candidates.json"

# 与 browser_poster.publish_one 内部一致的硬上限
TITLE_LIMIT = 31
CONTENT_LIMIT = 2000

# 查证与重发
MAX_RETRIES = 3
"""最多重发次数（不含首次发送）。只有 unknown 才会触发重发。"""
VERIFY_DELAY = 3.0
"""unknown 后第二次查证前等待的秒数。"""
LEDGER_DIR = ROOT / "runs" / "publish"
"""逐次发送/查证记录的落盘目录。"""

_REASONS: dict[str, tuple[str, ...]] = {
    STATUS_UNKNOWN: UNKNOWN_REASONS,
    STATUS_REJECTED: REJECTED_REASONS,
    STATUS_ERROR: ERROR_REASONS,
}

# 规则表给不出合法 reason 时的兜底，保证绝不因为"映射失败"把已发生的结果丢掉。
_FALLBACK_REASON = {
    STATUS_UNKNOWN: "no_response",
    STATUS_REJECTED: "title_too_long",
    STATUS_ERROR: "selector_missing",
}


class PosterError(RuntimeError):
    """环境没准备好（脚本目录不对、没 Cookie、浏览器起不来）。"""


def _new_attempt_id() -> str:
    """客户端生成的尝试 ID，格式与 TiebaAdapter 一致。"""
    return f"att-{uuid.uuid4().hex[:12]}"


def _norm_title(text: str) -> str:
    """标题比对用：去掉所有空白。吧页会把长标题折行、吞掉多余空格。"""
    return re.sub(r"\s+", "", text or "")


def _raw_status(ok: bool, detail: str, *, crashed: bool = False) -> dict[str, Any]:
    """把 `publish_one` 的 `(ok, msg)` 归一成 merge_publish 认识的原始四状态。

    crashed=True 指浏览器/CDP 层直接抛异常 —— 工具侧不可用，按 PLAN 归
    `error/selector_missing`，不能冒充成「发帖失败」或「不确定」。
    """
    if crashed:
        return {"status": STATUS_ERROR, "reason": "selector_missing"}

    verdict = classify_verdict(ok, detail or "")
    status = verdict["status"]
    if status == STATUS_OK:
        # 拿不到 tid 时 post_id 为 None，保持 null，不编造
        return {"status": STATUS_OK, "post_id": verdict.get("post_id")}

    reason = verdict.get("reason")
    allowed = _REASONS.get(status, ())
    if reason not in allowed:
        reason = _FALLBACK_REASON.get(status, "no_response")
    return {"status": status, "reason": reason}


@dataclass
class PublishResult:
    """`detailed=True` 时的审计对象：payload 之外还留着"实际发了什么"。"""

    payload: dict[str, Any]
    attempt_id: str
    detail: str = ""
    kw: str = ""
    title_sent: str = ""
    title_truncated: bool = False
    content_chars: int = 0
    content_truncated: bool = False
    elapsed: float = 0.0
    login_user: str | None = None
    post_id_source: str | None = None
    """post_id 从哪来：script / recheck / verify / None。"""
    rounds: list[dict[str, Any]] = field(default_factory=list)
    """`publish_verified` 的逐次记录：每一轮发送结果 + 查证证据。单次发送时为空。"""
    fenced_payload: dict[str, Any] = field(default_factory=dict)

    # -- payload 的便捷视图（只读） ---------------------------------------

    @property
    def ok(self) -> bool:
        return self.payload["status"] == STATUS_OK

    @property
    def status(self) -> str:
        return self.payload["status"]

    @property
    def reason(self) -> str | None:
        return self.payload.get("reason")

    @property
    def post_id(self) -> str | None:
        return self.payload.get("post_id")

    @property
    def post_url(self) -> str | None:
        return f"https://tieba.baidu.com/p/{self.post_id}" if self.post_id else None

    @property
    def json(self) -> str:
        """模型可见的那串字符（紧凑、无空格、中文不转义）。"""
        return dumps_observation(self.payload)

    def __str__(self) -> str:
        text = self.json
        if self.post_url:
            src = {
                "script": "脚本给出",
                "recheck": "吧页回查补全",
                "verify": "查证命中改判",
            }.get(self.post_id_source or "", "")
            text += f"\n{self.post_url}" + (f"（{src}）" if src else "")
        if self.detail and self.status != STATUS_OK:
            text += f"\n原始文案：{self.detail}"
        notes = []
        if self.title_truncated:
            notes.append(f"标题被截到 {TITLE_LIMIT} 字：{self.title_sent}")
        if self.content_truncated:
            notes.append(f"正文被截到 {self.content_chars} 字")
        if notes:
            text += "\n！" + "；".join(notes)
        return text


def _promote_to_ok(result: PublishResult, post_id: str) -> PublishResult:
    """查证在吧页上看到这篇 → 改判 ok，并沿用该轮的 attempt_id 便于对账。

    这不是"猜成功"：吧页里确实有这条帖子，比脚本的 UI 信号更硬。
    """
    payload = merge_publish({"status": STATUS_OK, "post_id": post_id}, result.attempt_id)
    return replace(result, payload=payload, post_id_source="verify")


def _load_defaults() -> dict[str, Any]:
    """默认值优先取 real/config.json，保证与 CLI 侧配置一致。"""
    data: dict[str, Any] = {}
    if REAL_CONFIG.exists():
        try:
            data = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PosterError(f"real/config.json 不是合法 JSON: {exc}") from exc
    return {
        "poster_dir": Path(data.get("poster_dir") or DEFAULT_POSTER_DIR),
        "config_name": data.get("tieba_config") or "config.json",
        "headless": bool(data.get("headless", False)),
        "browser": None,
        "profile": None,
    }


def _venv_python(poster_dir: Path) -> Path:
    """发帖脚本自带的解释器（依赖装在这里）。"""
    return Path(poster_dir).resolve() / ".venv" / "Scripts" / "python.exe"


def _import_poster(poster_dir: Path) -> tuple[Any, Any]:
    """导入 browser_poster / tieba_poster。两者互相 import，必须在同一目录下。"""
    directory = Path(poster_dir).resolve()
    if not (directory / "browser_poster.py").exists():
        raise PosterError(f"找不到发帖脚本: {directory / 'browser_poster.py'}")
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    venv_py = _venv_python(directory)
    hint = f"请用发帖脚本自带的解释器运行：\n    \"{venv_py}\" {Path(__file__).name} ..."
    try:
        import browser_poster as bp  # noqa: PLC0415 - 需要在 sys.path 就位后再导入
        import tieba_poster as tp  # noqa: PLC0415
    except SystemExit as exc:
        # browser_poster 顶层缺依赖时是 print + sys.exit()，不是抛 ImportError。
        # 不接住的话调用方看到的是"进程莫名退出"，比报错还难查。
        raise PosterError(
            f"发帖脚本在导入阶段中止（缺依赖，退出码 {exc.code}）。{hint}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 缺依赖要如实说，不吞掉
        raise PosterError(f"导入发帖脚本失败（{type(exc).__name__}: {exc}）。{hint}") from exc
    return bp, tp


class Poster:
    """持有浏览器，可连续发多条。用 with 语句保证浏览器被关掉。"""

    def __init__(
        self,
        *,
        poster_dir: str | Path | None = None,
        config: str | Path | None = None,
        headless: bool | None = None,
        browser: str | None = None,
        profile: str | None = None,
        keep_open: bool = False,
        title_limit: int = TITLE_LIMIT,
        recheck_polls: int = 3,
        recheck_interval: float = 1.5,
        verify_polls: int = 1,
        verify_interval: float = 1.5,
    ) -> None:
        defaults = _load_defaults()
        self.poster_dir = Path(poster_dir or defaults["poster_dir"])
        self.config_path = Path(config) if config else self.poster_dir / defaults["config_name"]
        self.headless = defaults["headless"] if headless is None else headless
        self.browser = browser or defaults["browser"]
        self.profile = profile or defaults["profile"]
        self.keep_open = keep_open
        self.title_limit = title_limit
        # 发帖脚本只给出 UI 信号、拿不到链接时，按标题回查吧页的次数与间隔
        self.recheck_polls = recheck_polls
        self.recheck_interval = recheck_interval
        # 重发流程里的查证：默认等够时间后只问一次吧页（3 秒是刻意留的可见延迟）
        self.verify_polls = verify_polls
        self.verify_interval = verify_interval
        self._bp: Any = None
        self._tp: Any = None
        self._edge: Any = None
        self._probe: tuple[Any, Any] | None = None
        self._recheck_error: str | None = None
        self.login_user: str | None = None
        # 发帖用的那串 cookie 与注入条数，落盘记录要用（见 _write_ledger）
        self.cookie: str | None = None
        self.cookie_count: int = 0

    # ------------------------------------------------------------------ 生命周期

    def __enter__(self) -> "Poster":
        self.open()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def open(self) -> "Poster":
        if self._edge is not None:
            return self
        self._bp, self._tp = _import_poster(self.poster_dir)

        if not self.config_path.exists():
            raise PosterError(f"找不到贴吧 config: {self.config_path}")
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        cookie = (raw.get("cookie") or "").strip()
        if not cookie:
            raise PosterError(
                f"{self.config_path} 里没有 cookie。先在 {self.poster_dir} 下跑 setup_cookie.py"
            )

        try:
            self._edge = self._bp.Edge(
                headless=self.headless,
                profile_dir=self.profile,
                browser=self.browser,
            )
        except Exception as exc:  # noqa: BLE001
            raise PosterError(f"启动浏览器失败: {type(exc).__name__}: {exc}") from exc

        count = self._edge.set_cookies(self._tp.parse_cookie(cookie))
        info = self._bp.check_login(self._edge)
        if str(info.get("no")) != "0":
            self.close()
            raise PosterError(f"登录态无效（注入 {count} 条 Cookie）: {info}")
        self.login_user = info.get("user")
        self.cookie = cookie
        self.cookie_count = count
        return self

    def close(self) -> None:
        if self._edge is not None:
            try:
                self._edge.close(keep=self.keep_open)
            except Exception:  # noqa: BLE001 - 关不掉不该盖住真正要报的错
                pass
            finally:
                self._edge = None

    # ------------------------------------------------------------------ 回查

    def _list_posts(self, kw: str, limit: int = 20) -> list[dict[str, Any]]:
        """读吧页列表。走 HTTP client（复用 cookie/代理），不需要浏览器。"""
        if self._probe is None:
            from real.real_probe import build_client, do_list  # noqa: PLC0415

            tp, client = build_client(self.poster_dir, str(self.config_path))
            self._probe = (do_list, tp, client)
        do_list, tp, client = self._probe
        return list(do_list(tp, client, kw, limit).get("posts") or [])

    def list_recent_posts(self, kw: str, limit: int = 20) -> list[dict[str, Any]]:
        """读吧页最近帖子（只读，不启动浏览器）。

        查证、发帖前查重用的都是这条路：抓 `/mo/q/m` 服务端渲染的吧页，
        只认带 `data-tid` 的 `<li>`，返回 `[{"post_id","title","pinned"}, ...]`。
        走 `requests`，不是浏览器 —— 所以比 `open()` 快得多（不需要 5 秒启动）。
        """
        kw = (kw or "").strip()
        if not kw:
            raise ValueError("kw（吧名）不能为空")
        if limit <= 0:
            raise ValueError("limit 必须为正整数")
        return self._list_posts(kw, limit)

    def _recheck_post_id(self, kw: str, title: str) -> str | None:
        """发帖成功后拿不到 tid 时，按标题回查吧页把它补上。

        只能按标题比对 —— 吧页列表没有正文。标题足够独特时可靠；
        同一个标题发过两次就分不清，取页面里第一条命中的。
        风控/网络异常直接放弃（不重试），别把账号推进冷却。
        """
        target = _norm_title(title)
        polls = max(1, self.recheck_polls)
        last_error: Exception | None = None
        for index in range(polls):
            try:
                posts = self._list_posts(kw)
            except Exception as exc:  # noqa: BLE001 - 回查失败不该让整次发帖崩掉
                last_error = exc
                break
            for post in posts:
                if _norm_title(str(post.get("title", ""))) == target:
                    post_id = str(post.get("post_id") or "").strip()
                    if post_id:
                        return post_id
            if index + 1 < polls:
                time.sleep(self.recheck_interval)
        self._recheck_error = f"{type(last_error).__name__}: {last_error}" if last_error else None
        return None

    # ------------------------------------------------------------------ 发帖

    def publish(
        self,
        kw: str,
        title: str,
        content: str,
        *,
        max_len: int = CONTENT_LIMIT,
        detailed: bool = False,
        recheck: bool = True,
    ) -> dict[str, Any] | PublishResult:
        """发一条主题帖，返回四状态 JSON（`detailed=True` 时返回审计对象）。

        参数非法 / 环境没准备好会抛异常；**发出去了但结果不确定**不抛异常，
        而是返回 `{"status": "unknown", ...}` —— 那是有信息量的结果，不是错误。

        保证：`status == "ok"` 时 `post_id` 一定非空。脚本只给了 UI 信号
        （拿不到链接）时，会按标题回查吧页补上；补不到就降级成 `unknown`。
        `recheck=False` 可关掉回查（省 1–5 秒），但那样拿不到链接的情况直接降级。
        """
        kw = (kw or "").strip()
        title = (title or "").strip()
        content = (content or "").strip()
        if not kw:
            raise ValueError("kw（吧名）不能为空")
        if not title:
            raise ValueError("title（标题）不能为空")
        if not content:
            raise ValueError("content（正文）不能为空")

        if self._edge is None:
            self.open()

        title_truncated = len(title) > self.title_limit
        content_truncated = len(content) > max_len
        sent_title = title[: self.title_limit]
        sent_content = content[:max_len]

        attempt_id = _new_attempt_id()
        started = time.monotonic()
        crashed = False
        try:
            ok, detail = self._bp.publish_one(
                self._edge, kw, sent_title, sent_content, max_len=max_len
            )
        except Exception as exc:  # noqa: BLE001 - 浏览器层崩溃按工具侧不可用记
            ok, detail, crashed = False, f"{type(exc).__name__}: {exc}", True

        raw = _raw_status(ok, detail, crashed=crashed)
        post_id_source = "script" if raw.get("post_id") else None

        # ok 必须能对上一条帖子。脚本只凭 UI 信号判成功时会没有链接，这里补。
        if raw["status"] == STATUS_OK and not raw.get("post_id"):
            self._recheck_error = None
            resolved = self._recheck_post_id(kw, sent_title) if recheck else None
            if resolved:
                raw = {"status": STATUS_OK, "post_id": resolved}
                post_id_source = "recheck"
            else:
                # 找不到就不认成功：可能还在审核，也可能根本没发出去。
                # 这是唯一诚实的处理 —— 空的 post_id 会让下游对账直接错位。
                why = "已关闭回查" if not recheck else (
                    f"回查 {self.recheck_polls} 次未见同标题"
                    + (f"（{self._recheck_error}）" if self._recheck_error else "")
                )
                raw = {"status": STATUS_UNKNOWN, "reason": "no_response"}
                post_id_source = None
                detail = f"{detail}｜{why}，降级 unknown"

        payload = merge_publish(raw, attempt_id)
        result = PublishResult(
            payload=payload,
            attempt_id=attempt_id,
            detail=detail or "",
            kw=kw,
            title_sent=sent_title,
            title_truncated=title_truncated,
            content_chars=len(sent_content),
            content_truncated=content_truncated,
            elapsed=round(time.monotonic() - started, 2),
            login_user=self.login_user,
            post_id_source=post_id_source,
        )
        return result if detailed else payload

    # -------------------------------------------------------------- 查证 + 重发

    def verify_post(
        self,
        kw: str,
        title: str,
        *,
        delay: float = 0.0,
        polls: int | None = None,
        interval: float | None = None,
    ) -> dict[str, Any]:
        """查证「吧页里到底有没有这篇」，返回**证据**而不是布尔值。

        与 `_recheck_post_id` 的分工：那个只负责补 post_id，补不到就静默；
        这个要把「查了几次、看到几条、有没有报错」写进重发记录，所以返回字典。
        永不抛异常 —— 查证本身失败是有信息量的结果，不该让整次发帖崩掉。
        """
        started = time.monotonic()
        if delay > 0:
            time.sleep(delay)

        target = _norm_title(title)
        times = max(1, self.verify_polls if polls is None else polls)
        gap = self.verify_interval if interval is None else interval

        listed = 0
        used = 0
        matched: dict[str, Any] | None = None
        error: str | None = None

        for index in range(times):
            try:
                posts = self._list_posts(kw)
            except Exception as exc:  # noqa: BLE001 - 查证失败只记证据
                error = f"{type(exc).__name__}: {exc}"
                break
            used += 1
            listed = len(posts)
            for post in posts:
                if _norm_title(str(post.get("title", ""))) == target:
                    post_id = str(post.get("post_id") or "").strip()
                    if post_id:
                        matched = {
                            "post_id": post_id,
                            "title": post.get("title"),
                            "author": post.get("author"),
                        }
                        break
            if matched:
                break
            if index + 1 < times:
                time.sleep(gap)

        return {
            "found": matched is not None,
            "post_id": (matched or {}).get("post_id"),
            "match": matched,
            "polls_used": used,
            "listed": listed,
            "delay": delay,
            "error": error,
            "elapsed": round(time.monotonic() - started, 2),
        }

    def publish_verified(
        self,
        kw: str,
        title: str,
        content: str,
        *,
        max_len: int = CONTENT_LIMIT,
        recheck: bool = True,
        max_retries: int = MAX_RETRIES,
        verify_delay: float = VERIFY_DELAY,
        retry_interval: float = 0.0,
    ) -> PublishResult:
        """发 → 查证 → 按需重发，最多重发 `max_retries` 次。

        `ok` 收工；`rejected` / `error` 是确定性结论，记录原因后**直接结束、不重发**；
        只有 `unknown` 才进入查证：查不到再等 `verify_delay` 秒查第二次，仍未命中才重发。
        查证命中即改判 ok（帖子确实在吧页上，只是脚本没拿到链接）。

        最终 `payload` 仍是标准四状态 JSON；每一轮的发送与查证全部留在 `rounds` 里。
        重发次数用尽仍非 ok 时，`rounds` 就是完整的失败档案。
        """
        rounds: list[dict[str, Any]] = []
        result: PublishResult | None = None
        attempts = max(0, max_retries) + 1

        for attempt_index in range(attempts):
            is_retry = attempt_index > 0
            if is_retry and retry_interval > 0:
                time.sleep(retry_interval)

            current = self.publish(
                kw, title, content, max_len=max_len, detailed=True, recheck=recheck
            )
            assert isinstance(current, PublishResult)
            result = current

            record: dict[str, Any] = {
                "round": attempt_index + 1,
                "is_retry": is_retry,
                "attempt_id": current.attempt_id,
                "status": current.status,
                "reason": current.reason,
                "post_id": current.post_id,
                "send_elapsed": current.elapsed,
                "detail": current.detail,
                "verifications": [],
                "conclusion": "",
            }
            rounds.append(record)

            if current.status == STATUS_OK:
                record["conclusion"] = "ok，结束"
                break

            if current.status in (STATUS_REJECTED, STATUS_ERROR):
                # 确定性结论：服务端明确拒绝 / 工具根本不可用，同样的内容再发一遍
                # 只会拿到同样的答复（duplicate 更是永远发不出去），重发纯属刷请求。
                record["conclusion"] = (
                    f"{current.status}（{current.reason}），明确没发出去，记录原因后结束，不重发"
                )
                break

            # 走到这里只剩 unknown —— 只有它才值得查证与重发
            first = self.verify_post(kw, current.title_sent)
            record["verifications"].append(first)
            if first["found"]:
                result = _promote_to_ok(current, str(first["post_id"]))
                record["conclusion"] = f"查证命中 {first['post_id']}，改判 ok"
                break

            # 帖子可能正在延迟可见，等一会儿再查一次
            second = self.verify_post(kw, current.title_sent, delay=verify_delay)
            record["verifications"].append(second)
            if second["found"]:
                result = _promote_to_ok(current, str(second["post_id"]))
                record["conclusion"] = (
                    f"等待 {verify_delay:g}s 后查证命中 {second['post_id']}，改判 ok"
                )
                break

            if attempt_index + 1 >= attempts:
                record["conclusion"] = f"查证未命中，重发次数已用尽（{max_retries} 次）"
                break
            record["conclusion"] = f"unknown（{current.reason}），查证未命中，重发"

        assert result is not None
        result.rounds = rounds
        return result


def publish(
    kw: str,
    title: str,
    content: str,
    *,
    headless: bool | None = None,
    poster_dir: str | Path | None = None,
    max_len: int = CONTENT_LIMIT,
    keep_open: bool = False,
    detailed: bool = False,
    recheck: bool = True,
    max_retries: int = MAX_RETRIES,
    verify_delay: float = VERIFY_DELAY,
    ledger_dir: str | Path | None = None,
) -> dict[str, Any] | PublishResult:
    """起浏览器 → 发 → 查证 → 按需重发（最多 `max_retries` 次）→ 关浏览器。

    返回最终那一轮的四状态 JSON；`detailed=True` 返回 `PublishResult`，
    用 `result.rounds` 取每一轮的发送与查证记录。`max_retries=0` 即退回单次发送。
    `ledger_dir` 给了就把逐次记录写成 JSON（默认不写，只在 CLI 里写）。
    """
    poster = Poster(poster_dir=poster_dir, headless=headless, keep_open=keep_open)
    try:
        result = poster.publish_verified(
            kw,
            title,
            content,
            max_len=max_len,
            recheck=recheck,
            max_retries=max_retries,
            verify_delay=verify_delay,
        )
        if ledger_dir is not None:
            _write_ledger(result, ledger_dir)
        return result if detailed else result.payload
    finally:
        poster.close()


# ---------------------------------------------------------------- 只读查询


def list_recent_posts(
    kw: str,
    limit: int = 20,
    *,
    poster_dir: str | Path | None = None,
    config: str | Path | None = None,
) -> list[dict[str, Any]]:
    """读吧页最近帖子，只读、不开浏览器、不需要 `with Poster()`。

        from publish_api import list_recent_posts

        for post in list_recent_posts("agenttest", 20):
            print(post["post_id"], post["title"])

    查证（`verify_post`）与发帖前查重用的就是这份数据，所以它天然可以当
    "我的帖子到底发出去没有"的权威依据。返回 `[{"post_id","title","pinned"}, ...]`。
    """
    poster = Poster(poster_dir=poster_dir, config=config)
    try:
        return poster.list_recent_posts(kw, limit)
    except (ImportError, SystemExit) as exc:
        # 读吧页复用发帖脚本的 requests。系统 Python 里没装时，tieba_poster 是
        # print + sys.exit()，不是 ImportError —— 两种都得接住，否则调用方看到的是
        # "进程莫名退出"，比报错还难查。
        detail = exc.code if isinstance(exc, SystemExit) else exc
        raise PosterError(
            f"读吧页缺少依赖（{detail}）。请用发帖脚本自带的解释器运行：\n"
            f"    \"{_venv_python(poster.poster_dir)}\" {Path(__file__).name} ..."
        ) from exc


def get_candidates(split: str | None = None) -> list[dict[str, Any]]:
    """取候选热梗，来自 `tieba_env/data/candidates.json`。

        from publish_api import get_candidates

        for item in get_candidates():
            print(item["id"], item["content"])

    每条是 `{"id","title","content","split","tags"}`；`split` 可取 train / test / real，
    传 `split="real"` 只拿发真帖用的那批。`content` 就是可以直接发出去的正文，
    `title` 是候选文件里写好的标题——没有才从正文推。
    """
    if not CANDIDATES_PATH.exists():
        raise PosterError(f"找不到候选文件：{CANDIDATES_PATH}")
    raw = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("candidates") or []
    items: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            # 候选文件里给了 title 就用它，别再从正文瞎猜——正文首句往往不是好标题。
            title = str(entry.get("title") or "").strip() or _title_from(content)
            items.append(
                {
                    "id": str(entry.get("id") or ""),
                    "title": title,
                    "content": content,
                    "split": str(entry.get("split") or ""),
                    "tags": list(entry.get("tags") or []),
                }
            )
        else:
            content = str(entry).strip()
            if content:
                items.append(
                    {
                        "id": "",
                        "title": _title_from(content),
                        "content": content,
                        "split": "",
                        "tags": [],
                    }
                )
    if split:
        items = [item for item in items if item["split"] == split]
    return items


def _candidate_ids(items: list[dict[str, Any]], limit: int = 8) -> str:
    """把候选 id 拼成一行提示，太长就截断。"""
    ids = [item["id"] for item in items if item["id"]]
    if not ids:
        return "（这批候选没有 id）"
    head = ", ".join(ids[:limit])
    if len(ids) > limit:
        head += f" …（共 {len(items)} 条，用 --list-candidates 看全部）"
    return head


def _pick_candidate(ref: int | str, split: str | None = None) -> dict[str, Any]:
    """取一条候选：`1`/`2`/… 按 1 基序号，`c01` 按 id，两种写法都认。

    纯数字一律当序号（id 不会长得像数字），其余按 id 精确匹配、忽略大小写。
    """
    items = get_candidates(split)
    if not items:
        raise PosterError(f"候选为空（split={split!r}）")

    text = str(ref).strip()
    if text.isdigit():
        index = int(text)
        if not 1 <= index <= len(items):
            raise PosterError(
                f"--candidate 越界：1..{len(items)}（收到 {index}）。现有 id：{_candidate_ids(items)}"
            )
        return items[index - 1]

    hit = [item for item in items if item["id"].lower() == text.lower()]
    if hit:
        return hit[0]
    raise PosterError(f"找不到候选 {text!r}。现有 id：{_candidate_ids(items)}")


def _title_from(content: str, limit: int = TITLE_LIMIT) -> str:
    """从正文推标题：按「：」或「，」切第一段。与 real_run.title_from 同规则。"""
    head = content.split("：")[0].split("，")[0].strip()
    if len(head) > limit:
        head = head[:limit]
    return head or "分享"


# ---------------------------------------------------------------------- 记录


def _format_rounds(rounds: list[dict[str, Any]]) -> str:
    """把逐次记录排成人能读的块。"""
    if not rounds:
        return ""
    lines = ["", f"发送 / 查证记录（共 {len(rounds)} 次提交）："]
    for record in rounds:
        head = f"  第 {record['round']} 次" + ("（重发）" if record["is_retry"] else "（首次）")
        head += f"  {record['status']}"
        if record.get("reason"):
            head += f"/{record['reason']}"
        if record.get("post_id"):
            head += f"  post_id={record['post_id']}"
        head += f"  发送耗时 {record['send_elapsed']}s"
        lines.append(head)
        for index, verify in enumerate(record.get("verifications") or [], 1):
            tag = "命中" if verify["found"] else "未命中"
            extra = f"  post_id={verify['post_id']}" if verify.get("post_id") else ""
            err = f"  错误={verify['error']}" if verify.get("error") else ""
            lines.append(
                f"      查证{index}：{tag}（查 {verify['polls_used']} 次，"
                f"看到 {verify['listed']} 条，等待 {verify['delay']:g}s，"
                f"耗时 {verify['elapsed']}s）{extra}{err}"
            )
        if record.get("conclusion"):
            lines.append(f"      → {record['conclusion']}")
    return "\n".join(lines)


def _classify_outcome(result: PublishResult) -> tuple[str, str]:
    """把一次运行归成一类结局，返回 (代号, 中文说明)。

    用户要能一眼分出这几种：一次就成功 / 先 unknown 后来成功 / 先 unknown 最后没成功 /
    第一次就被 rejected / 第一次就 error。
    """
    rounds = result.rounds or []
    submits = len(rounds)
    first = rounds[0] if rounds else {}
    first_status = str(first.get("status") or "")
    first_reason = str(first.get("reason") or "")
    retries = max(0, submits - 1)
    tail = f"{result.status}（{result.reason}）"

    if result.status == STATUS_OK:
        if submits == 1 and first_status == STATUS_OK:
            return "first_try_ok", "一次发送即成功"
        if submits == 1:
            # 脚本只给了 UI 信号、判 unknown，靠查吧页命中改判成功
            return (
                "first_try_verified",
                f"首次判为 unknown（{first_reason}），查吧页命中 {result.post_id} 后改判成功",
            )
        return (
            "retry_ok",
            f"首次 {first_status}（{first_reason}），重发 {retries} 次后成功"
            f"（post_id={result.post_id}）",
        )

    if result.status == STATUS_REJECTED:
        if submits == 1:
            return "rejected_first", f"第一次发送即被拒绝：{tail}"
        return "rejected_after_retry", f"首次 {first_status}，重发 {retries} 次后被拒绝：{tail}"

    if result.status == STATUS_ERROR:
        if submits == 1:
            return "error_first", f"第一次发送即报错：{tail}"
        return "error_after_retry", f"首次 {first_status}，重发 {retries} 次后报错：{tail}"

    # 只剩 unknown：重发用尽仍没确认
    return (
        "retry_failed",
        f"首次 unknown（{first_reason}），重发 {retries} 次后仍未确认：{tail}",
    )


def _cookie_keys(cookie: str | None) -> list[str]:
    """从 `a=1; b=2` 里取出键名，只用于核对注入了哪些字段。"""
    if not cookie:
        return []
    keys: list[str] = []
    for part in cookie.split(";"):
        name = part.split("=", 1)[0].strip()
        if name and name not in keys:
            keys.append(name)
    return keys


def _write_ledger(
    result: PublishResult,
    *,
    content: str = "",
    requested_title: str = "",
    cookie: str | None = None,
    cookie_count: int = 0,
    login_user: str | None = None,
    directory: str | Path | None = None,
) -> Path:
    """把这一次运行完整写成一个 JSON：cookie、吧名、标题、正文、结局、逐轮记录。

    成功也写 —— 失败记录能复盘，成功记录能对账（尤其是 post_id 靠回查补上的那种）。
    文件里含有 cookie 原文，等同账号凭据，别随意外发。
    """
    target = Path(directory or LEDGER_DIR)
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = target / f"publish-{stamp}-{uuid.uuid4().hex[:6]}.json"

    outcome, outcome_text = _classify_outcome(result)
    submits = len(result.rounds or [])

    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "outcome": outcome,
        "outcome_text": outcome_text,
        "succeeded": result.status == STATUS_OK,
        "first_try": outcome in ("first_try_ok", "first_try_verified"),
        "submit_count": submits,
        "retry_count": max(0, submits - 1),
        "kw": result.kw,
        "title": result.title_sent,
        "title_original": requested_title or result.title_sent,
        "title_truncated": result.title_truncated,
        "content": content,
        "content_chars": result.content_chars,
        "content_truncated": result.content_truncated,
        "status": result.status,
        "reason": result.reason,
        "post_id": result.post_id,
        "post_id_source": result.post_id_source,
        "attempt_id": result.attempt_id,
        "cookies": {
            "raw": cookie,
            "count": cookie_count,
            "keys": _cookie_keys(cookie),
        },
        "login_user": login_user or result.login_user,
        "rounds": result.rounds,
        "final": result.payload,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------- CLI


def _relaunch_in_venv() -> None:
    """CLI 专用：当前解释器不是发帖脚本的 .venv 时，换成它重跑自己。

    只在这里做、不在库调用里做 —— 库不能悄悄替换宿主进程。
    execv 失败就继续用当前解释器跑，后面会给出明确的缺依赖提示。
    """
    try:
        defaults = _load_defaults()
        venv_py = _venv_python(defaults["poster_dir"])
        if not venv_py.exists():
            return
        if Path(sys.executable).resolve() == venv_py.resolve():
            return
        os.execv(str(venv_py), [str(venv_py), str(Path(__file__).resolve()), *sys.argv[1:]])
    except OSError:
        return


def main(argv: list[str] | None = None) -> int:
    import argparse

    _relaunch_in_venv()

    ap = argparse.ArgumentParser(
        description=(
            "直接调用真实发帖脚本，输出四状态 JSON。两种发布方式：\n"
            "  1) 自己给内容：--kw --title --content / --content-file\n"
            "  2) 用候选热梗：--kw --candidate N 或 c01（标题用候选自带 title）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("kw_pos", nargs="?", help="吧名")
    ap.add_argument("title_pos", nargs="?", help="标题")
    ap.add_argument("content_pos", nargs="?", help="正文")
    ap.add_argument("--kw", dest="kw", help="吧名（也可用位置参数）")
    ap.add_argument("--title", dest="title", help="标题（不给就用候选自带 title，再没有就从正文首段生成）")
    ap.add_argument("--content", dest="content", help="正文")
    ap.add_argument("--content-file", help="正文从文件读（须 UTF-8，路径相对当前目录）")
    ap.add_argument(
        "--candidate",
        "--candidates",  # 复数写法常被误打，一并接住
        metavar="N|ID",
        help="用第 N 条（1 基序号）或候选 id（如 c01）作正文，两种写法等价",
    )
    ap.add_argument(
        "--split",
        choices=("train", "test", "real"),
        help="限定候选的 split（只能配 --candidate 用）",
    )
    ap.add_argument("--list-candidates", action="store_true", help="只列候选热梗，不发帖")
    ap.add_argument("--list-posts", metavar="KW", help="只读：列出该吧最近帖子，不发帖")
    ap.add_argument("--headless", action="store_true", help="无界面模式")
    ap.add_argument("--keep-open", action="store_true", help="发完不关浏览器")
    ap.add_argument("--login", action="store_true", help="只检查登录态，不发帖")
    ap.add_argument("--yes", action="store_true", help="跳过发帖前的确认")
    ap.add_argument(
        "--max-retries",
        type=int,
        default=MAX_RETRIES,
        help=f"最多重发次数（默认 {MAX_RETRIES}，0 等于只发一次）",
    )
    ap.add_argument(
        "--verify-delay",
        type=float,
        default=VERIFY_DELAY,
        help=f"unknown 后第二次查证前等待秒数（默认 {VERIFY_DELAY:g}）",
    )
    ap.add_argument("--no-retry", action="store_true", help="只发一次，不查证也不重发")
    args = ap.parse_args(argv)
    if args.no_retry:
        args.max_retries = 0

    # ---- 只读模式：先处理，免得为了看一眼候选白启动浏览器 ----
    if args.list_candidates:
        try:
            items = get_candidates(args.split)
        except PosterError as exc:
            ap.error(str(exc))
        if not items:
            print(f"候选为空（split={args.split!r}）")
            return 1
        for index, item in enumerate(items, 1):
            print(f"{index:>2}  {item['id']:<5} {item['split']:<6} {item['title']}")
            body = item["content"].replace("\n", " ")
            print(f"    {body[:52]}{'…' if len(body) > 52 else ''}")
        print(f"\n共 {len(items)} 条。发第 N 条：--kw 吧名 --candidate N"
              f"（也可以按 id：--candidate {items[0]['id'] or 'c01'}）")
        return 0

    if args.list_posts:
        try:
            posts = list_recent_posts(args.list_posts)
        except (PosterError, RuntimeError) as exc:
            print(f"读吧页失败：{exc}")
            return 1
        print(f"「{args.list_posts}」吧最近 {len(posts)} 条：")
        for post in posts:
            mark = "[置顶] " if post.get("pinned") else ""
            print(f"  {post['post_id']}  {mark}{post['title']}")
        return 0

    if args.split and args.candidate is None:
        ap.error("--split 只能配 --candidate 使用")

    # ---- 正文来源：三选一（--login 只体检，不需要正文）----
    kw = args.kw or args.kw_pos
    title = args.title or args.title_pos
    content = args.content or args.content_pos

    picked = sum(
        [
            bool(content),
            bool(args.content_file),
            args.candidate is not None,
        ]
    )
    if picked > 1:
        ap.error("--content / --content-file / --candidate 只能选一个")

    if not args.login:
        if args.candidate is not None:
            try:
                item = _pick_candidate(args.candidate, args.split)
            except PosterError as exc:
                ap.error(str(exc))
            content = item["content"]
            # 候选自带标题优先于自动生成：--title 显式给了才覆盖。
            title = title or item["title"]
            print(
                f"候选 {args.candidate} → {item['id'] or '-'}（{item['split'] or '-'}）"
                f"　标题：{item['title']}"
            )
        elif args.content_file:
            path = Path(args.content_file)
            # 相对路径基于当前工作目录，不是项目目录、也不是发帖脚本目录 —— 写错是常见事，
            # 与其抛 FileNotFoundError，不如把解析到的绝对路径直接印出来。
            if not path.exists():
                ap.error(f"正文文件不存在：{path.resolve()}（相对路径基于当前目录 {Path.cwd()}）")
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                ap.error(f"正文文件不是 UTF-8 编码：{path.resolve()}（{exc}）")
            print(f"正文来自文件：{path.resolve()}（{len(content)} 字）")
        elif not content:
            ap.error(
                "缺少正文，三种方式选一种：\n"
                "  --content \"正文\"\n"
                "  --content-file body.txt\n"
                "  --candidate N   （或 id，如 --candidate c01；先用 --list-candidates 看有哪些）"
            )

        if not title:
            title = _title_from(content)
            print(f"标题自动生成：{title}")

    if not kw and not args.login:
        ap.error("缺少吧名（--kw 或第一个位置参数）")

    poster = Poster(headless=args.headless, keep_open=args.keep_open)
    try:
        poster.open()
        print(f"已登录：{poster.login_user}（浏览器 {'headless' if poster.headless else '窗口'}）")
        if args.login:
            return 0

        if not title or not content:
            ap.error("缺少标题或正文")
        if len(title) > TITLE_LIMIT:
            print(f"！标题 {len(title)} 字超过 {TITLE_LIMIT}，将截为：{title[:TITLE_LIMIT]}")
        print(f"目标：{kw}吧\n标题：{title[:TITLE_LIMIT]}\n正文：{len(content)} 字")
        if not args.yes:
            try:
                answer = input("确认发布？输入 y 回车：").strip().lower()
            except EOFError:
                answer = "n"
            if answer not in ("y", "yes"):
                print("已取消")
                return 1

        result = poster.publish_verified(
            kw,
            title,
            content,
            max_retries=args.max_retries,
            verify_delay=args.verify_delay,
        )
        rounds_text = _format_rounds(result.rounds)
        if rounds_text:
            print(rounds_text)
        print("\n" + str(result))

        # 成败都落盘：一条记录里同时有 cookie、吧名标题正文、结局和逐轮证据
        path = _write_ledger(
            result,
            content=content,
            requested_title=title,
            cookie=poster.cookie,
            cookie_count=poster.cookie_count,
            login_user=poster.login_user,
        )
        _, outcome_text = _classify_outcome(result)
        print(f"\n结局：{outcome_text}")
        print(f"记录已保存（含 cookie 原文，勿外发）：{path}")

        if result.status == STATUS_OK:
            return 0

        retries = max(0, len(result.rounds) - 1)
        if result.status in (STATUS_REJECTED, STATUS_ERROR):
            print(
                f"最终状态 {result.status}（{result.reason}），"
                f"明确没发出去，按规则不重发，结束运行。"
            )
        else:
            print(f"最终状态 {result.status}，重发 {retries} 次后仍未确认，结束运行。")
        return 2
    except PosterError as exc:
        print(f"环境未就绪：{exc}")
        return 1
    finally:
        poster.close()


if __name__ == "__main__":
    sys.exit(main())
