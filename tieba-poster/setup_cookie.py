#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_cookie.py —— 交互式填写 Cookie，并立刻验证登录态

用法：
    python setup_cookie.py
    python setup_cookie.py --test "BDUSS=...; STOKEN=..."
    python setup_cookie.py --config 别的配置.json

它做四件事：
    1. 读现有 config.json（没有就从 config.example.json 复制一份）
    2. 让你把浏览器复制的 Cookie 整串粘进来
    3. 检查 BDUSS / STOKEN / BAIDUID 在不在，缺了就明确告诉你
    4. 写回 config.json，并马上请求百度验证登录态，打印你的昵称
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import tieba_poster as tp
except ImportError:
    sys.exit("找不到 tieba_poster.py，请在 tieba-poster 目录下运行本脚本")

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

REQUIRED = {"BDUSS": "登录凭证，没有它一定失败"}
RECOMMENDED = {
    "STOKEN": "发帖/回复必须",
    "BAIDUID": "设备标识，缺了更容易被风控拦",
    "BDUSS_BFESS": "风控专用，建议一起带上",
    "BAIDUID_BFESS": "风控专用，建议一起带上",
}


def load_cfg(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            sys.exit(f"配置文件 {path} 不是合法 JSON：{e}\n请先修好它，或删掉让它重新生成")
    example = Path(__file__).with_name("config.example.json")
    if example.exists():
        print(f"没找到 {path.name}，已按 config.example.json 生成一份")
        return json.loads(example.read_text(encoding="utf-8"))
    return dict(tp.DEFAULT_CONFIG)


def report(jar: dict[str, str]) -> bool:
    ok = True
    print("\n检查 Cookie 内容：")
    for key, why in REQUIRED.items():
        if key in jar and jar[key]:
            print(f"  [有] {key:16} 长度 {len(jar[key]):3}  ({why})")
        else:
            print(f"  [缺] {key:16} 缺失！{why}")
            ok = False
    for key, why in RECOMMENDED.items():
        if key in jar and jar[key]:
            print(f"  [有] {key:16} 长度 {len(jar[key]):3}")
        else:
            print(f"  [无] {key:16} 没有（{why}）")
    extra = set(jar) - set(REQUIRED) - set(RECOMMENDED)
    if extra:
        print(f"  另外还有 {len(extra)} 条其他 cookie，会一起保存")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="交互式填写并验证贴吧 Cookie")
    ap.add_argument("--config", default="config.json", help="配置文件路径（默认 config.json）")
    ap.add_argument("--test", default="", help="直接传 Cookie 串，不走交互输入")
    ap.add_argument("--no-verify", action="store_true", help="只写入，不联网验证")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = load_cfg(cfg_path)

    raw = args.test
    if not raw:
        if cfg.get("cookie"):
            print(f"\n{cfg_path.name} 里已经有一串 Cookie 了，继续操作会覆盖它。")
            print("（直接回车 = 保留原来的，只做一次登录验证）")
        print("\n请把浏览器里复制的 Cookie 整串粘贴到这里，然后回车。")
        print("（要包含 BDUSS=... 一直到串尾，中间不要换行）\n")
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return 1
        if not raw:
            raw = cfg.get("cookie", "")
            if not raw:
                print("没有输入内容，退出")
                return 1
            print("使用配置里原有的 Cookie 继续验证")

    jar = tp.parse_cookie(raw)
    if not jar:
        print("这段内容里解析不出任何 cookie（应该是 名字=值; 名字=值 的形式）")
        return 1

    if not report(jar):
        print("\n先别急着保存 —— BDUSS 缺失几乎一定是复制方式不对：")
        print("  不要在 Console 里敲 document.cookie（BDUSS 是 HttpOnly，拿不到）")
        print("  请到 Network → 某个请求 → 标头 → Request Headers → Cookie → 复制整行")
        return 1

    cleaned = "; ".join(f"{k}={v}" for k, v in jar.items())
    cfg["cookie"] = cleaned
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已写入 {cfg_path}（{len(jar)} 条 cookie）")

    if args.no_verify:
        return 0

    print("\n正在验证登录态 ...")
    try:
        client = tp.TiebaClient(cleaned, timeout=int(cfg.get("timeout", 20)),
                                proxy=cfg.get("proxy") or None,
                                trust_env=not bool(cfg.get("ignore_proxy")))
        info = client.get_user_info()
        client.get_tbs()
        print(f"  登录成功：{info.get('user_name')}（uid={info.get('user_id', '?')}）")
        print("\n可以开始发帖了：")
        print('  python tieba_poster.py post "posts/我的第一个帖子.md" --dry-run')
        return 0
    except tp.TiebaError as e:
        if e.code == -2:
            print(f"  网络没通：{e}")
            print("\n这是网络问题，不是 Cookie 问题（Cookie 已经存好了，不用重新复制）。")
            print("  下一步：python tieba_poster.py netcheck")
            print("  如果诊断显示「走代理失败、直连正常」，就加 --ignore-proxy 重跑：")
            print("    python tieba_poster.py check --ignore-proxy")
            return 2
        print(f"  验证失败：{e}")
        print("\n常见原因：")
        print("  1. 串没复制全（值被表格截断了）—— 重新从「标头」面板复制整行")
        print("  2. Cookie 已过期 —— 重新登录贴吧后再复制")
        print("  3. 复制到了 Response Cookies 而不是 Request Cookies")
        print(f"  Cookie 已写入 {cfg_path}，修正后重跑本脚本即可覆盖")
        return 2


if __name__ == "__main__":
    sys.exit(main())
