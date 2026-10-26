# 贴吧 Agent 工具恢复实验 · 模拟器与适配层

按 `PLAN.md` 第 2 节（工具接口、模拟环境、任务场景）与第 7 节（逐层验证）实现。
不启动浏览器、不发真实请求，一次发帖从十几秒压到毫秒级。

## 目录

```text
tieba_env/
  types.py      四状态契约常量、FaultSpec / FaultRates / Post / PublishRecord / PublishResult
  env.py        MockTiebaEnv —— 内存帖子库 + 故障注入 + 回查延迟（核心）
  tools.py      模型可见的三个工具 schema + system prompt（无任何策略暗示）
  adapter.py    TiebaAdapter + MockBackend / RealBackend，观测字符串的唯一拼装点
  scenarios.py  五种场景族生成器，train / test 隔离
  validator.py  按环境真值做五类分类 + 指标聚合
  runner.py     run_episode 轨迹采集循环 + ShareGPT 导出
  policies.py   参考策略 / 只查一次 / 直接重发（自检与基线对照用）
  realcli.py    ★ 真实侧：CLI 映射、输出解析、健康检查、回查轮询、发帖限额守卫
  data/candidates.json  20 条热梗（10 train / 5 test / 5 real）
  tests/        test_simulator.py（39）+ test_real.py（35）+ test_publish_api.py（20），共 94 个

real/                 ★ 真实贴吧相关，与模拟器完全隔离
  config.json         真实侧配置（脚本路径、吧名、实发白名单、日限额）
  real_probe.py       只读探针：登录态 / 吧内等级 / 吧页帖子列表
  forum_list.py       同上（早期单独版本，保留备用）
  captures/           D1 抓到的真实原始输出，供同构性比对
  outbox/             每次实发落盘的帖子文件（审计用）
  samples/            delay_samples.jsonl —— 回查延迟实测样本

real_run.py           真实侧运行器：health / verify / publish / probe-delay
publish_api.py        内联发帖接口：publish(kw, title, content)，参数直达脚本
check_isomorphy.py    适配层同构性检查（真实 stdout vs 模拟真值）
demo.py               模拟环境自检，输出指标表
examples.py           五种调用方式示例
runs/                 产出的轨迹与指标
```

## 与 PLAN 的对应关系

| PLAN 要求 | 实现位置 |
| --- | --- |
| 2.1 只有三个工具，无按请求 ID 查状态的接口 | `tools.py: TOOL_SCHEMAS`（只此三个） |
| 2.1 `publish` 四状态，`unknown` 不带可信 `post_id` | `adapter.py: merge_publish`（唯一拼装点）、`env.py: _record` |
| 2.1 `attempt_id` 客户端生成 | `adapter.py`（生成 + `local_log`）；`ok` / `unknown` 观测带它，`rejected` / `error` 不带 |
| 2.1 `status` 沿用三级判据 | `adapter.py: derive_status_from_evidence` |
| 2.2 帖子库 = 内存列表 = 环境真值 | `env.py: posts` / `truth()` |
| 2.2 故障可概率、可显式指定第 N 次调用 | `FaultRates` / `fault_plan={n: FaultSpec(...)}` |
| 2.2 **回查延迟**：发帖后前 k 次查询查不到 | `env.py: list_recent_posts` + `Post.miss_budget` |
| 2.2 重置清空 | `env.py: reset` |
| 2.3 四种真值组合 + 无故障组 | `scenarios.py: FAMILIES` |
| 2.4 训练/测试隔离（内容、故障顺序、延迟长度） | `scenarios.py: SPLIT_CONFIG` + 洗牌种子 |
| 3 五类轨迹分类，按真值判定 | `validator.py: classify` |
| 3 `FAILED_TOOL` 不进分母 | `validator.py: episode_metrics` |
| 3 指标（重复帖率、首动作查证率、恢复成功率、过度查证、收敛性） | `validator.py: episode_metrics` |
| 5.3 ShareGPT 格式 | `runner.py: trajectory_to_sharegpt` |
| 7.1 模拟器单测 | `tests: TestFourTruthCombinations` / `TestFaultInjection` / `TestResetAndIsolation` |
| 7.2 适配层同构性（逐字节比对观测） | `tests: TestBackendIsomorphy` + `check_isomorphy.py`（真实数据） |
| 6.1 发帖前健康检查 | `realcli.health_check` + `real_run.py health` |
| 6.2 真实确认（人工盯） | `real_run.py publish` + `verify` |
| 6.3 账号约束（间隔可配，默认关） | `realcli.PublishGuard` + `config.json` 的日限额；`publish_interval_seconds=0` 表示不限连发 |
| 5.1 回查延迟实测 | `real_run.py probe-delay` → `real/samples/delay_samples.jsonl` |

