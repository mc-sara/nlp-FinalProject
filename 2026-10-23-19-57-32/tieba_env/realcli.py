"""真实侧运行支撑：CLI 映射、输出解析、健康检查、回查轮询、发帖限额守卫。

这一层只做"如实执行 + 如实记录"，不含任何策略判断。所有函数都能被注入
（run / sleep / now），所以整条链可以在不起浏览器的情况下单测。

真实工具的两个硬约束（读自 tieba-poster 源码，不是猜的）：
  1. **CLI** 的 `post <文件>` 只吃帖子文件；但内部函数
     `publish_one(e, kw, title, content)` 直接吃三个字符串 →
     想内联传参就别走 CLI，见 publish_api.py；
  2. 它**不输出 JSON**，只有人类可读的
     `    成功（9.2s）https://tieba.baidu.com/p/11127594809` /
     `    未成功（10.1s）：无法确认：...` →
     四状态必须靠 parse_poster_output 解析，解析不了就老实记 unknown。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from .types import STATUS_ERROR, STATUS_OK, STATUS_REJECTED, STATUS_UNKNOWN

REQUIRED_HEALTH_CHECKS = ("login", "level", "editor", "list")


class ConfigError(RuntimeError):
    """配置缺字段、路径不对、脚本输出无法解析等部署问题。"""


class GuardError(RuntimeError):
    """限额守卫拒绝执行（间隔不足 / 超日限 / 验证码冷却中）。"""


# --------------------------------------------------------------------------- 配置


@dataclass
class RealCliConfig:
    """真实工具的路径与行为参数。全部来自 real/config.json。"""

    poster_dir: str
    python: str
    poster_script: str
    probe_script: str
    tieba_config: str
    kw: str
    # 唯一的实发开关：kw 不在这里面就绝不调 browser_poster.py post。
    # 留空 = 谁也发不出去，必须先由人显式加进去。
    allowed_publish_kw: list[str] = field(default_factory=list)
    title_limit: int = 31            # browser_poster 会截断到 31 字
    list_limit: int = 20
    headless: bool = False
    editor_check: bool = True
    require_min_level: int | None = None
    allow_empty_forum: bool = False

    publish_interval_seconds: int = 600
    max_publishes_per_day: int = 10
    recheck_polls: int = 7
    recheck_interval_seconds: float = 2.0
    captcha_cooldown_hours: int = 24

    outbox_dir: str = "real/outbox"
    state_path: str = "real/state.json"
    candidates_path: str = "tieba_env/data/candidates.json"

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "RealCliConfig":
        data = dict(data)
        required = ("poster_dir", "python", "poster_script", "probe_script", "tieba_config", "kw")
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise ConfigError(f"配置缺少必填项: {', '.join(missing)}")
        known = set(RealCliConfig.__dataclass_fields__)
        kwargs = {k: v for k, v in data.items() if k in known and k not in required}
        return RealCliConfig(**{k: str(data[k]) for k in required}, **kwargs)

    @staticmethod
    def load(path: str | Path) -> "RealCliConfig":
        file = Path(path)
        if not file.exists():
            raise ConfigError(f"配置文件不存在: {file}")
        return RealCliConfig.from_dict(json.loads(file.read_text(encoding="utf-8")))

    def resolve(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else (Path(self.poster_dir) / p)


# --------------------------------------------------------------------------- 输出解析

_VERDICT_RE = re.compile(r"^\s*(?P<neg>未)?成功（(?P<dur>[\d.]+)s）\s*[：:]?\s*(?P<msg>.*)$", re.M)
_TID_RE = re.compile(r"/p/(\d+)")

# 真实错误码/文案 → (status, reason)。按顺序匹配，先命中先用。
_FAILURE_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("无法确认",), STATUS_UNKNOWN, "no_response"),
    (("验证码", "[4]", "[40]"), STATUS_UNKNOWN, "captcha"),
    (("[101]", "未登录", "cookie", "Cookie", "登录态"), STATUS_ERROR, "login_expired"),
    (("[273]", "[1011]", "[34]", "等级", "未加入", "禁止发帖"), STATUS_REJECTED, "level_limit"),
    (("[1]", "内容不合法"), STATUS_REJECTED, "content_invalid"),
    (("[10]", "频繁"), STATUS_REJECTED, "rate_limited"),
    (("标题",), STATUS_REJECTED, "title_too_long"),
    (("重复",), STATUS_REJECTED, "duplicate"),
    (("超时", "Timeout", "timeout", "WebSocket", "websocket"), STATUS_UNKNOWN, "submit_timeout"),
)


def parse_poster_output(stdout: str, returncode: int = 0) -> tuple[dict[str, Any], str]:
    """解析 browser_poster.py 的输出 → (四状态 payload, 备注)。

    拿不到判定行时：返回码非 0 记 error/selector_missing（工具侧不可用，整条剔除），
    返回码为 0 但没判定行记 unknown/no_response（不知道就是不知道）。
    """
    matches = list(_VERDICT_RE.finditer(stdout))
    if not matches:
        note = "解析不到判定行"
        if returncode != 0:
            return {"status": STATUS_ERROR, "reason": "selector_missing", "_note": f"{note}，退出码 {returncode}"}, note
        return {"status": STATUS_UNKNOWN, "reason": "no_response", "_note": note}, note

    last = matches[-1]
    msg = last.group("msg").strip()
    if not last.group("neg"):
        tid = _TID_RE.search(msg)
        payload: dict[str, Any] = {"status": STATUS_OK, "post_id": tid.group(1) if tid else None}
        # 页面说成功但拿不到 tid（可能在审核）→ post_id 为 None，但状态仍是 ok
        return payload, msg

    for needles, status, reason in _FAILURE_RULES:
        if any(needle in msg for needle in needles):
            return {"status": status, "reason": reason, "_note": msg}, msg
    note = f"未映射的失败文案: {msg}"
    return {"status": STATUS_UNKNOWN, "reason": "no_response", "_note": note}, note


def classify_verdict(ok: bool, msg: str) -> dict[str, Any]:
    """把 `publish_one` 返回的 `(ok, msg)` 直接映射成四状态。

    跟 parse_poster_output 共用同一份 `_FAILURE_RULES` —— 规则只有一处，
    避免"CLI 路径"和"函数直调路径"对同一句话给出两种判定。

    与解析 stdout 的区别：这里已经有脚本自己给出的布尔判定，不需要再去
    正则匹配「成功（9.2s）」那一行，所以省掉退出码推断那一层。
    """
    msg = (msg or "").strip()
    if ok:
        tid = _TID_RE.search(msg)
        # 脚本说成功但拿不到 tid（可能在审核）→ 状态 ok，post_id 为 None，不编造
        return {"status": STATUS_OK, "post_id": tid.group(1) if tid else None}
    for needles, status, reason in _FAILURE_RULES:
        if any(needle in msg for needle in needles):
            return {"status": status, "reason": reason, "_note": msg}
    return {"status": STATUS_UNKNOWN, "reason": "no_response", "_note": msg}


# --------------------------------------------------------------------------- transport


def _parse_json_line(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ConfigError(f"输出里找不到 JSON: {stdout.strip()[:200]}")


@dataclass
class RealTransport:
    """一个 transport 同时服务 adapter 的三个工具和健康检查的四项。"""

    cfg: RealCliConfig
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    clock: Callable[[], datetime] = field(default=lambda: datetime.now().astimezone())
    timeout: float = 300.0
    log: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = args or {}
        if name == "get_candidates":
            return {"candidates": self._candidates()}
        if name in ("list_recent_posts", "list"):
            return self._probe("list", ["--kw", str(args.get("kw") or self.cfg.kw),
                                        "--limit", str(args.get("limit") or self.cfg.list_limit)])
        if name == "login":
            return self._probe("login", [])
        if name == "level":
            return self._probe("level", ["--kw", str(args.get("kw") or self.cfg.kw)])
        if name == "editor":
            return self._editor(str(args.get("kw") or self.cfg.kw))
        if name == "publish":
            return self._publish(args)
        raise ConfigError(f"未知动作 {name}")

    # -- 内部 ---------------------------------------------------------

    def _candidates(self) -> list[str]:
        path = Path(self.cfg.candidates_path)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("candidates") or []
        return [item["content"] if isinstance(item, dict) else str(item) for item in raw]

    def _exec(self, argv: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess:
        proc = self.run(
            argv, cwd=cwd or self.cfg.poster_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=self.timeout,
        )
        self.log.append({"argv": argv, "returncode": proc.returncode, "ts": self.clock().isoformat()})
        return proc

    def _probe(self, cmd: str, extra: list[str]) -> dict[str, Any]:
        argv = [
            self.cfg.python, self.cfg.probe_script,
            "--poster-dir", self.cfg.poster_dir,
            "--config", self.cfg.tieba_config,
            cmd, *extra,
        ]
        proc = self._exec(argv)
        payload = _parse_json_line(proc.stdout or "")
        if payload.get("error"):
            raise ConfigError(f"只读探针 {cmd} 失败: {payload['error']}")
        return payload

    def _editor(self, kw: str) -> dict[str, Any]:
        argv = [self.cfg.python, self.cfg.poster_script, "--config", self.cfg.tieba_config,
                "probe", "--kw", kw]
        proc = self._exec(argv)
        text = (proc.stdout or "") + (proc.stderr or "")
        found = {key: f"{key} : 已找到" in text for key in ("标题框", "正文框", "发布键")}
        return {
            "editor_ready": all(found.values()),
            "detail": ", ".join(f"{k}={'OK' if v else '缺失'}" for k, v in found.items()),
        }

    def _write_post_file(self, kw: str, title: str, content: str) -> Path:
        outbox = Path(self.cfg.outbox_dir)
        if not outbox.is_absolute():
            outbox = Path(__file__).resolve().parent.parent / outbox
        outbox.mkdir(parents=True, exist_ok=True)
        stamp = self.clock().strftime("%Y%m%d-%H%M%S-%f")
        path = outbox / f"post-{stamp}.md"
        if len(title) > self.cfg.title_limit:
            title = title[: self.cfg.title_limit]
        path.write_text(f"---\nkw: {kw}\ntitle: {title}\n---\n\n{content}\n", encoding="utf-8")
        return path

    def _publish(self, args: dict[str, Any]) -> dict[str, Any]:
        kw = str(args["kw"])
        title = str(args["title"])
        content = str(args["content"])
        post_file = self._write_post_file(kw, title, content)

        argv = [self.cfg.python, self.cfg.poster_script, "--config", self.cfg.tieba_config]
        if self.cfg.headless:
            argv.append("--headless")
        argv += ["post", str(post_file), "--yes"]

        proc = self._exec(argv)
        payload, note = parse_poster_output(proc.stdout or "", proc.returncode)
        payload["_note"] = note
        payload["_post_file"] = str(post_file)
        return payload


def build_transport(cfg: RealCliConfig, **kwargs: Any) -> RealTransport:
    return RealTransport(cfg=cfg, **kwargs)


# --------------------------------------------------------------------------- 健康检查


def _ok(detail: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "detail": detail, **extra}


def _bad(detail: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "detail": detail, **extra}


def _check_login(payload: Mapping[str, Any], cfg: RealCliConfig) -> dict[str, Any]:
    if payload.get("ok") and payload.get("tbs_ok"):
        return _ok(f"已登录 {payload.get('user', '')}".strip())
    if payload.get("error"):
        return _bad(f"登录态异常: {payload['error']}")
    return _bad(f"登录态异常: {dict(payload)}")


def _check_level(payload: Mapping[str, Any], cfg: RealCliConfig) -> dict[str, Any]:
    level = payload.get("level")
    liked = bool(payload.get("liked"))
    if cfg.require_min_level is None:
        # 不设阈值：README 实测「273 被拒」与等级无关，强判会误杀。只记录信息。
        return _ok(f"等级 {level}，已关注={liked}（未设阈值，仅记录）")
    if level is None:
        return _bad(f"读不到等级（未关注？liked={liked}）")
    if int(level) < cfg.require_min_level:
        return _bad(f"等级 {level} < 要求 {cfg.require_min_level}")
    return _ok(f"等级 {level}，已关注={liked}")


def _check_editor(payload: Mapping[str, Any], cfg: RealCliConfig) -> dict[str, Any]:
    if payload.get("editor_ready"):
        return _ok(payload.get("detail", "编辑器就绪"))
    return _bad(payload.get("detail", "编辑器元素缺失"))


def _check_list(payload: Mapping[str, Any], cfg: RealCliConfig) -> dict[str, Any]:
    posts = payload.get("posts")
    if not isinstance(posts, list):
        return _bad("列表返回里没有 posts 数组")
    if posts:
        return _ok(f"读到 {len(posts)} 条")
    if cfg.allow_empty_forum:
        return _ok("列表可读（空吧，配置允许）")
    return _bad("列表为空，无法据此查证")


_RUNNERS: dict[str, Callable[[Callable[..., dict[str, Any]], RealCliConfig], dict[str, Any]]] = {
    "login": lambda transport, cfg: transport("login", {}),
    "level": lambda transport, cfg: transport("level", {"kw": cfg.kw}),
    "editor": lambda transport, cfg: transport("editor", {"kw": cfg.kw}),
    "list": lambda transport, cfg: transport("list", {"kw": cfg.kw}),
}
_EVALUATORS = {
    "login": _check_login,
    "level": _check_level,
    "editor": _check_editor,
    "list": _check_list,
}


def health_check(transport: Callable[..., dict[str, Any]], cfg: RealCliConfig) -> dict[str, Any]:
    """PLAN 6.1：四项任一失败，整条轨迹记 FAILED_TOOL，不进训练集也不进分母。

    "取数据"与"判合格"分开：前者会抛异常（网络/登录），后者是纯函数，便于单测。
    """
    checks: dict[str, Any] = {}
    for name in REQUIRED_HEALTH_CHECKS:
        if name == "editor" and not cfg.editor_check:
            checks[name] = {"ok": True, "skipped": True, "detail": "配置里关闭了编辑器探测"}
            continue
        try:
            payload = _RUNNERS[name](transport, cfg)
        except Exception as exc:  # noqa: BLE001 - 取数失败本身就是一项失败
            checks[name] = _bad(f"调用失败: {type(exc).__name__}: {exc}")
            continue
        checks[name] = _EVALUATORS[name](payload, cfg)

    return {"ok": all(item["ok"] for item in checks.values()), "checks": checks}


# --------------------------------------------------------------------------- 回查轮询


def _norm(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def poll_for_title(
    list_fn: Callable[[], dict[str, Any]],
    title: str,
    *,
    polls: int = 7,
    interval: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """发帖后轮询吧页，直到看到该标题。既是查证手段，也是 D1 的回查延迟实测。

    注意：真实吧页列表只给标题，不给正文 → 只能按标题比对。
    所以标题必须足够独特，否则会误判（README 有说明）。
    """
    target = _norm(title)
    started = time.monotonic()
    last_posts: list[dict[str, Any]] = []
    for index in range(1, max(1, polls) + 1):
        last_posts = list_fn().get("posts") or []
        if any(_norm(post.get("title")) == target for post in last_posts):
            return {
                "found": True,
                "matched_at_poll": index,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "seen": len(last_posts),
            }
        if index < polls:
            sleep(interval)
    return {
        "found": False,
        "matched_at_poll": None,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "seen": len(last_posts),
    }


# --------------------------------------------------------------------------- 限额守卫


@dataclass
class PublishGuard:
    """发帖间隔、每日上限、验证码停机。

    PLAN 6.3：账号一旦触发风控，真实验证通道就断了。这层宁可拒绝执行，
    也不允许脚本化地连发。
    """

    cfg: RealCliConfig
    now_fn: Callable[[], datetime] = field(default=lambda: datetime.now().astimezone())

    @property
    def path(self) -> Path:
        p = Path(self.cfg.state_path)
        return p if p.is_absolute() else Path(__file__).resolve().parent.parent / p

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"publishes": [], "cooldown_until": None}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def check(self) -> None:
        state = self._load()
        now = self.now_fn()

        cooldown = state.get("cooldown_until")
        if cooldown:
            until = datetime.fromisoformat(cooldown)
            if now < until:
                raise GuardError(f"验证码冷却中，{until.isoformat(timespec='minutes')} 之前不发起真实发帖")

        stamps = [datetime.fromisoformat(t) for t in state.get("publishes") or []]
        # publish_interval_seconds <= 0 表示显式关闭间隔限制（连发不再需要等待）。
        # 日上限与验证码冷却不随之关闭 —— 它们防的是账号被封，不是"手快"。
        if stamps and self.cfg.publish_interval_seconds > 0:
            gap = (now - max(stamps)).total_seconds()
            if gap < self.cfg.publish_interval_seconds:
                wait = int(self.cfg.publish_interval_seconds - gap)
                raise GuardError(f"距上次发帖 {int(gap)}s，还需等 {wait}s（不要下调间隔）")

        recent = [t for t in stamps if now - t < timedelta(hours=24)]
        if len(recent) >= self.cfg.max_publishes_per_day:
            raise GuardError(f"24 小时内已发 {len(recent)} 条，达到上限 {self.cfg.max_publishes_per_day}")

    def record_publish(self) -> None:
        state = self._load()
        state.setdefault("publishes", []).append(self.now_fn().isoformat())
        self._save(state)

    def record_captcha(self) -> None:
        state = self._load()
        until = self.now_fn() + timedelta(hours=self.cfg.captcha_cooldown_hours)
        state["cooldown_until"] = until.isoformat()
        self._save(state)
