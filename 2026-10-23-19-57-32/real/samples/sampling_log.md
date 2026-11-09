# D1 真实采样记录（真实贴吧）

怎么用：跑一次操作，追加一行。推荐用脚本，它会自己写这一行：

```
.venv/bin/python real/samples/d1_post.py --count 9 --duplicate
```

原始返回自动落 `real/captures/`（`cap-<run_id>.jsonl` + `raw/<run_id>/`），表里不再手填文件名。
跑完执行 `python real/samples/derive_params.py` 生成 `artifacts/params_d1.json`。

## 覆盖计划

| 组 | 场景 | 条数 | 回答的问题 |
| --- | --- | --- | --- |
| 只读 | health | 1 | 工具契约：登录态 / 等级 / 元素 / 列表 |
| 只读 | list | 15 | 吧页「马上可见」比例、列表字段结构 |
| 只读 | detail | 2 | 详情页是否比列表更早看到、404 长什么样 |
| 只读 | profile | 2 | 个人主页是否出现该帖（第二证据源） |
| 发帖 | normal | 7 | 第几轮可见（k）、status 分布 |
| 发帖 | dup-risk | 1 | 同标题再发：拒 / 放行 / 观察中 |
| 发帖 | title-edge | 1 | 40 字标题被客户端截到 31 字（不发帖） |
| 发帖 | unknown | 1–3 | 出现即记，不主动构造 |

节奏：间隔 ≥ 120 s，当天 ≤ 10 次；出验证码立刻停当天，`计入统计` 记「否」。

## 记录表1 - agenttest自建吧数据

| # | time | type | variant | status | reason | post_id | bar@t | evidence | counted |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 17:13:25 | health | — | ok | — | — | agenttest@1 | login,level,editor,list | 是 |
| 2 | 17:13:38 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 3 | 17:13:41 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 4 | 17:13:44 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 5 | 17:13:47 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 6 | 17:13:50 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 7 | 17:13:52 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 8 | 17:13:55 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 9 | 17:13:58 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 10 | 17:14:01 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 11 | 17:14:04 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 12 | 17:14:07 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 13 | 17:14:10 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 14 | 17:14:12 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 15 | 17:14:15 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 16 | 17:14:18 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 17 | 17:14:21 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 18 | 17:14:24 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 19 | 17:14:26 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 20 | 17:14:29 | read | list | ok | — | — | agenttest@1 | list@t1 | 是 |
| 21 | 16:52:30 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 22 | 16:53:19 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 23 | 16:54:08 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 24 | 16:54:57 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 25 | 16:55:46 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 26 | 16:56:35 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 27 | 16:57:24 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 28 | 16:58:15 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 29 | 16:59:03 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 30 | 16:59:52 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 31 | 17:00:42 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 32 | 17:01:31 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 33 | 17:02:20 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 34 | 17:03:09 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 35 | 17:03:58 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 36 | 17:04:47 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 37 | 17:05:35 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 38 | 17:06:25 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 39 | 17:07:14 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 40 | 17:08:03 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 41 | 17:08:52 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 42 | 17:09:41 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 43 | 17:10:29 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 44 | 17:11:17 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 45 | 17:12:06 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 46 | 17:12:55 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 47 | 17:13:44 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 48 | 17:14:33 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 49 | 17:15:22 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |
| 50 | 17:16:26 | publish | normal | ok | — | — | agenttest@1 | list@t1 | 是 |