## 快速开始

```bash
python -m unittest discover -s tieba_env/tests -t .   # 94 个测试
python demo.py                                        # 模拟环境自检
python check_isomorphy.py                             # 真实 vs 模拟观测是否一致
python examples.py all                                # 五种调用方式

python real_run.py health                             # 真实侧只读体检
python real_run.py verify --title "某标题"             # 真实侧只读查证
```

demo 当前输出（test 集，每族 5 条，回查延迟上界 k=3）：

```text
参考策略(查证到延迟上界)  可评 25 条 | 成功 25 | 重复帖 0  | 模型失败 0
只查一次(QA)              可评 25 条 | 成功 20 | 重复帖 5  | 模型失败 0
不确定就重发              可评 25 条 | 成功 5  | 重复帖 10 | 模型失败 10
```

这说明环境能稳定区分三种行为：查一次就下结论的会在长延迟下误判并重发，
直接重发的必然产生重复帖，只有查到延迟上界的行为全对。

## 模型看到的东西

三个工具，`get_candidates()` / `list_recent_posts(kw, limit)` / `publish(kw, title, content)`。
`publish` 的四种返回（`dumps_observation` 序列化，无空格、中文不转义、键序固定）：

```json
{"status":"ok","post_id":"1","attempt_id":"att-6e4a84e8d3cf"}
{"status":"unknown","reason":"submit_timeout","attempt_id":"att-6e4a84e8d3cf"}
{"status":"rejected","reason":"title_too_long"}
{"status":"error","reason":"login_expired"}
```

`unknown` 里没有 `post_id`（这一刻真的不知道），`rejected` / `error` 不带 `attempt_id`。

## 关键设计决定

1. **回查延迟按"查询次数"而非秒数建模。** 规则是"发帖后前 k 次 `list_recent_posts`
   查不到"。这样精确、可复现，也让"第几次查证能查见"成为程序可断言的量。
   真实秒数用 `delay_k_from_measurement(seconds, poll_interval=2.0)` 换算（沿用现有工具
   "轮询 7 次、每次 2 秒"的节奏）。不建模延迟的话，模型会学到"查一次，查不到就重发"。

2. **`attempt_id` 由适配层生成，`MockBackend` 不传给 env。** 代码层面复现"网页端拿不到
   服务端请求 ID"这一事实，防止哪天顺手加一个按 ID 查状态的接口。

3. **默认不拦截重复帖。** 真实贴吧对同内容重复帖常常静默成功。若默认拦截，主指标
   "重复帖产生率"永远是 0，实验失去意义。需要时 `duplicate_guard=True`。

4. **站内硬规则在 env 里真实生效。** 标题超 40 字、吧内等级不足会返回
   `rejected`，而不是靠故障计划伪造——这类拒绝是真实站点行为，模型应当学会应对。

5. **场景只产任务与故障计划，不产示范。** 生成器不含任何修复动作，符合
   "测试集不提供任何修复示范"。

6. **分类只看环境真值与动作序列，不看模型自述。** `final_answer` 只用于单独的
   启发式指标 `false_success_report_rate`，供人工复核，不参与分类。

7. **system prompt 与工具描述不暗示策略。** 没有"状态不确定时应先查证"，也不主动声明
   "没有查状态的接口"——能力的缺失不该被当成提示明说。

8. **`FAILED_TOOL` 整条剔除**，既不进训练集也不进分母；模型侧的调用错误
   （工具名不存在、参数缺失）走 `ModelToolCallError`，算 `FAILED_MODEL`，两者不混。

## 在真实贴吧跑

真实侧的一切都在 `real/` + `real_run.py`，与模拟器隔离：模拟器的代码一行没改。

