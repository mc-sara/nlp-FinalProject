#!/usr/bin/env python
"""只读探针：登录态 / 吧内等级 / 吧页帖子列表，全部输出一行 JSON。

为什么要单独一个文件：现有 tieba-poster 没有"读吧页列表"的命令，而 PLAN 2.1 规定
`list_recent_posts` 是查重依据、也是唯一的状态查证手段。没有它整条实验链就断了。

为什么抓 /mo/q/m 而不是 /f?kw=：tieba_poster.py 第 264 行已写明新版 PC 吧页是 SPA 空壳，
服务端不返回帖子；移动页 /mo/q/m 仍是服务端渲染的完整 HTML（必须用桌面 UA，脚本已配好）。

**只读不写**，不改 tieba-poster 任何文件，只 import 它的 client 复用 cookie / 代理 / 重试。
必须用它的解释器跑（需要 requests）：

    cd C:/Users/15547/WorkBuddy/2026-10-22-17-21-40/tieba-poster
    .venv/Scripts/python.exe <本文件> login
    .venv/Scripts/python.exe <本文件> level --kw python
    .venv/Scripts/python.exe <本文件> list  --kw python --limit 20
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_POSTER_DIR = Path("C:/Users/15547/WorkBuddy/2026-10-22-17-21-40/tieba-poster")
MOBILE_URL = "/mo/q/m"
DETAIL_URL = "/p/"
PROFILE_URL = "/home/main"

# 风控页特征：命中任一就停止本次实验（PLAN 6.2），不要靠猜。
_RISK_MARKERS = ("百度安全验证", "wappass", "验证码", "访问过于频繁", "captcha")

_DETAIL_MISSING = ("贴子不存在", "帖子不存在", "该主题已被删除", "已删除", "抱歉，您访问的贴子")
_PAGE_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)

_TAG_RE = re.compile(r"<[^>]+>")
_ICON_RE = re.compile(r'<span class="ti_title_icon[^"]*"[^>]*>.*?</span>', re.S)
_LI_RE = re.compile(r"<li\b([^>]*data-tid=\"(\d+)\"[^>]*)>(.*?)</li>", re.S)
_TITLE_RE = re.compile(r'class="ti_title"[^>]*>(.*?)</div>', re.S)


def _clean(fragment: str) -> str:
    text = _ICON_RE.sub("", fragment)          # 去掉「置顶 / 精品」小图标里的文字
    text = _TAG_RE.sub("", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _save_raw(raw_out: str | Path | None, name: str, body: str) -> str | None:
    """把原始页面落盘，返回路径。不传目录就不写（默认零副作用）。"""
    if not raw_out:
        return None
    directory = Path(raw_out)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.html"
    path.write_text(body, encoding="utf-8", errors="replace")
    return str(path)


def _check_risk(page: str, url: str = "") -> None:
    joined = page + url
    for marker in _RISK_MARKERS:
        if marker in joined:
            raise RuntimeError(f"风控拦截（{marker}），请停止本次实验")


def _page_title(page: str) -> str:
    match = _PAGE_TITLE_RE.search(page)
    return _clean(match.group(1)) if match else ""


def parse_threads(html: str, limit: int = 20) -> list[dict[str, Any]]:
    """从移动吧页 HTML 抽出帖子列表。

    只认带 data-tid 的 <li>，天然过滤广告位和推荐位（它们没有 tid）。
    顺序即页面顺序：置顶在前，其后大致按最后回复时间。
    """
    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for attrs, tid, body in _LI_RE.findall(html):
        if tid in seen:
            continue
        match = _TITLE_RE.search(body)
        if not match:
            continue
        title = _clean(match.group(1))
        if not title:
            continue
        seen.add(tid)
        posts.append(
            {
                "post_id": tid,
                "title": title,
                "pinned": "tl_top" in attrs or "ti_icon_zhiding" in body,
            }
        )
        if len(posts) >= max(1, limit):
            break
    return posts


def _namespace() -> argparse.Namespace:
    """tieba_poster.load_config 需要一个带这些属性的 namespace（此处不传 CLI 覆盖）。"""
    return argparse.Namespace(
        cookie="", user_agent="", proxy="", default_kw="",
        dry_run=False, yes=False, ignore_proxy=False,
    )


def build_client(poster_dir: Path, config: str):
    sys.path.insert(0, str(poster_dir))
    import tieba_poster as tp  # noqa: PLC0415 - 必须延后导入

    cfg_path = Path(config)
    if not cfg_path.is_absolute():
        cfg_path = poster_dir / cfg_path
    return tp, tp.build_client(tp.load_config(cfg_path, _namespace()))


def do_login(tp, client) -> dict[str, Any]:
    info = client.get_user_info()
    tbs = client.get_tbs()
    return {
        "ok": True,
        "user": info.get("user_name") or "",
        "uid": str(info.get("user_id") or ""),
        "tbs_ok": bool(tbs),
    }


def do_level(tp, client, kw: str) -> dict[str, Any]:
    status = client.get_forum_status(kw)
    return {
        "kw": status.get("kw", kw),
        "level": status.get("user_level"),
        "liked": bool(status.get("is_like")),
        "fid": status.get("fid") or "",
        "signed": bool(status.get("signed")),
    }


def do_list(
    tp, client, kw: str, limit: int, *, nocache: bool = False, raw_out: str | Path | None = None
) -> dict[str, Any]:
    """读吧页帖子列表。

    `nocache=True` 时给请求加上时间戳与 `no-cache` 头，绕开 CDN / 中间缓存 ——
    刚发的帖子有时在缓存副本里看不到，回查就会出现「明明发出去了却查不到」。
    `raw_out` 给了目录就把原始 HTML 落盘，路径回填到 `raw_file`。
    """
    params: dict[str, Any] = {"kw": kw}
    extra: dict[str, Any] = {}
    if nocache:
        params["_t"] = str(int(time.time() * 1000))
        extra["headers"] = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
    resp = client._request("GET", tp.BASE + MOBILE_URL, params=params, **extra)
    page = resp.content.decode("utf-8", "ignore")
    _check_risk(page, str(resp.url))
    return {
        "kw": kw,
        "posts": parse_threads(page, limit),
        "raw_file": _save_raw(raw_out, "list", page),
    }


def do_detail(tp, client, post_id: str, raw_out: str | Path | None = None) -> dict[str, Any]:
    """帖子详情页：判断「真的存在 / 已被删」，与吧页列表互相独立的一个证据源。"""
    resp = client._request("GET", tp.BASE + DETAIL_URL + str(post_id))
    page = resp.content.decode("utf-8", "ignore")
    _check_risk(page, str(resp.url))
    missing = any(marker in page for marker in _DETAIL_MISSING)
    return {
        "post_id": str(post_id),
        "http_status": int(getattr(resp, "status_code", 0) or 0),
        "exists": bool(getattr(resp, "status_code", 200) == 200 and not missing),
        "deleted_hint": missing,
        "title": _page_title(page),
        "raw_file": _save_raw(raw_out, f"detail-{post_id}", page),
    }


def do_profile(
    tp, client, uname: str = "", post_id: str = "", raw_out: str | Path | None = None
) -> dict[str, Any]:
    """个人主页：看帖子是否出现在自己的发帖记录里（吧页看不到时的第二证据源）。

    不解析整页结构（页面改版频繁），只回答一个二值问题：pid 在不在这一页里。
    """
    params: dict[str, Any] = {}
    if uname:
        params["un"] = uname
    resp = client._request("GET", tp.BASE + PROFILE_URL, params=params)
    page = resp.content.decode("utf-8", "ignore")
    _check_risk(page, str(resp.url))
    return {
        "uname": uname,
        "http_status": int(getattr(resp, "status_code", 0) or 0),
        "contains_post": bool(post_id) and str(post_id) in page,
        "html_len": len(page),
        "raw_file": _save_raw(raw_out, "profile", page),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="只读探针：登录态 / 等级 / 吧页列表 / 详情 / 主页")
    ap.add_argument("--poster-dir", default=str(DEFAULT_POSTER_DIR))
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--raw-out", default="", help="把原始页面写到这个目录（默认不写）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="校验 Cookie 登录态")
    sp_level = sub.add_parser("level", help="查吧内等级与关注状态")
    sp_level.add_argument("--kw", required=True)
    sp_list = sub.add_parser("list", help="读吧页最新帖子列表")
    sp_list.add_argument("--kw", required=True)
    sp_list.add_argument("--limit", type=int, default=20)
    sp_detail = sub.add_parser("detail", help="读帖子详情页，判断是否还在")
    sp_detail.add_argument("--post-id", required=True)
    sp_profile = sub.add_parser("profile", help="读个人主页，看帖子是否出现在发帖记录里")
    sp_profile.add_argument("--uname", default="")
    sp_profile.add_argument("--post-id", default="")

    args = ap.parse_args(argv)
    raw_out = args.raw_out or None

    try:
        tp, client = build_client(Path(args.poster_dir), args.config)
        if args.cmd == "login":
            payload = do_login(tp, client)
        elif args.cmd == "level":
            payload = do_level(tp, client, args.kw)
        elif args.cmd == "detail":
            payload = do_detail(tp, client, args.post_id, raw_out=raw_out)
        elif args.cmd == "profile":
            payload = do_profile(tp, client, args.uname, args.post_id, raw_out=raw_out)
        else:
            payload = do_list(tp, client, args.kw, args.limit, raw_out=raw_out)
    except Exception as exc:  # noqa: BLE001 - 一律以 JSON 报错，便于上层解析
        code = getattr(exc, "code", None)
        payload = {"error": f"{type(exc).__name__}: {exc}"}
        if code is not None:
            payload["code"] = code
        print(json.dumps(payload, ensure_ascii=False))
        return 1

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
