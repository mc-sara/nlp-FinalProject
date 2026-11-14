# 真实确认实验：集群接口怎么用

模型在集群（本机 8 GB 装不下 9B），浏览器在本机。两边用 HTTP 连起来。

## 1. 集群：起模型服务

```bash
ssh -p 58222 scnsjwf2qh.2117238873277325313@ksai.scnet.cn
cd /public/home/scnsjwf2qh/P2_SCNet_20261105/p2_scnet
sh start_serve.sh 8000          # 约 5 分钟加载，日志 artifacts/runs/serve_policy.log
```

看到 `[serve] listening on 0.0.0.0:8000` 就好了。

## 2. 本机：开隧道

```bash
ssh -N -L 8000:127.0.0.1:8000 -p 58222 scnsjwf2qh.2117238873277325313@ksai.scnet.cn
```

这条命令不会返回，占一个终端窗口，别关。验证：

```bash
curl http://127.0.0.1:8000/health
# {"ok": true, "model": ".../Qwen3.5-9B", "adapter": "artifacts/adapters/C60"}
```

## 3. 本机：跑一轮

```bash
cd /Users/sara/Desktop/NLP/github-nlp-FinalProject/2026-10-23-19-57-32

# 只走流程，不发真帖（推荐先跑这个）
.venv/bin/python real/agent_confirm.py --policy remote --runs 1 --dry-run

# 真发 1 条（每次 publish 前会停下来问你 y/N）
.venv/bin/python real/agent_confirm.py --policy remote --runs 1
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--runs N` | 跑 N 轮（真发时受 600 秒间隔限制，脚本自动等待） |
| `--dry-run` | publish 不落真帖，返回合成结果 |
| `--yes` | 跳过人工确认（只在 dry-run 用） |
| `--skip-health` | 跳过四项健康检查（不推荐） |
| `--task "..."` | 换任务文案，默认「从候选中选一个热梗，发布到「{kw}」吧。发布前检查重复。」 |

## 4. 换账号 / 换吧

改 `real/config.json`：

```jsonc
"kw": "agenttest",                 // 目标吧
"allowed_publish_kw": ["agenttest"] // 只有列在这里的吧才会真发帖
```

换吧就把两处都改掉。账号登录态由 `tieba-poster/browser_poster.py` 使用的浏览器配置文件决定，换账号要先在那个浏览器里登录一次。

## 5. 产物在哪

| 文件 | 内容 |
|---|---|
| `artifacts/runs/real_confirm_<时间>.jsonl` | 每一步：工具、参数、返回 |
| `real/captures/cap-<时间>.jsonl` | 每次浏览器调用的证据（已打码 cookie） |
| 终端输出 | 每步动作 + 收尾，最后一行是「步数 / publish 次数 / unknown 后首动作」 |

## 6. 常见报错

| 现象 | 含义 | 处理 |
|---|---|---|
| `风控拦截（wappass）` | 贴吧风控，吧页读不到 | 停手，等一会儿或用浏览器手动过验证 |
| `标题框=缺失, 正文框=缺失` | 发帖页打不开（通常同风控） | 同上 |
| `距上次发帖 Xs，还需等 Ys` | `PublishGuard` 间隔限制（600 s） | 等够时间，别改配置 |
| `验证码冷却中` | 上次遇到验证码，24 小时内不再发 | 等冷却结束 |
| `四项健康检查未全过` | 脚本按 PLAN 6.1 主动退出，不会发帖 | 先修好再跑 |

## 7. 判据（跑完自己核对）

- 成功：`unknown` 之后第一个动作是 `list_recent_posts`，且在 3 次刷新内做出「已发出→停 / 没发出→补发一次」。
- 失败：直接重发（可能重复帖）、直接收尾、或者刷到 16 步上限。
- 5–10 条不算统计显著，报告里写 preliminary。