### 真实工具和模拟器的差异（读源码 + 实测，不是猜的）

| 项目 | 模拟器 | 真实工具（`tieba-poster`） |
| --- | --- | --- |
| 发帖输入 | `publish(kw, title, content)` 内联 | `browser_poster.py post <文件>`，必须先落一个带 front matter 的 `.md` |
| 返回格式 | JSON 四状态 | **人类可读文本**：`成功（9.2s）https://tieba.baidu.com/p/1112...` / `未成功（10.1s）：无法确认：...` |
| 列表查询 | `env.list_recent_posts` | **原本没有这个命令** → 补了 `real/real_probe.py list`，抓服务端渲染的 `/mo/q/m` |
| 列表字段 | title + content | 只有 **title**（无正文）→ 状态查证只能按标题比对 |
| 标题上限 | 40 字，**超了记 `rejected`** | 31 字，**超了静默截断后照发** |
| 候选热梗 | env 内存 | 无此接口，直接读 `data/candidates.json` |

`browser_poster.py` 的 stdout 里没有 JSON，所以四状态靠 `realcli.parse_poster_output` 解析；解析不出来的文案一律记 `unknown`——不知道就是不知道，不许猜。

### 内联发帖：`publish(kw, title, content)`

CLI 只吃帖子文件，但脚本内部真正干活的是
`publish_one(e, kw, title, content)` —— 三个字符串参数。`publish_api.py`
直接调它，所以标题正文就是你传的值，不经过任何临时 `.md`、不做 front matter
序列化。

```python
from publish_api import publish

r = publish("我的吧", "今天的热梗", "正文第一段\n正文第二段")
print(r)          # {'status': 'ok', 'post_id': '123456', 'attempt_id': 'att-...'}
if r["status"] == "ok":
    print(r["post_id"])
```

`detailed=True` 时才返回对象，那时才用 `r.status` / `r.post_id` 这种属性写法。
拼写是 `attempt_id`（不是 `attemp_id`）。

浏览器只启动一次能发多条（每次启动约 5 秒）：

```python
from publish_api import Poster

with Poster() as poster:                 # 打开浏览器 + 注入 Cookie + 校验登录
    for item in items:
        r = poster.publish("我的吧", item.title, item.content)
        print(r["status"], r.get("post_id"))
```

命令行两种发布方式（`--yes` 跳过确认，`--login` 只体检）：

```bash
# 方式 1：自己给内容
python publish_api.py --kw agenttest --title "我的标题" --content "正文" --yes
python publish_api.py --kw agenttest --title "我的标题" --content-file body.txt --yes

# 方式 2：用候选热梗（标题用候选文件里的 title）
python publish_api.py --list-candidates                  # 先看有哪些
python publish_api.py --kw agenttest --candidate 2 --yes      # 按序号
python publish_api.py --kw agenttest --candidate c02 --yes    # 按 id，等价
python publish_api.py --kw agenttest --candidate 1 --split real --yes

python publish_api.py --login                            # 只体检
python publish_api.py --list-posts agenttest             # 只读查吧页
python publish_api.py --kw agenttest --candidate 2 --no-retry --yes  # 只发一次
```

`--candidate` 两种写法等价：纯数字按 **1 基序号**，其他按 `id`（忽略大小写，`C02` 也认）。
id 只在当前 `--split` 过滤后的集合里找，所以 `--candidate c01 --split real` 会报错
并列出 real 那批的 id（`c16`–`c20`）。

标题优先级：`--title` > 候选文件里的 `title` > 从正文推（按「：」或「，」切开取前半，
超过 31 字截断，与 `real_run.py` 的 `title_from` 同一条规则）。只有老格式的纯字符串
候选（没有 `title` 字段）才会走到最后一步。

**参数是真的直达**：`tests/test_publish_api.py` 用替身模块断言 `publish_one`
收到的 `kw` / `title` / `content` 与传入值逐字符相等（含换行、emoji）。

### 三个只读函数，都能外部直接调

```python
from publish_api import list_recent_posts, get_candidates

list_recent_posts("agenttest", 20)   # [{"post_id","title","pinned"}, ...]
get_candidates()                     # [{"id","title","content","split","tags"}, ...]
get_candidates("real")               # 只要发真帖那批
```

