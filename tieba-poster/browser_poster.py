#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_poster.py —— 用真实浏览器（Edge + CDP）发帖

为什么要走浏览器：
    新版贴吧发帖接口 /c/c/thread/add_pc 要求三样由前端 JS 实时生成的字段
    —— sign（MD5）、jt（加密串）、Acs-Token（请求头）。
    这些东西纯 requests 复现不了（实测 46 种签名组合全部不命中）。
    让真实浏览器去点按钮，签名由页面自己算，风控就绕过去了。

为什么不用 selenium：
    本机的 msedgedriver 155 有 bug，任何导航都返回 `unhandled request`
    （连 about:blank 都失败，与网络、代理、沙箱都无关）。所以直接讲 CDP 协议。

用法：
    python browser_poster.py probe --kw 孙笑川      # 探测页面元素（调试用）
    python browser_poster.py post posts/xxx.md     # 发帖
    python browser_poster.py post posts/           # 发一个目录里所有帖子
    加 --headless 可以隐藏窗口；加 --keep-open 发完不关浏览器
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

try:
    from websocket import create_connection
except ImportError:
    sys.exit("缺少 websocket-client，请先执行："
             ".venv/Scripts/pip.exe install websocket-client")

try:
    import tieba_poster as tp
except ImportError:
    sys.exit("找不到 tieba_poster.py，请在 tieba-poster 目录下运行")

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass


HERE = Path(__file__).resolve().parent


def _pick(p: Path) -> Path:
    """路径在「当前目录」下不存在时，退回到脚本所在目录再找一次。

    这样就算你站在别的目录里跑，`config.json` 和 `posts/xxx.md`
    也能被正确解析，不用非得先 cd 过去。
    """
    if p.is_absolute() or p.exists():
        return p
    alt = HERE / p
    return alt if alt.exists() else p