## 记录表2 - 公共吧数据
| # | time | type | variant | status | reason | post_id | bar@t | evidence | counted |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 19:06:52 | health | — | error | health_failed | — | 生活@1 | login,level,editor,list | 否 |
| 2 | 19:07:37 | health | — | ok | — | — | 生活@1 | login,level,editor,list | 是 |
| 3 | 19:10:31 | publish | normal | unknown | poster_msg_lost_after_crash | 11172640997 | 生活@— | 事后list命中:p2 | 是 |
| 4 | 19:29:44 | health | — | ok | — | — | 生活@1 | login,level,editor,list | 是 |
| 5 | 19:29:47 | publish | dup-risk | error | md_missing_front_matter | — | 生活@— | rc=1 未发帖（输出被GBK崩溃吞掉，19:37 用同一条代码路径复现 TiebaError-7） | 否 |
| 6 | 19:36:57 | health | — | ok | — | — | 生活@1 | login,level,editor,list | 是 |
| 7 | 19:37:00 | publish | dup-risk | error | md_missing_front_matter | — | 生活@— | rc=1 TiebaError-7:缺少吧名(kw)，帖子文件未被解析，未发帖 | 否 |
| 8 | 19:47:30 | health | — | ok | — | — | 生活@1 | login,level,editor,list | 是 |
| 9 | 19:47:50 | publish | dup-risk | ok | recheck_confirmed | 11172719804 | 生活@— | poster报[None]未知错误(假失败)；回查吧页 0→1 条同名（19:29/19:36 均无此帖），21.7s 后确已上线 | 是 |
| 10 | 19:56:26 | health | — | ok | — | — | 生活@1 | login,level,editor,list | 是 |
| 11 | 19:57:20 | publish | dup-risk | error | editor_not_opened | — | 生活@— | 点了「发贴」按钮但编辑器20s未出现，提交键没按过，必未发出（回查亦1→1）；疑似发帖入口被静默限流 | 否 |
| 12 | 20:00:51 | health | — | error | health_failed | — | 生活@1 | login,level,editor,list | 否 |
| 13 | 20:03:34 | health | — | ok | — | — | 生活@1 | login,level,editor,list | 是 |
| 14 | 20:08:21 | health | — | error | health_failed | — | 生活@1 | login,level,editor,list | 否 |
| 15 | 20:09:02 | health | — | ok | — | — | 生活@1 | login,level,editor,list | 是 |
| 16 | 20:09:43 | publish | normal | ok | recheck_confirmed | 11172759769 | 生活@1 | list@t1:p1(poster:no_response) | 是 |
| 17 | 20:12:31 | publish | normal | ok | recheck_confirmed | 11172765031 | 生活@1 | list@t1:p1(poster:no_response) | 是 |
| 18 | 20:15:16 | publish | normal | ok | recheck_confirmed | 11172769848 | 生活@1 | list@t1:p1(poster:no_response) | 是 |
| 19 | 20:19:40 | publish | normal | error | editor_not_opened | — | 生活@— | 未回查（status=error） | 否 |
| 20 | 09:36:46 | health | — | ok | — | — | 生活@1 | login,level,editor,list | 是 |
| 21 | 09:37:06 | publish | normal | ok | recheck_confirmed | 11174198052 | 生活@1 | list@t1:p1(poster:no_response) | 是 |
| 22 | 09:39:36 | publish | normal | ok | recheck_confirmed | 11174204908 | 生活@1 | list@t1:p1(poster:no_response) | 是 |
| 23 | 09:42:08 | publish | normal | ok | recheck_confirmed | 11174211968 | 生活@2 | list@t2:p1(poster:no_response) | 是 |
| 24 | 09:44:38 | publish | normal | ok | recheck_confirmed | 11174219028 | 生活@1 | list@t1:p1(poster:no_response) | 是 |
| 25 | 09:50:15 | health | — | ok | — | — | 生活@1 | login,level,editor,list | 是 |
| 26 | 09:52:30 | health | — | ok | — | — | 生活@1 | login,level,editor,list | 是 |
| 27 | 09:52:51 | publish | dup-risk | ok | recheck_confirmed | 11174242623 | 生活@1 | list:1→2条同名；新帖 11174242623 在最上，旧帖 11172719804 仍在 → 服务端接受重复提交（不去重） | 是 |
| 28 | 09:55:23 | publish | normal | ok | recheck_confirmed | 11174249819 | 生活@1 | list@t1:p1(poster:no_response) | 是 |


## 列名解释

- `#`：序号，从 1 递增；与 `runs/real/`、`real/captures/` 里同时间戳的文件对应（轨迹自动按时间命名，精确到秒就能对上）。
- `time`：操作时间，`HH:MM:SS`。
- `type`：`health`（健康检查）/ `read`（只读访问）/ `publish`（真实发帖）。
- `variant`：同一 type 下的变体。read 填 `bar` / `detail` / `profile` / `level`；publish 填 `normal` / `dup` / `long-title` / `unknown`（非人为构造的 unknown 也记这里，成因写 `reason`）。`derive_params.py` 按 `type+variant` 分组，不再靠命令文本猜。
- `status`：`ok` / `unknown` / `rejected` / `notfound` / `error`。
  - `ok` = 工具返回成功；`unknown` = 工具不确定；`rejected` = 工具明确拒绝；`notfound` = 发帖调用成功但三轮吧页都没看到（真值未确认）；`error` = 工具侧不可用（未发出）。
