#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tieba_poster.py —— 百度贴吧自动发帖工具（命令行）

用法速查：
    python tieba_poster.py check                      # 检查 Cookie 登录态
    python tieba_poster.py post posts/example.md      # 发一个帖子文件
    python tieba_poster.py post posts/                # 批量发一个目录下所有帖子
    python tieba_poster.py reply --kw python --tid 123456 --text "顶一下"
    python tieba_poster.py sign --kw python           # 贴吧签到
    python tieba_poster.py template posts/new.md      # 生成帖子文件模板

只依赖 requests：pip install requests
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("缺少依赖，请先执行：pip install requests")

HERE = Path(__file__).resolve().parent


def _pick(p: Path) -> Path:
    """路径在当前目录下找不到时，回退到脚本所在目录再找一次。

    这样站在别的目录里跑（`python tieba-poster/tieba_poster.py check`）
    也能正确读到 config.json 和 posts/ 下的帖子。
    """
    if p.is_absolute() or p.exists():
        return p
    alt = HERE / p
    return alt if alt.exists() else p

# Windows 控制台用 UTF-8 输出，避免中文乱码
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

__version__ = "1.0.2"

BASE = "https://tieba.baidu.com"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 发帖接口常见错误码提示（百度会调整，仅供参考）
POST_ERROR_HINTS = {
    1: "内容不合法或被拦截（常见：敏感词、外链、内容过短、纯表情）",
    4: "需要验证码（账号或该吧触发风控）",
    9: "吧不存在 / fid 无效（吧名写错了？），或发帖参数被服务端拒绝",
    10: "发帖过于频繁，或该吧需要验证码",
    20: "标题/内容触发审核",
    30: "帖子进入人工审核",
    34: "该贴吧禁止发帖 / 账号在本吧被限制",
    40: "需要验证码（风控）",
    101: "未登录或登录态失效",
    273: "被拒：旧接口 /f/commit/post/add 已对不上新版风控（需要 JS 签名），"
         "这条 requests 路基本必然失败——发帖请用 browser_poster.py。"
         "也可能同时存在等级/关注门槛，可用 forum 命令自查",
    1011: "你还没加入该吧，或吧内等级不够",
}
SIGN_OK_ERRNO = {0, 1101, 1102}  # 0=签到成功，1101/1102=今天已签到


# --------------------------------------------------------------------------- #
# 异常 & 日志
# --------------------------------------------------------------------------- #
class TiebaError(Exception):
    def __init__(self, code: Any, message: str, raw: Any = None):
        self.code = code
        self.message = message
        self.raw = raw
        super().__init__(f"[{code}] {message}")


def log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


# --------------------------------------------------------------------------- #
# 内容结构
# --------------------------------------------------------------------------- #
@dataclass
class Post:
    kw: str                     # 吧名，例如 "python"（不带“吧”字）
    title: str                  # 主题帖标题
    content: str                # 正文
    images: list[Path] = field(default_factory=list)
    sign: bool | None = None    # None = 跟随全局配置


def parse_cookie(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:]
    jar: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                jar[k] = v
    return jar


def split_content(text: str, limit: int) -> list[str]:
    """把超长正文按段落切成多段，第一段发主题帖，其余作为回复跟帖。"""
    text = text.replace("\r\n", "\n").strip()
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        while len(para) > limit:            # 单段就超长 -> 硬切
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[:limit])
            para = para[limit:]
        if not buf:
            buf = para
        elif len(buf) + 2 + len(para) <= limit:
            buf = f"{buf}\n\n{para}"
        else:
            chunks.append(buf)
            buf = para
    if buf:
        chunks.append(buf)
    return chunks