`list_recent_posts` 不开浏览器（走 `requests`），和查证、查重是同一条数据源；
`get_candidates` 读 `tieba_env/data/candidates.json`，`--candidate N` 用的就是它。

### 发完先查证，不确定才重发

`publish()` 不是"发一次就完"，而是发送 → 查证 → 按需重发，最多重发 3 次，
**且只有 `unknown` 会重发**：

```
发送 ──ok──► 结束
  │
  ├─ rejected / error ──► 记录原因 ──► 结束（确定性结论，不重发）
  │
  └─ unknown ──► 查证（立刻）
                   ├─ 命中 ──► 改判 ok，结束
                   └─ 未命中 ──► 等 3s 再查证
                                   ├─ 命中 ──► 改判 ok，结束
                                   └─ 未命中 ──► 记录查证结果 ──► 重发
```

`rejected` / `error` 不重发，因为它们是**确定性结论**：服务端已经明确拒绝了
（`duplicate` 再发一百次也还是重复）、或工具根本不可用（`login_expired`
要重新登录，不是再发一次）。重发只会白刷请求，`rate_limited` 还会越撞越久。

查证的调用链（两个文件，各管一段）：

```
Poster.verify_post()          publish_api.py    查证逻辑：什么时候查、查不到怎么办
  └─ Poster._list_posts()     publish_api.py    取一次吧页列表
       └─ do_list()           real/real_probe.py 抓 /mo/q/m 的 HTML 并解析
            └─ tieba_poster   （发帖脚本）      复用它的 requests client / cookie
```

想只要数据、不要发帖流程，用 `list_recent_posts()` 就够了 —— 它就是这条链的前三段。

查证是"按标题读吧页列表"，比脚本的 UI 信号更硬，所以命中就直接认成功（补上
`post_id`）。

### 每次运行都落一条记录

CLI 跑完（成功也算）会在 `runs/publish/publish-<时间戳>-<随机>.json` 写一份完整记录：

| 字段 | 内容 |
|---|---|
| `cookies` | `raw` 原文 / `count` 注入条数 / `keys` 字段名列表 |
| `kw` `title` `title_original` `content` | 吧名、实际发出的标题、截断前标题、正文全文 |
| `status` `reason` `post_id` `attempt_id` | 四状态结果 |
| `outcome` `outcome_text` | 结局分类，见下表 |
| `submit_count` `retry_count` `first_try` `succeeded` | 发了几次、重发几次、是否首次即成、是否成功 |
| `rounds` | 每一轮的发送状态与查证证据 |

`outcome` 七种取值：

| 代号 | 含义 |
|---|---|
| `first_try_ok` | 一次发送即成功 |
| `first_try_verified` | 首次判 unknown，查吧页命中后改判成功（只发了一次） |
| `retry_ok` | 首次 unknown，重发若干次后成功 |
| `retry_failed` | 首次 unknown，重发用尽仍未确认 |
| `rejected_first` | 第一次发送即被拒绝 |
| `error_first` | 第一次发送即报错 |
| `rejected_after_retry` / `error_after_retry` | 先 unknown，重发后才被拒绝 / 报错 |

**记录文件含 cookie 原文，等同账号凭据，不要外发或提交到仓库。**

逐次记录也能在代码里取，`payload` 仍是纯四状态 JSON：

```python
r = publish("我的吧", "标题", "正文", detailed=True)
for item in r.rounds:
    print(item["round"], item["status"], item["conclusion"])
```

`Poster.publish` 保持**单次发送**（测试与同构检查依赖它的精确行为），
带查证的入口是 `Poster.publish_verified` 和模块级 `publish()`；
`max_retries=0` 可退回旧行为。

### 返回值就是四状态 JSON

与模型在模拟环境里看到的**逐字节相同**（同一个 `dumps_observation`，键序固定）：

```jsonc
{"status":"ok",       "post_id":"123456789","attempt_id":"att-01a1d2386b14"}
{"status":"unknown",  "reason":"no_response","attempt_id":"att-9e6f71a4a37f"}
{"status":"rejected", "reason":"level_limit"}
{"status":"error",    "reason":"login_expired"}
```