# =========================================================== Edge + CDP 底座

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    got = shutil.which("msedge")
    if got:
        return got
    raise SystemExit("找不到 Edge。请确认已安装 Microsoft Edge。")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Edge:
    """极简 CDP 客户端：启动 Edge → 连浏览器级 websocket → 操作一个页面。"""

    def __init__(self, headless: bool = False, profile_dir: str | None = None,
                 timeout: float = 40.0):
        self.headless = headless
        self.tmp_profile = None
        if profile_dir:
            self.profile = profile_dir
        else:
            self.tmp_profile = tempfile.mkdtemp(prefix="tieba_edge_")
            self.profile = self.tmp_profile

        self.port = free_port()
        args = [
            find_edge(),
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=msEdgeFirstRunExperience",
            "--disable-infobars",
            "--window-size=1440,900",
            "about:blank",
        ]
        if headless:
            args.insert(1, "--headless=new")
        else:
            args.insert(1, "--start-maximized")

        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.ws = None
        self.sid = None
        self._id = 0
        self.timeout = timeout
        self.events: list[dict] = []      # 缓冲收到的事件（网络、日志等）
        self._connect()

    # ---- 连接
    def _connect(self) -> None:
        ws_url = None
        for _ in range(80):
            if self.proc.poll() is not None:
                raise SystemExit("Edge 启动后就退出了，"
                                 "检查是否有残留进程占着配置目录")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version", timeout=1) as r:
                    ws_url = json.loads(r.read().decode())["webSocketDebuggerUrl"]
                break
            except Exception:
                time.sleep(0.3)
        if not ws_url:
            raise SystemExit("Edge 调试端口没起来，可能被安全软件拦了")

        self.ws = create_connection(ws_url, timeout=self.timeout,
                                    max_size=128 * 1024 * 1024,
                                    suppress_origin=True)
        tid = self._browser_cmd("Target.createTarget",
                                {"url": "about:blank"})["targetId"]
        self.sid = self._browser_cmd("Target.attachToTarget",
                                     {"targetId": tid, "flatten": True})["sessionId"]
        for m in ("Page.enable", "Network.enable", "Runtime.enable"):
            self.raw(m)

    # ---- 收发
    def _send(self, mid: int, payload: dict, wait: bool = True):
        self.ws.send(json.dumps(payload))
        if not wait:
            return None
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{payload['method']} 失败：{msg['error']}")
                return msg.get("result", {})
            if "method" in msg:                 # 事件，先存起来
                self.events.append(msg)

    def _browser_cmd(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        return self._send(self._id, {"id": self._id, "method": method,
                                     "params": params or {}})

    def raw(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        return self._send(self._id, {"id": self._id, "method": method,
                                     "params": params or {},
                                     "sessionId": self.sid})

    def pump(self, seconds: float = 0.4) -> None:
        """空转一会儿，把路上排队的事件收进缓冲。"""
        end = time.time() + seconds
        self.ws.settimeout(0.15)
        try:
            while time.time() < end:
                try:
                    msg = json.loads(self.ws.recv())
                    if "method" in msg:
                        self.events.append(msg)
                except Exception:
                    pass
        finally:
            self.ws.settimeout(self.timeout)

    def wait_event(self, keyword: str, timeout: float = 25,
                   method: str = "Network.responseReceived") -> dict | None:
        """在事件缓冲里等一条 URL 含 keyword 的响应事件。"""
        end = time.time() + timeout
        while time.time() < end:
            for ev in self.events:
                if ev.get("method") != method:
                    continue
                p = ev.get("params", {})
                resp = p.get("response") or {}
                if keyword in (resp.get("url") or ""):
                    return p
            self.pump(0.3)
        return None

    # ---- 常用动作
    def set_cookies(self, jar: dict[str, str]) -> int:
        cookies = [{"name": k, "value": v, "domain": ".baidu.com",
                    "path": "/", "secure": True} for k, v in jar.items()]
        self.raw("Network.setCookies", {"cookies": cookies})
        return len(cookies)

    def goto(self, url: str, timeout: float = 30) -> None:
        self.raw("Page.navigate", {"url": url})
        self.wait_js("document.readyState === 'complete'", timeout=timeout)

    def eval(self, expr: str):
        r = self.raw("Runtime.evaluate", {"expression": expr,
                                          "returnByValue": True,
                                          "awaitPromise": True})
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError("JS 报错："
                               + str((d.get("exception") or {}).get("description")
                                     or d.get("text")))
        return r.get("result", {}).get("value")

    def wait_js(self, expr: str, timeout: float = 20, interval: float = 0.3) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.eval(f"!!({expr})"):
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def rect(self, js_el: str):
        # 用普通拼接而不是 f-string，避免大括号转义出错
        return self.eval(
            "(function(){var el=" + js_el + ";"
            "if(!el)return null;"
            "el.scrollIntoView({block:'center'});"
            "var r=el.getBoundingClientRect();"
            "return {x:r.x+r.width/2,y:r.y+r.height/2,w:r.width,h:r.height};"
            "})()")

    def click_at(self, x: float, y: float) -> None:
        for t in ("mousePressed", "mouseReleased"):
            self.raw("Input.dispatchMouseEvent",
                     {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})

    def click(self, js_el: str, desc: str = "元素", timeout: float = 12) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            r = self.rect(js_el)
            if r and r["w"] > 1 and r["h"] > 1:
                self.click_at(r["x"], r["y"])
                return True
            time.sleep(0.3)
        print(f"    ! 没找到可点的{desc}")
        return False

    def close(self, keep: bool = False) -> None:
        try:
            self.ws and self.ws.close()
        except Exception:
            pass
        if not keep:
            try:
                self.proc.terminate()
            except Exception:
                pass
            if self.tmp_profile:
                time.sleep(1.0)
                shutil.rmtree(self.tmp_profile, ignore_errors=True)


# =========================================================== 页面元素定位

# 标题框 / 正文框：都是 Quill 编辑器，靠 data-placeholder 区分
SEL_TITLE = ("[...document.querySelectorAll('.ql-editor')]"
             ".find(x=>(x.getAttribute('data-placeholder')||'').includes('标题'))")
SEL_BODY = ("[...document.querySelectorAll('.ql-editor')]"
            ".find(x=>(x.getAttribute('data-placeholder')||'').includes('正文'))")
SEL_PUBLISH_ENTRY = "document.querySelector('.operate-btn.publish')"
SEL_SUBMIT = ("document.querySelector('.issue-btn')"
              "|| [...document.querySelectorAll('div,button')]"
              ".find(x=>x.innerText.trim()==='发布'&&x.getBoundingClientRect().width>1)")

LOGIN_JS = r"""
(async () => {
  try {
    const r = await fetch('https://tieba.baidu.com/f/user/json_userinfo',
                          {credentials: 'include'});
    const j = await r.json();
    const d = (j && j.data) || {};
    return {no: j && j.no, user: d.user_name || d.show_nickname || '',
            uid: d.user_id || d.open_uid || ''};
  } catch (e) { return {err: String(e)}; }
})()
"""

TOAST_JS = r"""
(() => {
  const keep = /成功|失败|错误|频繁|审核|等级|验证码|不能|无法|请|稍后|重复/;
  const out = [];
  document.querySelectorAll(
    '[class*=toast],[class*=Toast],[class*=message],[class*=Message],[class*=tip]'
  ).forEach(e => {
    const t = (e.innerText || '').trim().replace(/\s+/g, ' ');
    const r = e.getBoundingClientRect();
    if (t && t.length <= 40 && r.width > 1 && r.height > 1 && keep.test(t)) out.push(t);
  });
  return [...new Set(out)].slice(0, 5);
})()
"""


def check_login(e: Edge) -> dict:
    """必须在贴吧域下才能带上 Cookie 请求，所以先确保站在 tieba 上。"""
    try:
        host = e.eval("location.hostname") or ""
    except Exception:
        host = ""
    if "tieba.baidu.com" not in host:
        try:
            e.goto("https://tieba.baidu.com/")
        except Exception as ex:
            return {"err": f"打开贴吧失败：{ex}"}
    try:
        return e.eval(LOGIN_JS) or {}
    except Exception as ex:
        return {"err": str(ex)}


def open_editor(e: Edge, kw: str) -> tuple[bool, str]:
    """打开吧页 → 点「发贴」→ 等编辑器出现。"""
    url = f"https://tieba.baidu.com/f?kw={quote(kw)}"
    e.goto(url)
    if not e.wait_js(SEL_PUBLISH_ENTRY, timeout=25):
        return False, "吧页没加载出「发贴」按钮（可能吧名写错 / 页面改版）"
    time.sleep(1.2)
    if not e.click(SEL_PUBLISH_ENTRY, "「发贴」按钮"):
        return False, "点不到「发贴」按钮"
    if not e.wait_js(f"{SEL_TITLE} && {SEL_BODY}", timeout=20):
        return False, "编辑器没弹出来"
    time.sleep(0.8)
    return True, ""


def fill_editor(e: Edge, sel: str, text: str, desc: str) -> bool:
    """点进编辑区，用真实输入事件打字（Quill 只认真实输入）。"""
    r = e.rect(sel)
    if not r:
        print(f"    ! 找不到{desc}输入框")
        return False
    e.click_at(r["x"], r["y"])
    time.sleep(0.3)
    e.raw("Input.insertText", {"text": text})
    time.sleep(0.6)
    got = e.eval(f"(()=>{{const el={sel};return el?el.innerText.trim():''}})()")
    if not got:
        print(f"    ! {desc}没填进去")
        return False
    return True


def read_add_response(e: Edge, req_id: str) -> dict | None:
    """按 requestId 取响应体。响应没下载完时 getResponseBody 会报错，所以重试几次。"""
    for _ in range(8):
        try:
            r = e.raw("Network.getResponseBody", {"requestId": req_id})
            body = r.get("body") or ""
            if r.get("base64Encoded"):
                import base64
                body = base64.b64decode(body).decode("utf-8", "ignore")
            data = json.loads(body)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        time.sleep(0.4)
        e.pump(0.2)
    return None


def forum_has_title(client, kw: str, title: str,
                    tries: int = 7, gap: float = 2.0) -> str | None:
    """回查吧页（移动版 SSR）确认帖子真的上线了。

    返回 tid 字符串表示找到了；返回 "" 表示页面里有标题但没解析出 tid；
    返回 None 表示没找到。
    """
    for _ in range(tries):
        try:
            client._page_cache.clear()          # 必须清缓存，不然读的还是旧页面
            html = client._forum_html(kw)
        except Exception:
            html = ""
        idx = html.find(title) if html else -1
        if idx >= 0:
            seg = html[max(0, idx - 900):idx]
            tids = re.findall(r'data-tid="(\d+)"', seg)
            return tids[-1] if tids else ""
        time.sleep(gap)
    return None


def publish_one(e: Edge, client, kw: str, title: str, content: str,
                max_len: int = 2000) -> tuple[bool, str]:
    """发一条主题帖。返回 (是否成功, 说明)。"""
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        return False, "标题为空"
    if len(title) > 31:
        print(f"    · 标题超 31 字，已截断（原 {len(title)} 字）")
        title = title[:31]
    if not content:
        return False, "正文为空"
    if len(content) > max_len:
        print(f"    · 正文 {len(content)} 字超限，只发前 {max_len} 字")
        content = content[:max_len]

    e.events.clear()
    ok, why = open_editor(e, kw)
    if not ok:
        return False, why

    if not fill_editor(e, SEL_TITLE, title, "标题"):
        return False, "标题填写失败"
    if not fill_editor(e, SEL_BODY, content, "正文"):
        return False, "正文填写失败"

    before_url = e.eval("location.href")
    if not e.click(SEL_SUBMIT, "「发布」按钮"):
        return False, "点不到「发布」按钮"

    # ① 首选：抓 add_pc 接口的响应
    ev = e.wait_event("/c/c/thread/add_pc", timeout=25)
    if ev is None:
        ev = e.wait_event("thread/add", timeout=3)
    data = {}
    if ev:
        data = read_add_response(e, ev.get("requestId")) or {}

    time.sleep(0.8)
    toasts = e.eval(TOAST_JS) or []

    if data:
        no = data.get("no", data.get("err_code"))
        if str(no) == "0":
            d = data.get("data") if isinstance(data.get("data"), dict) else {}
            tid = d.get("tid") or d.get("thread_id") or ""
            return True, (f"https://tieba.baidu.com/p/{tid}" if tid else "接口返回成功")
        msg = (data.get("error") or data.get("error_msg")
               or data.get("errmsg") or "未知错误")
        code = no
        hint = tp.POST_ERROR_HINTS.get(code)
        if hint is None and isinstance(code, str) and code.isdigit():
            hint = tp.POST_ERROR_HINTS.get(int(code))
        txt = f"[{code}] {msg}"
        if hint:
            txt += f"（{hint}）"
        if toasts:
            txt += "\n      页面提示：" + " / ".join(toasts)
        return False, txt

    # ② 接口没抓到：回查吧页，确认帖子是否真的上线（最可靠的独立判据）
    tid = forum_has_title(client, kw, title)
    if tid is not None:
        link = f"https://tieba.baidu.com/p/{tid}" if tid else "已出现在吧内列表"
        return True, f"（回查确认）{link}"

    # ③ 最后看页面提示
    if any("成功" in t for t in toasts):
        return True, "页面提示发送成功，但吧页还没刷出来（可能在审核）"
    if toasts:
        return False, "页面提示：" + " / ".join(toasts)
    now_url = e.eval("location.href")
    if now_url != before_url and "/p/" in now_url:
        return True, now_url
    return False, "既没抓到接口，吧页也查不到该帖，可能被风控静默拦截"


# =========================================================== 子命令

def cmd_probe(e: Edge, kw: str) -> int:
    e.goto("https://tieba.baidu.com/")
    print("\n=== 登录态 ===")
    print("  ", check_login(e))

    ok, why = open_editor(e, kw)
    print(f"\n=== 打开 {kw} 吧的发帖编辑器 ===")
    if not ok:
        print("  失败：", why)
        return 1
    print("  编辑器已弹出")
    print("  标题框 :", "已找到" if e.eval(f"!!({SEL_TITLE})") else "没找到")
    print("  正文框 :", "已找到" if e.eval(f"!!({SEL_BODY})") else "没找到")
    print("  发布键 :", "已找到" if e.eval(f"!!({SEL_SUBMIT})") else "没找到")
    return 0


def collect(target: Path) -> list:
    if target.is_file():
        return tp.load_posts(target)
    files = tp.collect_post_files(target)
    out = []
    for f in files:
        out.extend(tp.load_posts(f))
    return out


def cmd_post(e: Edge, cfg: dict, target: Path, assume_yes: bool) -> int:
    if not target.exists():
        print(f"找不到 {target}")
        return 1
    posts = collect(target)
    if not posts:
        print("没有可发的帖子")
        return 1

    print("\n===== 待发布清单 =====")
    for i, p in enumerate(posts, 1):
        body = p.content.replace("\n", " ")
        print(f"{i}. 吧名：{p.kw}  标题：{p.title}")
        print(f"   正文 {len(p.content)} 字 / {len(p.content.splitlines())} 段"
              f"   {body[:40]}{'…' if len(body) > 40 else ''}")
        if p.images:
            print(f"   ! 带 {len(p.images)} 张图，浏览器方案暂不传图，会被忽略")
    print("======================")

    if not assume_yes:
        try:
            ans = input("确认发布？输入 y 回车：").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            print("已取消")
            return 0

    info = check_login(e)
    if str(info.get("no")) != "0":
        print(f"登录态无效：{info}")
        return 1
    print(f"\n以 {info.get('user')} 身份发帖（浏览器：{'headless' if e.headless else '窗口'}）")

    # 回查吧页要用 requests 客户端（移动版 SSR），单独建一个
    client = tp.TiebaClient(cfg["cookie"],
                            trust_env=not cfg.get("ignore_proxy"),
                            timeout=int(cfg.get("timeout", 20)))

    ok_n = fail_n = 0
    for i, p in enumerate(posts, 1):
        t0 = time.time()
        print(f"\n[{time.strftime('%H:%M:%S')}] [{i}/{len(posts)}] "
              f"{p.kw}吧 · {p.title}")
        try:
            ok, msg = publish_one(e, client, p.kw, p.title, p.content)
        except Exception as ex:
            ok, msg = False, f"{type(ex).__name__}: {ex}"
        dur = time.time() - t0
        if ok:
            ok_n += 1
            print(f"    成功（{dur:.1f}s）{msg}")
        else:
            fail_n += 1
            print(f"    失败（{dur:.1f}s）：{msg}")
        if i < len(posts):
            time.sleep(2)

    print(f"\n[{time.strftime('%H:%M:%S')}] 完成：成功 {ok_n} 条，失败 {fail_n} 条")
    return 0 if fail_n == 0 else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="用浏览器发贴（绕过 JS 签名风控）")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--headless", action="store_true",
                    help="无界面模式（默认显示窗口，方便你看它在干什么）")
    ap.add_argument("--keep-open", action="store_true", help="结束后不关浏览器")
    ap.add_argument("--profile", default=None,
                    help="指定浏览器配置目录（默认每次用临时目录）")
    sub = ap.add_subparsers(dest="cmd")

    sp = sub.add_parser("probe", help="探测页面元素，看发帖控件长什么样")
    sp.add_argument("--kw", required=True, help="吧名")

    st = sub.add_parser("post", help="发帖")
    st.add_argument("target", help="帖子文件或目录")
    st.add_argument("--yes", action="store_true", help="跳过确认")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 0

    cfg_path = _pick(Path(args.config))
    if not cfg_path.exists():
        print(f"找不到 {args.config}")
        print(f"  当前目录：{Path.cwd()}")
        print(f"  脚本目录：{HERE}")
        print(f"  请先切到脚本目录再运行：cd \"{HERE}\"")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cookie = (cfg.get("cookie") or "").strip()
    if not cookie:
        print("config.json 里没有 cookie，先跑：python setup_cookie.py")
        return 1

    print("启动 Edge ...")
    e = Edge(headless=args.headless, profile_dir=args.profile)
    code = 1
    try:
        n = e.set_cookies(tp.parse_cookie(cookie))
        print(f"已注入 {n} 条 Cookie")
        if args.cmd == "probe":
            code = cmd_probe(e, args.kw)
        else:
            code = cmd_post(e, cfg, _pick(Path(args.target)), args.yes)
    finally:
        if args.keep_open:
            try:
                input("\n按回车关闭浏览器 ...")
            except EOFError:
                pass
        e.close(keep=args.keep_open)
    return code


if __name__ == "__main__":
    sys.exit(main())
