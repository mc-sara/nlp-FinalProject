"""集群侧模型服务：把训练好的 adapter 暴露成 HTTP /act，供 Mac 端的 agent 循环调用。

本机跑不动 9B（8 GB 内存），所以真实确认实验把推理留在集群、浏览器留在 Mac：
    Mac: agent_confirm.py --server http://<集群>:8000
    集群: python real/serve_policy.py --model .../Qwen3.5-9B --adapter .../adapters/C60

只在集群上做 forward，不训练、不写数据、不碰真实贴吧。
接口（JSON）：
    GET  /health  -> {"ok": true, "model": ..., "adapter": ...}
    POST /act     -> {"task": str, "history": [{tool_name, arguments, observation}, ...]}
                  <- {"raw": str, "finish_reason": str, "action": {...}, "error": str|null,
                      "prompt_tokens": int, "completion_tokens": int}
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

STATE: dict = {}


def action_payload(action) -> dict:
    """ToolAction(name, arguments) / FinalAction(text) → JSON。"""
    if hasattr(action, "text"):
        return {"kind": "final", "text": action.text}
    return {"kind": "tool", "tool_name": action.name, "arguments": dict(action.arguments or {})}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("", "/health"):
            self._send(200, {"ok": True, "model": STATE["model"], "adapter": STATE["adapter"]})
        else:
            self._send(404, {"ok": False, "error": "unknown path"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/act":
            self._send(404, {"ok": False, "error": "unknown path"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(400, {"ok": False, "error": f"bad json: {exc}"})
            return

        policy = STATE["policy"]
        scenario = SimpleNamespace(task=body["task"])  # 只有 task 进 prompt
        policy.reset()
        try:
            action = policy.act(body.get("history") or [], scenario)
        except Exception as exc:  # noqa: BLE001 - 设备/显存故障要显式回传，不静默
            self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        record = policy.records[-1] if policy.records else {}
        errors = policy.errors[-1] if policy.errors else None
        if isinstance(action, str) and action.startswith("parse_error:"):
            self._send(200, {
                "ok": True, "raw": record.get("raw", ""), "finish_reason": record.get("finish_reason"),
                "action": None, "error": errors, "prompt_tokens": record.get("prompt_tokens"),
                "completion_tokens": record.get("completion_tokens"),
            })
            return
        self._send(200, {
            "ok": True,
            "raw": record.get("raw", ""),
            "finish_reason": record.get("finish_reason"),
            "action": action_payload(action),
            "error": None,
            "prompt_tokens": record.get("prompt_tokens"),
            "completion_tokens": record.get("completion_tokens"),
        })

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志，避免刷屏
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="集群侧模型服务（只做 forward）")
    parser.add_argument("--repo", default=".", help="p2.py 所在目录（集群工程根）")
    parser.add_argument("--model", required=True)
    parser.add_argument("--adapter", default=None)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--context", type=int, default=8192)
    parser.add_argument("--max-new", type=int, default=512)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(args.repo).resolve()))
    from p2 import QwenPolicy  # noqa: E402 - 只在集群上存在

    print(f"[serve] loading {args.model} adapter={args.adapter}", flush=True)
    STATE.update(model=args.model, adapter=args.adapter,
                 policy=QwenPolicy(args.model, args.adapter, args.context, args.max_new))
    print(f"[serve] listening on {args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