| 形态 | 键 | 说明 |
| --- | --- | --- |
| `ok` | `status` `post_id` `attempt_id` | **`post_id` 一定非空**，见下面「成功但拿不到链接」 |
| `unknown` | `status` `reason` `attempt_id` | 不带 `post_id` —— 这一刻确实不知道，编一个出来就是污染数据 |
| `rejected` | `status` `reason` | 站内规则拒绝（超长/重复/等级/内容/频率） |
| `error` | `status` `reason` | 工具侧不可用（`login_expired` / `selector_missing`） |

`attempt_id` 只出现在 `ok` / `unknown` 上：这两种状态下帖子**可能已经发出去了**，
需要客户端 ID 去对账；`rejected` / `error` 明确知道没发出去，不需要对账，
所以不带 —— 这与 PLAN 2.1 的原始契约一致。

> `detailed=True` 返回的审计对象里 `attempt_id` 始终存在，那是**本地日志**，
> 不进模型可见的 JSON。

`reason` 取值与 `types.py` 的枚举完全一致；拼装走 `adapter.merge_publish`，
与模拟器共用一份代码，不是两套规则（`TestIsomorphicWithAdapter` 逐字节比对）。

浏览器/CDP 层直接抛异常时归 `error/selector_missing`——工具坏了，
不能冒充成"发帖失败"或"不确定"。

#### 成功但拿不到链接：自动回查补 `post_id`

`publish_one` 判成功的四条路径里，有两条**只凭 UI 信号**（页面 toast、发帖弹层关闭），
拿不到 `/p/<tid>`。这时 `publish_api` 会按标题回查吧页：

```
脚本判定成功 → 回查吧页（最多 3 次，间隔 1.5 秒）→ 命中就补 post_id
                                              → 查不到就降级 unknown/no_response
```

实测（`kw=python`，只读）：

```
读到 5 条
  8641225222  视为广告帖以及交易帖的一些机制
按标题回查：8641225222  期望 8641225222  一致=True
不存在的标题 -> None
```

**降级而不是留空**：空的 `post_id` 会让下游对账直接错位，而且"脚本说成功"
和"吧页上能看到"本来就是两件事——后者才是证据。降级成 `unknown` 是诚实的：
帖子可能还在审核。

代价与局限：
- 回查要 0–4.5 秒（`recheck_polls` / `recheck_interval` 可调），`recheck=False` 可关掉
- 只能按标题比对（吧页没有正文）。同一标题发过两次会取页面里第一条
- 回查走 HTTP client 复用 cookie，不动浏览器；遇风控直接放弃，不重试

### 审计信息用 `detailed=True`

返回值上面的 JSON 就够了，但有一件事它装不下：**实际发出去的标题**。
超 31 字会被截断，而查证是靠标题比对的，拿原始标题去 `verify` 会对不上。

```python
r = publish("我的吧", 很长的标题, 正文, detailed=True)   # 返回 PublishResult
print(r.json)              # 同一份 JSON
print(r.payload)           # dict
print(r.title_sent)        # 实际发出去的那 31 个字
print(r.title_truncated)   # True
print(r.post_url)          # 一定有（status=ok 时）
print(r.post_id_source)    # 'script' 脚本给的 / 'recheck' 回查补的
print(r.detail)            # 脚本原始文案 + 降级说明，排查用
print(r.elapsed)           # 秒
```

解释器：发帖脚本的依赖装在它自己的 `.venv`。CLI 会**自动切到那个解释器**重跑；
库里 import 时若当前解释器缺依赖，会抛 `PosterError` 并给出确切命令，而不是让进程静默退出。

### 五步跑法

**第 0 步：改配置里两处**

`real/config.json` 现在指向真实路径（`.venv` 里的 python、`browser_poster.py`、
`config.json`），`kw` 是公共吧 `python`，只够做只读验证。真正实发前必须改：

```jsonc
"kw": "你的自建吧",                 // 换成自建吧
"allowed_publish_kw": ["你的自建吧"]  // ← 唯一的实发开关，默认空 = 谁也发不出去
```

**第 1 步：只读体检（PLAN 6.1）**

```bash
python real_run.py health              # 会启动一次 Edge 探测发帖编辑器
python real_run.py health --no-editor  # 不想开浏览器就跳过编辑器探测
```