# --------------------------------------------------------------------------- #
# 贴吧客户端
# --------------------------------------------------------------------------- #
class TiebaClient:
    def __init__(self, cookie: str, user_agent: str = DEFAULT_UA, timeout: int = 20,
                 proxy: str | None = None, extra_fields: dict[str, str] | None = None,
                 fid_map: dict[str, str] | None = None, trust_env: bool = True):
        self.cookies = parse_cookie(cookie)
        if "BDUSS" not in self.cookies:
            raise TiebaError(-1, "Cookie 中找不到 BDUSS —— 请从浏览器复制完整的 Cookie 串")
        self.timeout = timeout
        self.extra_fields = {str(k): str(v) for k, v in (extra_fields or {}).items()}
        self.fid_map = {str(k).strip(): str(v).strip() for k, v in (fid_map or {}).items()}
        self._tbs: str | None = None
        self._fid_cache: dict[str, str] = {}
        self._fid_warned: set[str] = set()
        self._page_cache: dict[str, str] = {}
        self._like_cache: dict[str, dict] | None = None

        self.s = requests.Session()
        self.s.trust_env = trust_env          # False = 忽略系统代理/环境变量，强制直连
        self.s.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": BASE + "/",
            "Origin": BASE,
            "Connection": "keep-alive",
        })
        self.s.cookies.update(self.cookies)
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}

    # ---------------------------- 底层请求 ---------------------------- #
    def _request(self, method: str, url: str, retries: int = 3, **kw) -> requests.Response:
        last: Exception | None = None
        for i in range(retries):
            try:
                return self.s.request(method, url, timeout=self.timeout, **kw)
            except UnicodeEncodeError as e:
                # 请求头里混进了非 latin-1 字符（典型：中文吧名没做 URL 编码）
                raise TiebaError(
                    -13,
                    f"请求头里含无法编码的字符：{e}\n"
                    f"  这属于脚本内部问题，把这段报错发我；临时办法："
                    f"先把吧名换成英文吧试试",
                )
            except requests.RequestException as e:
                last = e
                if i < retries - 1:
                    time.sleep(2 * (i + 1))

        is_timeout = isinstance(last, (requests.Timeout,))
        if is_timeout:
            raise TiebaError(
                -2,
                f"网络超时（{self.timeout}s × {retries} 次都没响应）。\n"
                f"  这不是 Cookie 的问题。常见原因：开了代理/VPN，或 DNS 解析到连不通的 IP。\n"
                f"  先跑这个定位：python tieba_poster.py netcheck\n"
                f"  确认是代理问题的话，加 --ignore-proxy 强制直连再试一次",
            )
        raise TiebaError(-2, f"网络请求失败（{type(last).__name__}）：{last}")

    @staticmethod
    def _json(r: requests.Response, ctx: str) -> dict:
        text = r.content.decode("utf-8", "ignore")
        try:
            data = json.loads(text)
        except ValueError:
            snippet = re.sub(r"\s+", " ", text)[:200]
            raise TiebaError(-4, f"{ctx}：接口返回非 JSON（可能被风控或需验证码）：{snippet}")
        if data is None:
            raise TiebaError(-4, f"{ctx}：服务器返回 null，通常表示 Cookie 无效或已过期")
        if not isinstance(data, dict):
            snippet = re.sub(r"\s+", " ", text)[:200]
            raise TiebaError(-4, f"{ctx}：接口返回格式异常（{type(data).__name__}）：{snippet}")
        return data

    # ---------------------------- 基础信息 ---------------------------- #
    def get_user_info(self) -> dict:
        r = self._request("GET", f"{BASE}/f/user/json_userinfo")
        j = self._json(r, "查询登录态")
        data = j.get("data") if isinstance(j.get("data"), dict) else {}
        if str(j.get("no")) != "0" or not data:
            raise TiebaError(j.get("no", 101), j.get("error") or "登录态无效，请重新获取 Cookie")

        # 不同版本接口字段名不一样：老版 user_name，现在多见 show_nickname / user_name_show
        if not data.get("user_name"):
            data["user_name"] = (data.get("show_nickname") or data.get("user_name_show")
                                 or data.get("user_name_url") or "")
        if not data.get("user_id"):
            data["user_id"] = data.get("open_uid") or data.get("user_itieba_id") or ""

        if not data.get("is_login") and not data.get("user_name"):
            raise TiebaError(101, "登录态无效：接口没返回用户信息，Cookie 可能已过期")
        return data

    def get_tbs(self, force: bool = False) -> str:
        if self._tbs and not force:
            return self._tbs
        r = self._request("GET", f"{BASE}/dc/common/tbs")
        j = self._json(r, "获取 tbs")
        if j.get("is_login") != 1 or not j.get("tbs"):
            raise TiebaError(101, "未登录：Cookie 已失效（BDUSS/STOKEN 过期），请重新获取")
        self._tbs = j["tbs"]
        return self._tbs

    def _forum_html(self, kw: str) -> str:
        """抓吧页面的 HTML（一个请求里同时含 fid 和你在该吧的等级/关注状态）。

        新版 PC 吧页 /f?kw= 是 SPA 空壳（只有埋点脚本，抓不到数据）；
        移动页 /mo/q/m 仍是服务端渲染的完整 HTML，而且必须用桌面 UA 请求
        —— 换成移动 UA 反而会被 403。
        """
        kw = kw.strip()
        if kw in self._page_cache:
            return self._page_cache[kw]
        html = ""
        for url, params in ((f"{BASE}/mo/q/m", {"kw": kw}),
                            (f"{BASE}/f", {"kw": kw, "ie": "utf-8"})):
            try:
                r = self._request("GET", url, params=params)
            except TiebaError:
                continue
            text = r.content.decode("utf-8", "ignore")
            if "百度安全验证" in text or "wappass" in r.url:
                continue
            html = text
            if re.search(r'"user_level"|"forum_id"', text):
                break
        self._page_cache[kw] = html
        return html

    def my_like_forums(self) -> dict[str, dict]:
        """当前账号关注的吧（含真实 forum_id / 等级 / 经验），来自 newmoindex 接口。

        这是最可靠的一份数据：吧页面里混杂着广告位、推荐位的 forum_id，
        直接正则抓「第一个 forum_id」会抓到别人家的吧。
        """
        if self._like_cache is not None:
            return self._like_cache
        try:
            r = self._request("GET", f"{BASE}/mo/q/newmoindex")
            j = self._json(r, "查询关注列表")
        except TiebaError:
            self._like_cache = {}
            return self._like_cache
        data = j.get("data") if isinstance(j.get("data"), dict) else {}
        out: dict[str, dict] = {}
        for f in data.get("like_forum") or []:
            name = (f.get("forum_name") or "").strip()
            if name:
                out[name] = f
        self._like_cache = out
        return out

    @staticmethod
    def _pick_fid(html: str, kw: str) -> str | None:
        """从页面里挑「和吧名对得上」的 forum_id，避开推荐位的干扰。"""
        targets = {kw, quote(kw, safe="")}
        for m in re.finditer(r'fid["\']?\s*[:=]\s*["\']?(\d+)["\']?', html):
            seg = html[max(0, m.start() - 220): m.start() + 220]
            if any(t in seg for t in targets):
                return m.group(1)
        m = re.search(r'"forum"\s*:\s*\{[^{}]*?"id"\s*:\s*(\d+)', html)
        return m.group(1) if m else None

    def get_fid(self, kw: str) -> str:
        """查吧的 fid。查不到时返回空串，让发帖接口按 kw 自己找，不直接失败。"""
        kw = kw.strip()
        if kw in self._fid_cache:
            return self._fid_cache[kw]
        if kw in self.fid_map:                       # 用户手动指定的映射优先
            self._fid_cache[kw] = self.fid_map[kw]
            return self.fid_map[kw]

        # 已关注的吧：newmoindex 直接给出 forum_id，最可信
        info = self.my_like_forums().get(kw)
        if info and info.get("forum_id"):
            fid = str(info["forum_id"])
            self._fid_cache[kw] = fid
            return fid

        fid = self._pick_fid(self._forum_html(kw), kw)
        if fid:
            self._fid_cache[kw] = fid
            return fid

        if kw not in self._fid_warned:
            self._fid_warned.add(kw)
            log(f"  提示：没解析到「{kw}」的 fid，先按空 fid 试发"
                f"（失败请检查吧名，或在 config.json 的 fid_map 里手动指定）")
        return ""

    def get_forum_status(self, kw: str) -> dict:
        """查当前账号在该吧的状态：等级、是否关注、经验。

        数据取自 newmoindex（账号关注列表），而不是吧页面 —— 吧页面里
        的 user_level/is_like 是首屏别的模块的默认值，会读错。
        """
        kw = kw.strip()
        info = self.my_like_forums().get(kw)
        if info:
            try:
                lv = int(info.get("user_level") or 0)
            except (TypeError, ValueError):
                lv = None
            try:
                exp = int(info.get("user_exp") or 0)
            except (TypeError, ValueError):
                exp = None
            return {
                "kw": kw,
                "fid": self.get_fid(kw) or str(info.get("forum_id") or ""),
                "user_level": lv or None,
                "level_name": "",
                "is_like": bool(info.get("is_like")),
                "cur_score": exp,
                "levelup_score": None,
                "signed": bool(info.get("is_sign")),
            }
        # 没关注这个吧：只有 fid 可信，等级无从得知
        return {
            "kw": kw,
            "fid": self.get_fid(kw),
            "user_level": None,
            "level_name": "",
            "is_like": False,
            "cur_score": None,
            "levelup_score": None,
            "signed": None,
        }

    # ---------------------------- 发帖 / 回复 ---------------------------- #
    @staticmethod
    def _forum_referer(kw: str) -> str:
        """HTTP 头只能是 latin-1 字符，中文吧名必须先 URL 编码。

        否则 urllib3 组装请求头时会炸：
            UnicodeEncodeError: 'latin-1' codec can't encode characters ...
        英文吧名是 ASCII 所以看不出问题，一换成中文吧就崩。
        """
        return f"{BASE}/f?kw={quote(kw, safe='')}"

    def _commit(self, data: dict, referer: str) -> dict:
        payload = dict(data)
        payload.update(self.extra_fields)
        r = self._request(
            "POST", f"{BASE}/f/commit/post/add", data=payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": referer,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        j = self._json(r, "发帖")
        if str(j.get("no")) != "0":
            no = j.get("no")
            hint = POST_ERROR_HINTS.get(no if isinstance(no, int) else -999, "")
            msg = j.get("error") or j.get("error_msg") or "未知错误"
            if hint:
                msg = f"{msg}（{hint}）"
            data = j.get("data")
            if isinstance(data, dict) and data:
                msg += "\n  [接口返回] " + json.dumps(data, ensure_ascii=False)[:300]
            raise TiebaError(no, msg, raw=j)
        return j.get("data") if isinstance(j.get("data"), dict) else {}

    def post_thread(self, kw: str, title: str, content: str) -> dict:
        fid = self.get_fid(kw)
        tbs = self.get_tbs()
        return self._commit({
            "ie": "utf-8",
            "kw": kw,
            "fid": fid,
            "tid": "0",
            "vcode": "",
            "vcode_md5": "",
            "floor_num": "0",
            "tbs": tbs,
            "content": content,
            "title": title,
            "rich_text": "1",
            "anonymous": "",
            "fbuf": "",
            "mouse_pwd": "",
            "mouse_pwd_t": "",
            "mouse_pwd_isclick": "",
            "__type__": "0",
        }, referer=self._forum_referer(kw))

    def reply(self, kw: str, tid: str, content: str) -> dict:
        fid = self.get_fid(kw)
        tbs = self.get_tbs()
        return self._commit({
            "ie": "utf-8",
            "kw": kw,
            "fid": fid,
            "tid": str(tid),
            "vcode": "",
            "vcode_md5": "",
            "floor_num": "0",
            "tbs": tbs,
            "content": content,
            "title": "",
            "rich_text": "1",
            "anonymous": "",
            "__type__": "0",
        }, referer=f"{BASE}/p/{tid}")

    # ---------------------------- 签到 ---------------------------- #
    def sign_forum(self, kw: str) -> str:
        tbs = self.get_tbs()
        r = self._request(
            "POST", f"{BASE}/sign/add",
            data={"ie": "utf-8", "kw": kw, "tbs": tbs},
            headers={"X-Requested-With": "XMLHttpRequest",
                     "Referer": self._forum_referer(kw)},
        )
        j = self._json(r, "签到")
        data = j.get("data") if isinstance(j.get("data"), dict) else {}
        errno = data.get("errno", j.get("no"))
        if str(j.get("no")) == "0" and str(errno) in {str(x) for x in SIGN_OK_ERRNO}:
            return data.get("errmsg") or "签到成功"
        raise TiebaError(errno, data.get("errmsg") or j.get("error") or "签到失败", raw=j)

    # ---------------------------- 图片上传（实验性） ---------------------------- #
    def upload_image(self, path: Path) -> str:
        """实验性：尝试把图片传到贴吧，返回可插入正文的 [img] 标签。
        该接口随时可能变动，失败请手动在网页端补图。"""
        tbs = self.get_tbs()
        with open(path, "rb") as f:
            r = self.s.post(
                f"{BASE}/c/c/upload/pic",
                params={"tbs": tbs},
                files={"picFile": (path.name, f, "image/png")},
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/"},
                timeout=60,
            )
        j = self._json(r, "上传图片")
        if str(j.get("no")) != "0":
            raise TiebaError(j.get("no"), f"图片上传失败：{j.get('error')}", raw=j)
        data = j.get("data") or {}
        url = data.get("pic_url") or data.get("url")
        if not url:
            m = re.search(r'https?://[^\s"\'<>]+', json.dumps(j, ensure_ascii=False))
            url = m.group(0) if m else None
        if not url:
            raise TiebaError(-5, "图片上传后没拿到 URL", raw=j)
        return f"[img pic_type=1 width=600 height=400]{url}[/img]"


# --------------------------------------------------------------------------- #
# 帖子文件解析（支持 .md/.txt 的 front matter，或 .json）
# --------------------------------------------------------------------------- #
META_RE = re.compile(r"^([A-Za-z_\u4e00-\u9fa5]+)\s*[:：]\s*(.*)$")
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


def _parse_meta_block(block: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = META_RE.match(line)
        if m:
            meta[m.group(1).strip().lower()] = m.group(2).strip()
    return meta


def _post_from_dict(d: dict, base_dir: Path) -> Post:
    kw = str(d.get("kw") or d.get("吧名") or "").strip()
    title = str(d.get("title") or d.get("标题") or "").strip()
    content = str(d.get("content") or d.get("正文") or "")
    raw_imgs = d.get("images") or d.get("图片") or []
    if isinstance(raw_imgs, str):
        raw_imgs = [x for x in re.split(r"[,，]", raw_imgs) if x.strip()]
    images = [(base_dir / str(x).strip()) for x in raw_imgs if str(x).strip()]
    sign = d.get("sign")
    return Post(kw=kw, title=title, content=content, images=images,
                sign=bool(sign) if sign is not None else None)


def load_posts(path: Path, default_kw: str = "") -> list[Post]:
    if not path.exists():
        raise TiebaError(-6, f"文件不存在：{path}")

    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else [data]
        posts = [_post_from_dict(it, path.parent) for it in items]
    else:
        raw = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
        raw = raw.replace("\r\n", "\n")
        meta: dict[str, str] = {}
        body = raw
        m = FRONT_MATTER_RE.match(raw)
        if m:
            meta = _parse_meta_block(m.group(1))
            body = raw[m.end():]
        meta = {**meta, "content": body}
        posts = [_post_from_dict(meta, path.parent)]

    for p in posts:
        p.kw = p.kw or default_kw
        p.content = p.content.strip()
        if not p.kw:
            raise TiebaError(-7, f"{path.name}：缺少吧名（kw），请在开头元信息里写 kw: 吧名")
        if not p.title:
            first = next((ln for ln in p.content.splitlines() if ln.strip()), "")
            p.title = re.sub(r"^#+\s*", "", first.strip())[:60]
        if not p.content:
            raise TiebaError(-7, f"{path.name}：正文为空")
    return posts


def collect_post_files(target: Path) -> list[Path]:
    if target.is_dir():
        files = sorted(
            [p for p in target.iterdir()
             if p.suffix.lower() in {".md", ".txt", ".json"} and not p.name.startswith("_")],
            key=lambda x: x.name,
        )
        return files
    return [target]


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: dict[str, Any] = {
    "cookie": "",                    # 必填：浏览器复制的完整 Cookie（含 BDUSS、STOKEN）
    "user_agent": DEFAULT_UA,
    "timeout": 20,
    "proxy": "",
    "default_kw": "",                # 帖子文件没写 kw 时用这个吧名
    "title_max_len": 31,             # 标题长度上限，超出截断
    "content_limit": 9000,           # 单楼正文上限，超出自动拆成跟帖
    "delay_range": [8, 20],          # 多帖之间随机间隔（秒）
    "sign_in": False,                # 发帖前是否顺带签到
    "upload_images": False,          # 是否尝试实验性上传图片
    "dry_run": False,
    "extra_fields": {},              # 追加到发帖表单的额外字段（应急用）
    "fid_map": {},                   # 手动指定 吧名 -> fid，例如 {"python": "10177"}
    "ignore_proxy": False,           # True = 忽略系统代理/环境变量，强制直连百度
}


def load_config(path: Path | None, cli: argparse.Namespace) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if path and path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            raise TiebaError(-8, f"配置文件 {path} 不是合法 JSON：{e}")
    elif path and not path.exists():
        log(f"提示：配置文件 {path} 不存在，使用命令行参数 / 默认值")

    # 环境变量优先于配置文件，命令行参数优先于环境变量
    env_cookie = os.environ.get("TIEBA_COOKIE") or os.environ.get("BAIDU_COOKIE")
    if env_cookie:
        cfg["cookie"] = env_cookie

    for key in ("cookie", "user_agent", "proxy", "default_kw"):
        val = getattr(cli, key, None)
        if isinstance(val, str) and val.strip():
            cfg[key] = val.strip()
    if getattr(cli, "dry_run", False):
        cfg["dry_run"] = True
    if getattr(cli, "upload_images", False):
        cfg["upload_images"] = True
    if getattr(cli, "sign", False):
        cfg["sign_in"] = True
    if getattr(cli, "ignore_proxy", False):
        cfg["ignore_proxy"] = True
    return cfg


# --------------------------------------------------------------------------- #
# 业务命令
# --------------------------------------------------------------------------- #
def build_client(cfg: dict[str, Any]) -> TiebaClient:
    if not cfg.get("cookie"):
        raise TiebaError(-9, "没有 Cookie。请在 config.json 里填 cookie 字段，"
                             "或设置环境变量 TIEBA_COOKIE，或用 --cookie 传参")
    return TiebaClient(
        cookie=cfg["cookie"],
        user_agent=cfg.get("user_agent") or DEFAULT_UA,
        timeout=int(cfg.get("timeout", 20)),
        proxy=cfg.get("proxy") or None,
        extra_fields=cfg.get("extra_fields") or {},
        fid_map=cfg.get("fid_map") or {},
        trust_env=not bool(cfg.get("ignore_proxy")),
    )


def cmd_check(cfg: dict[str, Any]) -> int:
    client = build_client(cfg)
    info = client.get_user_info()
    tbs = client.get_tbs()
    log(f"登录成功：{info.get('user_name')}（uid={info.get('user_id', '?')}）")
    log(f"tbs = {tbs[:12]}...（已获取）")
    return 0


def _windows_system_proxy() -> tuple[str, str]:
    """读 Windows 系统代理（设置 → 网络和 Internet → 代理 那个开关）"""
    if sys.platform != "win32":
        return ("", "")
    try:
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            enable = int(winreg.QueryValueEx(k, "ProxyEnable")[0])
            try:
                server = str(winreg.QueryValueEx(k, "ProxyServer")[0])
            except OSError:
                server = ""
        return ("已开启" if enable else "未开启", server)
    except Exception as e:                                    # noqa: BLE001
        return (f"读取失败（{e}）", "")


def cmd_netcheck(cfg: dict[str, Any]) -> int:
    """不碰 Cookie，单纯查网络：超时的锅到底在谁身上。"""
    url = f"{BASE}/dc/common/tbs"
    host = "tieba.baidu.com"
    print("===== 贴吧连通性诊断 =====\n")

    print("[1] 代理设置")
    envs = {k: os.environ.get(k) for k in
            ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy")}
    hit = {k: v for k, v in envs.items() if v}
    if hit:
        for k, v in hit.items():
            print(f"    环境变量 {k:12} = {v}")
    else:
        print("    环境变量：没有设置代理")
    if sys.platform == "win32":
        state, server = _windows_system_proxy()
        print(f"    Windows 系统代理：{state}" + (f"    {server}" if server else ""))
    used = requests.utils.get_environ_proxies(url)
    print(f"    requests 实际会走：{used if used else '（直连，不走代理）'}")

    print("\n[2] DNS 解析")
    addrs: list[str] = []
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        addrs = sorted({i[4][0] for i in infos})
        for a in addrs:
            print(f"    {a}  ({'IPv6' if ':' in a else 'IPv4'})")
        if not addrs:
            print("    没解析出地址！")
    except Exception as e:                                    # noqa: BLE001
        print(f"    解析失败：{e}")

    print("\n[3] TCP 443 握手（不走代理，裸连）")
    if addrs:
        for a in addrs[:4]:
            t0 = time.time()
            try:
                s = socket.create_connection((a, 443), timeout=6)
                s.close()
                print(f"    {a:28} 连通    {(time.time() - t0) * 1000:.0f} ms")
            except Exception as e:                            # noqa: BLE001
                print(f"    {a:28} 失败    {type(e).__name__}: {e}")
    else:
        print("    跳过（没有可用地址）")

    def probe(label: str, trust_env: bool, timeout: int = 8, rounds: int = 2) -> int:
        s = requests.Session()
        s.trust_env = trust_env
        okc = 0
        for _ in range(rounds):
            t0 = time.time()
            try:
                r = s.get(url, headers={"User-Agent": DEFAULT_UA}, timeout=timeout)
                cost = (time.time() - t0) * 1000
                okc += 1
                print(f"    {label:16} 成功  HTTP {r.status_code}  {cost:.0f} ms")
            except Exception as e:                            # noqa: BLE001
                cost = (time.time() - t0) * 1000
                print(f"    {label:16} 失败  {cost:.0f} ms  {type(e).__name__}")
        return okc

    print("\n[4] HTTPS 请求（默认设置 = 会走代理）")
    ok_default = probe("默认设置", trust_env=True)
    print("\n[5] HTTPS 请求（强制直连 = 忽略代理）")
    ok_direct = probe("强制直连", trust_env=False)

    print("\n[6] 结论")
    rounds = 2
    if ok_default == rounds and ok_direct == rounds:
        print("    两种方式都通，网络没问题。刚才的超时可能是偶发抖动，直接重试即可。")
    elif ok_default < rounds and ok_direct == rounds:
        print("    ★ 元凶是代理：走代理失败，直连正常。")
        print("      解决：命令加 --ignore-proxy，或在 config.json 里设 \"ignore_proxy\": true")
        print("      长期方案：给代理软件加一条规则，让 *.baidu.com 直连。")
    elif ok_default == rounds and ok_direct < rounds:
        print("    只有直连失败、走代理正常 —— 这很少见，保持默认（走代理）别加 --ignore-proxy。")
    else:
        print("    两种方式都不通，属于网络/DNS 层面的问题：")
        print("      1. 换网络试试（手机热点）")
        print("      2. 检查 hosts 文件里有没有把 tieba.baidu.com 指到奇怪 IP")
        print("      3. 检查防火墙/安全软件是否拦了 python.exe")
        print("      4. 关闭 VPN 后重试")
    return 0 if (ok_default == rounds or ok_direct == rounds) else 1


def _preflight(client: TiebaClient, posts: list[Post]) -> dict[str, dict]:
    """发帖前查一遍等级/关注状态，返回 {吧名: 状态} 供失败时做诊断。

    贴吧官方规则确实是「4 级头衔以上才能发主题帖」，但实测并不是所有吧都真拦，
    所以这里只做信息提示，不作为拦截依据。
    """
    checked: set[str] = set()
    cache: dict[str, dict] = {}
    for p in posts:
        if p.kw in checked:
            continue
        checked.add(p.kw)
        try:
            st = client.get_forum_status(p.kw)
        except TiebaError:
            continue
        cache[p.kw] = st
        lv = st.get("user_level")
        liked = bool(st.get("is_like"))
        if not st.get("fid") and lv is None:
            log(f"预检：查不到「{p.kw}」这个吧 —— 吧名可能写错了。"
                f"注意别带「吧」字（「孙笑川吧」应写成「孙笑川」）")
            continue
        if not liked:
            log(f"预检：你还没关注 {p.kw} 吧 → 部分吧对未关注账号设限，建议先点关注")
            continue
        if lv is None:
            continue
        if lv >= 4:
            log(f"预检：{p.kw}吧 {lv} 级、已关注 → 符合多数吧的发帖门槛")
        else:
            log(f"预检：{p.kw}吧 {lv} 级、已关注 → 部分吧要求 4 级，"
                f"若浏览器发帖被拦再考虑签到养等级")
    return cache


def cmd_post(cfg: dict[str, Any], target: Path, assume_yes: bool) -> int:
    if not cfg.get("dry_run"):
        log("[!] 提醒：这条 requests 发帖路已被新风控封死（稳定返回 273），"
            "真正要发帖请用：python browser_poster.py post <文件>")
    files = collect_post_files(target)
    if not files:
        raise TiebaError(-10, f"目录 {target} 下没有 .md/.txt/.json 帖子文件")

    posts: list[Post] = []
    for f in files:
        try:
            posts.extend(load_posts(f, cfg.get("default_kw") or ""))
        except TiebaError as e:
            log(f"跳过 {f.name}：{e}")

    if not posts:
        raise TiebaError(-10, "没有可发布的帖子")

    title_max = int(cfg.get("title_max_len", 31))
    limit = int(cfg.get("content_limit", 9000))
    dry = bool(cfg.get("dry_run"))

    print("\n===== 待发布清单 =====")
    for i, p in enumerate(posts, 1):
        if len(p.title) > title_max:
            p.title = p.title[:title_max]
            log(f"标题超长，已截断到 {title_max} 字")
        chunks = split_content(p.content, limit)
        print(f"{i}. 吧名：{p.kw}  标题：{p.title}")
        print(f"   正文 {len(p.content)} 字 / {len(chunks)} 段"
              + (f"，图片 {len(p.images)} 张" if p.images else ""))
    print("======================\n")

    if dry:
        log("dry-run 模式：只预览，不实际发帖")
        for p in posts:
            print("-" * 60)
            print(f"【{p.kw}】{p.title}")
            print(p.content[:600] + ("..." if len(p.content) > 600 else ""))
        if cfg.get("cookie"):
            try:
                _preflight(build_client(cfg), posts)
            except TiebaError as e:
                log(f"（预检跳过：{e}）")
        return 0

    if not assume_yes:
        if sys.stdin.isatty():
            ans = input("确认发布以上内容？(y/N) ").strip().lower()
            if ans not in ("y", "yes"):
                log("已取消")
                return 0
        else:
            raise TiebaError(-11, "非交互环境下请加 --yes 明确确认发布")

    client = build_client(cfg)
    info = client.get_user_info()
    log(f"以 {info.get('user_name')} 身份发帖")
    forum_status = _preflight(client, posts)

    delay_lo, delay_hi = (cfg.get("delay_range") or [8, 20])[:2]
    ok = fail = 0

    for idx, p in enumerate(posts, 1):
        prefix = f"[{idx}/{len(posts)}] {p.kw}吧 · {p.title}"
        try:
            if cfg.get("sign_in") or p.sign:
                try:
                    log(f"签到 {p.kw}吧 → {client.sign_forum(p.kw)}")
                except TiebaError as e:
                    log(f"签到失败（不影响发帖）：{e}")

            body = p.content
            if p.images:
                if cfg.get("upload_images"):
                    tags = []
                    for img in p.images:
                        try:
                            tags.append(client.upload_image(img))
                        except (TiebaError, OSError) as e:
                            log(f"图片 {img.name} 上传失败：{e}")
                    if tags:
                        body = body + "\n\n" + "\n".join(tags)
                else:
                    log(f"提示：忽略 {len(p.images)} 张本地图片（贴吧不允许外链图，"
                        f"加 --upload-images 试实验性上传，或发完手动补图）")

            chunks = split_content(body, limit)
            data = client.post_thread(p.kw, p.title, chunks[0])
            tid = str(data.get("tid") or "")
            ok += 1
            log(f"{prefix} → 成功  https://tieba.baidu.com/p/{tid}" if tid
                else f"{prefix} → 成功（未返回 tid）")

            for n, extra in enumerate(chunks[1:], 2):
                if not tid:
                    log(f"  第 {n} 段无法跟帖（缺 tid），请手动粘贴")
                    break
                time.sleep(random.uniform(3, 7))
                try:
                    client.reply(p.kw, tid, extra)
                    log(f"  已跟帖第 {n} 段（{len(extra)} 字）")
                except TiebaError as e:
                    fail += 1
                    log(f"  跟帖第 {n} 段失败：{e}")

        except TiebaError as e:
            fail += 1
            log(f"{prefix} → 失败：{e}")
            if str(e.code) in ("273", "1011"):
                st = forum_status.get(p.kw) or {}
                lv = st.get("user_level")
                liked = st.get("is_like")
                if lv is not None:
                    log(f"      诊断：你在 {p.kw}吧 是 {lv} 级"
                        f"（{st.get('level_name') or '-'}）、"
                        f"{'已关注' if liked else '未关注'} → "
                        f"贴吧要求 4 级，先关注 + 每天签到")
                else:
                    log("      诊断：拿不到你的吧内等级，先确认吧名拼写是否正确")

        if idx < len(posts):
            wait = random.uniform(delay_lo, delay_hi)
            log(f"随机等待 {wait:.1f} 秒后继续…")
            time.sleep(wait)

    print()
    log(f"完成：成功 {ok} 条，失败 {fail} 条")
    return 0 if fail == 0 else 1


def cmd_reply(cfg: dict[str, Any], kw: str, tid: str, text: str) -> int:
    client = build_client(cfg)
    if cfg.get("dry_run"):
        log(f"dry-run：将回复 {kw}吧 /p/{tid}：{text[:80]}")
        return 0
    data = client.reply(kw, tid, text)
    log(f"回复成功 pid={data.get('pid', '?')}  https://tieba.baidu.com/p/{tid}")
    return 0


def cmd_forum(cfg: dict[str, Any], kws: list[str]) -> int:
    """查当前账号在指定吧的等级/关注状态 —— 发帖被 273 拒绝时先看这个。"""
    client = build_client(cfg)
    rc = 0
    for i, kw in enumerate(kws):
        try:
            st = client.get_forum_status(kw)
        except TiebaError as e:
            log(f"{kw}吧 → 查询失败：{e}")
            rc = 1
            continue
        lv = st.get("user_level")
        liked = bool(st.get("is_like"))
        print(f"\n{st['kw']} 吧   (fid = {st.get('fid') or '?'})")
        print(f"  你的等级：{f'{lv} 级' if lv else '未知（未关注该吧）'}")
        print(f"  关注状态：{'已关注' if liked else '未关注'}"
              + ("，今天已签到" if st.get("signed") else ""))
        if st.get("cur_score") is not None:
            print(f"  吧内经验：{st['cur_score']}")

        if not st.get("fid") and lv is None:
            print("  发主题帖：查不到这个吧 —— 吧名可能写错了。"
                  "注意别带「吧」字，比如「孙笑川吧」要写成「孙笑川」")
        elif not liked:
            print("  发主题帖：你还没关注这个吧 —— 部分吧对未关注账号设限，建议先点关注")
        elif lv is not None and lv < 4:
            print(f"  发主题帖：{lv} 级偏低。多数吧要求 4 级，但这个吧未必；"
                  f"若被拒（273）再考虑养到 4 级")
        else:
            print("  发主题帖：可以发")

        if i < len(kws) - 1:
            time.sleep(random.uniform(1, 3))
    return rc


def cmd_sign(cfg: dict[str, Any], kws: list[str]) -> int:
    client = build_client(cfg)
    rc = 0
    for i, kw in enumerate(kws):
        try:
            log(f"{kw}吧 → {client.sign_forum(kw)}")
        except TiebaError as e:
            rc = 1
            log(f"{kw}吧 → 签到失败：{e}")
        if i < len(kws) - 1:
            time.sleep(random.uniform(2, 5))
    return rc


def cmd_template(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        log(f"{path} 已存在，未覆盖")
        return 1
    path.write_text(TEMPLATE, encoding="utf-8")
    log(f"模板已生成：{path}")
    print("\n编辑好正文后执行：python tieba_poster.py post " + str(path))
    return 0


TEMPLATE = """---
kw: python
title: 我写的一个小工具，分享给大家
# images: 1.png, 2.png       # 可选，纯文本发帖请留空
# sign: true                 # 可选，发帖前先给本吧签到
---

这里写正文。支持普通文本，也可以直接用 Markdown 习惯（贴吧不渲染 Markdown，
但纯文本排版照样好看）。

## 小标题可以这样写

1. 第一点
2. 第二点

正文超过 9000 字会自动拆成「主题帖 + 跟帖」，不用手动分段。
"""


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    # 公共参数：同时挂在主命令和子命令上，放在子命令前后都能识别。
    # 子命令里用 SUPPRESS 做默认值，避免把主命令已解析的值覆盖掉。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help="配置文件路径（默认 config.json）")
    common.add_argument("--cookie", default=argparse.SUPPRESS,
                        help="直接传 Cookie 串（也可用环境变量 TIEBA_COOKIE）")
    common.add_argument("--user-agent", dest="user_agent", default=argparse.SUPPRESS,
                        help="自定义 User-Agent")
    common.add_argument("--proxy", default=argparse.SUPPRESS,
                        help="HTTP 代理，如 http://127.0.0.1:7890")
    common.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="只预览，不实际发帖")
    common.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                        help="跳过发布前的人工确认")
    common.add_argument("--ignore-proxy", action="store_true", default=argparse.SUPPRESS,
                        help="忽略系统代理/环境变量，强制直连百度（网络超时时用）")

    common_kw = argparse.ArgumentParser(add_help=False, parents=[common])
    common_kw.add_argument("--kw", dest="default_kw", default=argparse.SUPPRESS,
                           help="默认吧名（帖子文件未指定时使用）")

    p = argparse.ArgumentParser(
        prog="tieba_poster",
        description="百度贴吧自动发帖 / 回复 / 签到工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python tieba_poster.py netcheck                         # 网络超时先跑这个\n"
            "  python tieba_poster.py check\n"
            "  python tieba_poster.py post posts/example.md --dry-run\n"
            "  python tieba_poster.py post posts/master --yes          # 批量发目录\n"
            "  python tieba_poster.py reply --kw python --tid 812345678 --text 顶\n"
            "  python tieba_poster.py sign --kw python --kw cpp\n"
            "  python tieba_poster.py forum --kw python              # 查自己的等级/关注\n"
            "  python tieba_poster.py check --ignore-proxy             # 强制直连，绕过代理\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"tieba_poster {__version__}")
    p.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json）")
    p.add_argument("--cookie", default="", help="直接传 Cookie 串（也可用环境变量 TIEBA_COOKIE）")
    p.add_argument("--user-agent", dest="user_agent", default="", help="自定义 User-Agent")
    p.add_argument("--proxy", default="", help="HTTP 代理，如 http://127.0.0.1:7890")
    p.add_argument("--kw", dest="default_kw", default="", help="默认吧名（帖子文件未指定时使用）")
    p.add_argument("--dry-run", action="store_true", help="只预览，不实际发帖")
    p.add_argument("--yes", action="store_true", help="跳过发布前的人工确认")
    p.add_argument("--ignore-proxy", action="store_true",
                   help="忽略系统代理/环境变量，强制直连百度（网络超时时用）")

    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("check", parents=[common], help="检查 Cookie 登录态")
    sub.add_parser("netcheck", parents=[common], help="诊断网络：超时是不是代理/DNS 的锅")

    sp = sub.add_parser("post", parents=[common_kw], help="发主题帖（文件或目录）")
    sp.add_argument("target", help="帖子文件（.md/.txt/.json）或目录")
    sp.add_argument("--upload-images", action="store_true", default=argparse.SUPPRESS,
                    help="尝试实验性上传本地图片")
    sp.add_argument("--sign", action="store_true", default=argparse.SUPPRESS,
                    help="发帖前顺带给该吧签到")

    sr = sub.add_parser("reply", parents=[common], help="回复某个帖子")
    sr.add_argument("--kw", dest="kw_reply", default="", help="吧名")
    sr.add_argument("--tid", required=True, help="帖子 tid（网址 /p/ 后面那串数字）")
    sr.add_argument("--text", default="", help="回复内容")
    sr.add_argument("--file", default="", help="从文件读取回复内容")

    ss = sub.add_parser("sign", parents=[common], help="贴吧签到")
    ss.add_argument("--kw", action="append", required=True, dest="kws", help="吧名，可重复多次")

    sf = sub.add_parser("forum", parents=[common],
                        help="查你在某吧的等级/关注状态（发帖被 273 拒绝时先看这个）")
    sf.add_argument("--kw", action="append", required=True, dest="kws", help="吧名，可重复多次")

    st = sub.add_parser("template", help="生成帖子文件模板")
    st.add_argument("path", help="输出路径，如 posts/new.md")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.cmd:
        parser.print_help()
        return 0

    try:
        if args.cmd == "template":
            return cmd_template(Path(args.path))

        cfg = load_config(_pick(Path(args.config)) if args.config else None, args)

        if args.cmd == "check":
            return cmd_check(cfg)
        if args.cmd == "netcheck":
            return cmd_netcheck(cfg)
        if args.cmd == "post":
            return cmd_post(cfg, _pick(Path(args.target)), args.yes)
        if args.cmd == "reply":
            kw = args.kw_reply or cfg.get("default_kw") or ""
            if not kw:
                raise TiebaError(-12, "请用 --kw 指定要回复哪个吧")
            text = args.text
            if args.file:
                text = Path(args.file).read_text(encoding="utf-8").strip()
            if not text:
                raise TiebaError(-12, "回复内容为空，请用 --text 或 --file 提供")
            return cmd_reply(cfg, kw, args.tid, text)
        if args.cmd == "sign":
            return cmd_sign(cfg, args.kws)
        if args.cmd == "forum":
            return cmd_forum(cfg, args.kws)
        parser.print_help()
        return 0
    except TiebaError as e:
        log(f"错误：{e}")
        return 2
    except KeyboardInterrupt:
        log("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
