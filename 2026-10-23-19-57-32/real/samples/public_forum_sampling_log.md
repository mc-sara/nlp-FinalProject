# 公共吧真实采样记录表


## Record Table

| # | time | type | variant | status | reason | post_id | bar@t | evidence | counted |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | | | | | | | | | |

## 列名解释

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

## 还需要的数据文件

除本表外，每次操作还需同时留存以下两类数据：

| # | 数据 | 内容 | 存放位置 | 用途 |
| --- | --- | --- | --- | --- |
| ① | 本采样表 | 汇总记录（即上表） | `real/samples/public_forum_sampling_log.md` | 汇总，供 `derive_params.py` 算参数 |
| ② | 原始返回 | 每次操作的完整 JSON 原文 | `real/captures/cap-<run_id>.jsonl`（一次运行一个文件、每次调用一行） | 供 D2 做格式对齐（同构性） |
| ③ | 轨迹文件 | 发帖的完整轨迹 | `runs/real/*.json` | `probe-delay` 自动生成，含 health + 每次工具调用 + verdict |