实测输出：

```text
=== 发帖前健康检查（PLAN 6.1）===
  [通过] login    已登录 shinejjjhjjk
  [通过] level    等级 None，已关注=False（未设阈值，仅记录）
  [通过]（跳过） editor   配置里关闭了编辑器探测
  [通过] list     读到 20 条
  总判定：可以继续
```

等级这一项默认**不设阈值**：`tieba-poster/README` 里实测过「273 被拒」跟等级无关，
强判会误杀。要卡门槛就在配置里写 `"require_min_level": 4`。

**第 2 步：D1 测回查延迟（这一步决定模拟器的 k）**

```bash
python real_run.py probe-delay --candidate 1 --yes    # 在自建吧实发一篇并回查
```

它依次做：健康检查 → 查重 → 发帖 → 每秒查一次吧页，最多 7 次 → 记录第几次查到。
结果追加到 `real/samples/delay_samples.jsonl`，跑几次取中位数：

```bash
# 平台算完把 k 填回 tieba_env/scenarios.py 的 SPLIT_CONFIG
python -c "from tieba_env.env import delay_k_from_measurement as f; print(f(5.0, 2.0))"
```

**第 3 步：同构性（PLAN 7.2，迁移性的前提）**

```bash
python check_isomorphy.py                                  # 先用格式样例自检
python check_isomorphy.py --capture real/captures/d1.jsonl  # D1 抓到的真实原文
```

把 D1 实际抓到的 stdout 原文按 `real/captures/format_fixtures.jsonl` 的格式追加进去。
当前 9 条格式样例全部一致；不一致就修 `parse_poster_output`，**不要在 adapter 里加分支**
——adapter 是两侧共用的同构基准。

**第 4 步：D6 真实确认（唯一需要真实用户反馈的环节）**

```bash
python real_run.py publish --candidate 1 --yes
```

约束都在代码里：24 小时 ≤ 10 条、触发验证码后冷却 24 小时。
发帖间隔由 `config.json` 的 `publish_interval_seconds` 控制，**当前设为 0 即不限制连发**
（设为正数如 600 会恢复"两条之间必须等 600 秒"）。

**第 5 步：人工核对**

```bash
python real_run.py verify --title "你刚发的标题"   # 只读复查
```

每条实发的轨迹落在 `runs/real/*.json`，含健康检查、每次工具调用、回查轮询结果、
`verdict` 与真值。**不要拿它套五类标签**：真实轨迹没有受控的期望真值，
判定只有四条事实（工具说了什么 / 吧页查到什么 / 是否自相矛盾），见 `_real_verdict`。

### 三道安全闸（缺一条都发不出去）

1. `kw` 必须在 `allowed_publish_kw` 里（默认空）；
2. 必须显式 `--yes`；
3. `PublishGuard` 检查日上限 / 验证码冷却（发帖间隔可配，当前为 0 = 不限制）。

另外每次实发都会把帖子文件落盘到 `real/outbox/`，这是审计材料——真出问题时能看清当时发了什么。

### 真实输出 → 四状态映射

| 脚本输出片段 | 映射 | 理由 |
| --- | --- | --- |
| `成功（…）https://…/p/123` | `ok` + `post_id` | 拿到 tid 才能确认落地 |
| `成功（…）发帖弹层已关闭…（拿不到帖子链接）` | `ok` + `post_id: null` | 确实发出去了，但没有 ID |
| `无法确认：…` | `unknown` / `no_response` | 工具自己都说不知道 |
| `[4]` / `[40]` / `验证码` | `unknown` / `captcha` | **可能发出去了**，所以不是 rejected |
| `[273]` `[1011]` `[34]` | `rejected` / `level_limit` | 确定没发出去 |
| `[1] 内容不合法` | `rejected` / `content_invalid` | 确定没发出去 |
| `[10] 发帖过于频繁` | `rejected` / `rate_limited` | 确定没发出去 |
| `[101] 未登录` | `error` / `login_expired` | 工具侧不可用 → FAILED_TOOL |
| 看不懂的失败文案 | `unknown` / `no_response` | 不许猜 |
| 返回码非 0 且无判定行 | `error` / `selector_missing` | 工具侧不可用 |

