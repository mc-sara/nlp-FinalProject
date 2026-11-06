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

## 记录表

| # | 时间 | 类型 | 变体 | status | reason | post_id | 吧@t | 证据 | 计入统计 |
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

## 怎么跑

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

## 历史说明

- 前 50 行是 2026-11-04 的快速采集：20 条只读 + 30 篇发帖，全部「第 1 轮可见」（k=0）。
- 2026-11-04 的 30 篇连发验证了「账号能发、k=0 是常态」，也验证了必须限速：那次之后按 120 s 间隔重跑。