- `reason`：`unknown` 必填成因（`submit_timeout` / `captcha` / `no_response`）；`rejected` 填拒绝原因（`title_too_long` / `duplicate` / `deleted`）；`notfound` 填 `not_visible_after_3_polls`；其余填 `—`。
- `post_id`：帖子 ID；拿不到填 `—`（`unknown` 常见，正好用来算“确认信号缺失率”）。
- `bar@t`：首次在吧页看到该帖的时刻，单位秒（`0` = 发完立刻可见，`2` / `4` = 第 1 / 2 次轮询命中）；三轮都没看到填 `—`。read 行填 `—`。这一列替换原来的 `visible_at` + `elapsed(s)`。
- `evidence`：另两个证据源的结果，紧凑写 `profile:Y/N` + `detail:200/404`，如 `profile:Y,detail:200`；没查填 `—`。这样一行就能看出“吧页看不到、主页看得到”的冲突。
- `counted`：是否计入统计，`yes` / `no`（健康检查失败、工具报错、触发风控填 `no`）。

单元格里的 `|` 写成 `\|`；表里不写 cookie / BDUSS / STOKEN。

## 怎么跑 - 接下来是跑在公共吧环境里

**0. 配置**：`real/config.json`（Mac 路径见 README）。

**1. 一键跑 9 篇 + 1 次重复 + 标题边界**

```
.venv/bin/python real/samples/d1_post.py --count 9 --duplicate --title-edge
```

**2. 只读探针**（不占发帖次数）

```
.venv/bin/python real/real_probe.py list --kw agenttest --limit 20 --raw-out real/captures/raw/manual
.venv/bin/python real/real_probe.py detail --post-id <pid>
.venv/bin/python real/real_probe.py profile --uname <名字> --post-id <pid>
```

**3. 手工补一行**（脚本没覆盖的情况）

```
.venv/bin/python real/samples/record_row.py --type read --variant detail --status ok \
    --bar-at agenttest@1 --evidence "detail@t1:200" --post-id 11139485781
```

**4. 收尾**

```
python real/samples/derive_params.py
```

## 字段约定

- `类型`：`health` / `read` / `publish`。
- `变体`：read 用 `list` / `detail` / `profile`；publish 用 `normal` / `dup-risk` / `title-edge`。
- `status`：`ok` / `unknown` / `rejected` / `notfound` / `error`。
- `吧@t`：`吧名@第几轮看到`（1 起）。k = 轮次 - 1；`—` 表示没看到或不是发帖行。
- `证据`：`list@t2:p3`（第 2 轮列表第 3 位）/ `detail@t1:200` / `profile:yes` / `poster`。
- `计入统计`：健康检查失败、工具报错填「否」；notfound 仍记「是」，只是不进 k。
- 公共吧实验也记这一张表，`吧@t` 写实际吧名。
- 没有 Command / JSON 列：命令在轨迹 `tool_calls`，原始返回在 capture。某次没生成 capture 就在 `证据` 写 `cap:缺失`，不回表加列。

## 数据存储位置

| 数据 | 位置 | 用途 |
| --- | --- | --- |
| 本表 | `real/samples/sampling_log.md` | 汇总，供 `derive_params.py` 算参数 |
| 原始返回 | `real/captures/cap-<run_id>.jsonl`（一次运行一个文件，每次调用一行） | 同构性比对、补漏 |
| 轨迹 | `runs/real/*.json` | few-shot、评测；含 health + 每次调用 + verdict |

## 数据记录结构
```
runs/real/
  20261106-151330-real-<id>.json
real/captures/
  20261106-151330-<id>.jsonl  
  read-01.txt ... read-19.txt 
real/samples/
  sampling_log.md           
  delay_samples.jsonl   
```