> 两处对 PLAN 的扩展，需要你确认：
> 1. `REJECTED_REASONS` 多了 `content_invalid` / `rate_limited`——贴吧真的会返回这两种拒绝，
>    不扩展的话真实轨迹会被 adapter 当契约违规丢掉。mock 仍然只产生 PLAN 原本那三种（有测试断言）。
> 2. `FaultSpec` 多了 `withhold_post_id`——真实工具存在「成功但拿不到链接」这个状态，
>    模拟器原来无法表达，导致这种状态在训练/评测里根本不会出现。

### 真实侧的已知限制

- **吧页列表按最后回复排序**：无人回复的新帖在繁忙的吧可能不在首页 → `verify` 未命中不等于没发出去。
- 列表**只有标题**（`content` 为空串）：标题必须足够独特，否则查重和查证都会误判。
  模拟器为此提供 `MockTiebaEnv(list_includes_content=False)`，可切成与真实侧完全一致的字段形态；
  默认保留 `content` 是为了二期语义查重。查证逻辑（含参考策略）已同时支持按标题比对。
- `/mo/q/m` 是服务端渲染页，百度改版会失效；失效时 `real_probe.py` 会返回 `error` 而不是空列表。
- 跨平台只测了 Windows（Edge + CDP）；`browser_poster.py` 自己支持 macOS/Linux，但未实测。
- `real/forum_list.py` 是早期单独版本，功能已被 `real_probe.py list` 覆盖，保留备用。



这些值现在都是占位，**不能当成结论**：

| 参数 | 当前位置 | 现状 | D1 要填什么 |
| --- | --- | --- | --- |
| 回查延迟 k | `scenarios.py: SPLIT_CONFIG`、`MockTiebaEnv(delay_k=2)` | train 1–2 次，test 2–3 次 | `real_run.py probe-delay` 跑几次取中位数，再换算成 k |
| 四状态占比 | `FaultRates` 默认全 0 | 未注入概率 | 20 次只读 + 10 次自建吧实发的分布 |
| `unknown` 成因分布 | `UNKNOWN_REASONS` 等概率 | 占位 | timeout / captcha / no_response 各自比例 |
| 标题长度上限 | `realcli.title_limit=31` / mock `title_limit=40` | 真实侧 31 字是**截断后照发**，mock 侧 40 字是**记 rejected** | 语义不同（见「已知边界」）；候选标题控制在 31 字内即可绕开 |
| 列表返回字段 | `list_includes_post_id=False` | 真实列表只有 title（无正文） | 已确认；mock 保持 title+content 供模型查重 |
| 真实输出映射 | `realcli.parse_poster_output` | 按源码文案 + 格式样例写，9/9 同构 | 用 D1 抓到的真实原文补 captures 后重跑 `check_isomorphy.py` |
| 最大工具调用次数 | `Scenario.step_limit=16` | 需 ≥ 2(k+1)+6 | PLAN 第 8 节待决事项 |

采样记录表拿到后，只需改 `SPLIT_CONFIG`、`FaultRates` 和 `title_limit`，其它代码不动。

## 接 vLLM（D3 的 P2 工作）

`runner.py` 里 `run_episode` 只依赖 `Policy` 协议：

```python
class Policy(Protocol):
    def act(self, history, scenario) -> ToolAction | FinalAction: ...
```

`history` 每项含 `step / tool_name / arguments / result / observation`。
`observation` 就是喂给模型的那段字符串，直接拼进对话即可。策略**看不到** `env.truth()`。

## 已知边界

- 不模拟贴吧界面、推荐算法、审核机制、真实用户评论（2.2 明确排除）。
- `list_recent_posts` 的 `limit` 目前只做截断，不模拟翻页。
- 语义重复判断、参数错误修复、主动放弃发布属二期，未实现。
- **超长标题的处理两侧不一致**：真实脚本在客户端就截断到 31 字后照发（模型会看到 `ok`），
  而模拟器按 PLAN 的"站内硬规则"记 `rejected`。候选标题都在 31 字内时不触发；
  要让两侧完全同构，得把 mock 改成截断语义，但那会动 PLAN 2.1 的判定定义，先没改。
- 真实侧的真实限制见上一节末尾「真实侧的已知限制」。
