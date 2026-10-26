#!/usr/bin/env python
"""只读：读指定吧的最新帖子列表（服务端渲染的移动吧页）。

为什么需要它：现有 tieba-poster 没有"读吧页列表"的命令，而 PLAN 2.1 规定
`list_recent_posts` 是查重依据、也是唯一的状态查证手段。没有它，整条实验链就断了。

为什么抓 /mo/q/m 而不是 /f?kw=：
    tieba_poster.py 第 264 行已经写明，新版 PC 吧页是 SPA 空壳，服务端不返回帖子；
    移动页 /mo/q/m 仍是服务端渲染的完整 HTML，且必须用桌面 UA 请求（脚本里已配好）。

这个脚本**只读不写**，不改 tieba-poster 任何文件，只 import 它的 client 复用 cookie/代理/重试。
必须用它自己的解释器跑（需要 requests）：

    cd C:/Users/15547/WorkBuddy/2026-10-22-17-21-40/tieba-poster
    .venv/Scripts/python.exe <本文件> --kw python --limit 20

输出一行 JSON：{"kw": "...", "posts": [{"title": "...", "post_id": "...", "pinned": false}]}
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_POSTER_DIR = Path("C:/Users/15547/WorkBuddy/2026-10-22-17-21-40/tieba-poster")
MOBILE_URL = "/mo/q/m"

_TAG_RE = re.compile(r"<[^>]+>")
_ICON_RE = re.compile(r'<span class="ti_title_icon[^"]*"[^>]*>.*?</span>', re.S)
_LI_RE = re.compile(r"<li\b([^>]*data-tid=\"(\d+)\"[^>]*)>(.*?)</li>", re.S)
_TITLE_RE = re.compile(r'class="ti_title"[^>]*>(.*?)</div>', re.S)


def _clean(fragment: str) -> str:
    text = _ICON_RE.sub("", fragment)          # 去掉「置顶 / 精品」小图标里的文字
    text = _TAG_RE.sub("", text)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_threads(html: str, limit: int = 20) -> list[dict[str, Any]]:
    """从移动吧页 HTML 抽出帖子列表。

    只认带 data-tid 的 <li>，天然过滤掉广告位和推荐位（它们没有 tid）。
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
    """tieba_poster.load_config 需要一个带这些属性的 namespace（未提供 CLI 覆盖）。"""
    return argparse.Namespace(
        cookie="", user_agent="", proxy="", default_kw="",
        dry_run=False, yes=False, ignore_proxy=False,
    )


def fetch(kw: str, limit: int, poster_dir: Path, config: str) -> dict[str, Any]:
    sys.path.insert(0, str(poster_dir))
    import tieba_poster as tp  # noqa: PLC0415 - 必须延后导入

    cfg_path = Path(config)
    if not cfg_path.is_absolute():
        cfg_path = poster_dir / cfg_path
    cfg = tp.load_config(cfg_path, _namespace())
    client = tp.build_client(cfg)

    resp = client._request("GET", tp.BASE + MOBILE_URL, params={"kw": kw})
    page = resp.content.decode("utf-8", "ignore")
    if "百度安全验证" in page or "wappass" in str(resp.url):
        raise SystemExit(json.dumps({"error": "风控拦截（百度安全验证），停止本次读取"}, ensure_ascii=False))
    return {"kw": kw, "posts": parse_threads(page, limit)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="只读：读吧页最新帖子列表")
    ap.add_argument("--kw", required=True, help="吧名，不带「吧」字")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--poster-dir", default=str(DEFAULT_POSTER_DIR))
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args(argv)

    try:
        payload = fetch(args.kw, args.limit, Path(args.poster_dir), args.config)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 一律以 JSON 报错，便于上层解析
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
