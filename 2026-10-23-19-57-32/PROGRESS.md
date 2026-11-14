# 进度备份（2026-11-10 13:55）

本文件记录**当前真实状态**，包括已作废的实验，避免后面误当结论用。

## 一句话状态

口径已冻结（标题 31 字截断 / 吧名 agenttest / `origin` 标注）；候选池扩到 100 条（train 60 / test 25 / real 15）；
**ABC 实验按「方案 1」执行：每组 300 条场景、100 步**。C 组旧 adapter（100 条数据、8 epoch）作废，只当管线验证。
三组跑完后再做一次真实环境确认（D6），本阶段实验即结束。

## 1. 口径冻结（v1，已提交）

| 项 | 旧 | 新 | 依据 |
|---|---|---|---|
| 超长标题 | mock 判 `rejected(title_too_long)`（40 字） | 客户端 31 字截断后照发，服务端拿到 31 字 | 真实侧实测 `title_limit=31`（`real/samples/sampling_log_2.md`、`table2_public.csv`） |
| 模拟器吧名 | train `测试` / test `实验二` | 两侧都是 `agenttest` | 与真实采样吧一致；隔离靠 `candidates.json` 的 split 候选池 + `scenario_id`，不靠吧名 |
| 轨迹来源 | 无标注 | `origin="reference_policy"` | 这些是脚本专家（`ReferencePolicy`）的 rollout，不是模型自己跑的 |

代码改动：`tieba_env/env.py`（`title_limit=31` + `publish()` 截断、删 `title_too_long` 分支）、
`tieba_env/types.py`（`REJECTED_REASONS_MOCK_ONLY = ("duplicate", "level_limit")`）、
`tieba_env/scenarios.py`（`SPLIT_CONFIG` 两侧 kw `agenttest`）、`demo.py`（build 写 `origin`）、
`README.md`（四处同步）。测试 104 个通过，唯一失败是既有的 Windows 路径用例
`tests/test_real.py:127 test_resolve_relative_to_poster_dir`（POSIX 上必然失败，与本改动无关）。

## 2. 候选池（已扩到 100 条）

`tieba_env/data/candidates.json`：**100 条**，`train 60 / test 25 / real 15`，id、标题、正文三元组唯一，
标题 ≤14 字、正文 45–71 字。原 20 条一字未动，新增 80 条（`c21–c70` train、`c71–c90` test、`c91–c100` real）。
**这 80 条是我按贴吧生活语感写的，不是网络采集的真实帖**，报告里必须写明，不能当真实采样证据。

`tests/test_simulator.py` 的 `test_candidates_file_has_20_entries` 已换成
`test_candidates_pool_is_isolated_by_split`（断言 100 条、id/标题唯一、3 个 split 各 ≥15 条）；
`tieba_env/scenarios.py` 的 `load_candidates` 补上 `title` 字段（原来归一化会丢掉）。

## 3. 数据（方案 1：每组 300 条）

- `runs/sim/train.jsonl`：**300 条**（5 族 × 60；正常 60 / 恢复 240），`runs/sim/test.jsonl`：**93 条**
  （每族 20 → 去掉 7 个被用作人工修正素材的退休场景，族分布 20/18/18/18/19）。
- 生成命令（2026-11-13 重生成，带候选池随机排列与措辞池）：
  `python demo.py --build --per-family 60 --splits train --shuffle-pool`、
  `--per-family 20 --splits test --shuffle-pool`、B 组 `--families normal --per-family 300 --splits train --shuffle-pool`。
- 多样性（2026-11-13 实测）：C-train 观测 **300** 种 / 动作序列 5 种 / 收尾句 **15** 种；
  B 观测 **300** 种 / 动作序列 1 种（固定 3 步）/ 收尾句 **6** 种；test 观测 93 种 / 收尾句 15 种。
  850 次 publish 全部满足 `title == _make_title(content)` 且 `content ∈ get_candidates` 返回池。
- 训练输入：C 用 `runs/sim/train_all.jsonl`（300 + `human_corrected.jsonl` 7 = **307** 行，id 唯一、
  `split` 全 train、`origins={reference_policy, human_corrected}`）；B 用 `runs/sim_b/train.jsonl`（300 行纯 normal）。
- 集群数据集（2026-11-13 重建）：`artifacts/data_v2/tieba_B.json` 300 / `tieba_C.json` 307，
  token 审计 B max 3173 / C max 3708（cutoff 8192），监督 token B 54,515 / C 99,468。
  `prepare` 新增 `--only {B,C,both}`（只出 B 时不再强制要求 recovery 样本）。

## 4. ABC 实验设计（方案 1，2026-11-10 定稿）

| 组 | 训练数据 | 训练 | 目的 |
|---|---|---|---|
| A | 无 | 不训练（原始 Qwen3.5-9B） | 下限基线 |
| B | `tieba_B`：300 条纯 normal 轨迹 | LoRA 100 步 | 控制「只学格式/工具调用」这一项 |
| C | `tieba_C`：300 条（normal 60 + 故障恢复 240） | LoRA 100 步 | 主组：看故障恢复策略是否被学到 |

- 三组**完全一致**：Agent Runner、工具接口、系统提示、解码（greedy、`max_new=512`、`context=8192`、
  `qwen3_5_nothink`）、最大工具调用 16 步、测试任务集。
- LoRA 超参（B/C 相同）：`r=8, alpha=16, lora_target=all`、lr `5e-5`、cosine + warmup 0.1、bs 1 × ga 8、
  100 步 = 800 样本次 ÷ 300 条 = **2.7 epoch**、bf16、梯度检查点、`flash_attn=disabled`。
- 预计单组 ≈73 分钟（100 步 × 41 s + 约 5 分钟加载），B、C 串行 ≈2.5 小时。
- **评测**：test split 每组 100 条（每族 20，`--seed 1042`），A/B/C 各跑一遍；
  指标 = 重复帖产生率、不确定后首动作查证率、恢复成功率、无故障成功率（能力保持）、
  过度查证（查证次数 > 3）、收敛性（平均工具调用数 / 触顶比例）、error 比例、FAILED_TOOL 单列（不进分母）。
- **真实确认（D6）**：三组评测后，用公共吧「生活」发 5–10 次，人工盯；
  只验证「策略在真实条件下没崩」，不做统计显著性。这是本阶段的最后一步。

## 5. 训练历史：C 组旧 adapter 作废

- 2026-11-09 20:12:05 → 21:24 跑完（100 步，`train_runtime` 1:08:15 ≈ 41 s/step，
  `train_loss` 0.04848，`epoch` 7.72）。产物集群 `artifacts/adapters/C/`，`adapter_model.safetensors` 86 MB，
  sha256 前 24 位 `0fbe1a61438a84d2f04ffd69`。
- **作废理由**：数据是旧口径（超长标题被 mock 判 `rejected`、吧名 `测试`），且 100 步对 100 条样本是
  **8 epoch**，样本本身又全是 `ReferencePolicy` 首跑正确的蒸馏轨迹。该 adapter 只说明「LoRA 管线能跑通」。
- 未训练 B 组、未跑 A/B/C 评测；`chain_after_C.sh` 已于 2026-11-09 20:49 kill（`artifacts/runs/chain.log`）。

### 5.1 基线探针：旧 C adapter 是不是“复读机”（2026-11-13）

用 `probe.py` 对 `artifacts/adapters/C`（上面那个作废的旧 adapter）跑两个探针，
目的是区分“模型读观察再决定”与“把节拍背下来”。脚本自身用两个已知答案的对照组校准过：
`--policy reference`（脚本专家）判“读观察”，`--policy replay`（固定节拍）判“复读”，两者结论相反。

**openloop**（visible 5 条 + delayed 5 条，同一场景跑两遍：真实列表 / 恒定空列表）：

| 族 | 真实列表 | 恒空列表 |
|---|---|---|
| visible（4 条） | 4 步：`get → list → publish → list` 即收尾 | 7 步：再查 2 次 + 补发一次 |
| delayed（5 条） | 5 步：查 2 次命中 | 7 步：同上 |

9 个场景**全部**动作随观察变化（0 个不变）→ 判“读观察”。

**kscan**（delayed 族 × k=0/1/2/3，每档 3 条）：

| k | list 次数 | 总步数 | post_count |
|---|---|---|---|
| 0 | 2 | 4 | 1 |
| 1 | 3 | 5 | 1 |
| 2 | 4 | 6 | 1 |
| 3 | 4（预算 3 轮封顶） | 7 | **2** |

结论：旧 C **不是复读机**——动作随观察变化，查证次数严格随 k 增加（k=3 撞预算后按规则补发一次）。
之前“C 只是把训练里的 4/5 步节拍背下来”的判断**不成立**；仍然成立的只有“测试与训练同分布、80/80 高估泛化”。

附带发现：**k=3 时补发会让 `post_count=2`**（3 次查不到就看不出延迟，按“补发一次”规则动手），
这种场景判分会计重复帖；测试集里没有 k=3，此前未暴露。

### 5.2 C38 训练（38 步，2026-11-13）

配置 `artifacts/configs/train_C38.yaml`：`dataset: tieba_C`（`artifacts/data_v2`，307 行）、`max_steps: 38`、
lr 5e-5、cosine+warmup 0.1、LoRA r=8/α=16/target all、bs1×ga8、cutoff 8192、`save_steps: 20`。

- 2026-11-13 21:31 启动，权重加载 2:27，21:35:24 开始计步；
  `train_runtime` **1:22:44（4965 s，130.6 s/step）**，`train_loss` **0.1775**，`epoch` **0.9902**。
- loss 轨迹：前 10 步均值 0.3135 → 后 10 步均值 0.1004；最低 0.0801（step 32），最高 0.4311（step 2）；
  末端 8 步在 0.08–0.12 之间锯齿，未平滑贴底（原始曲线见 `artifacts/adapters/C38/trainer_log.jsonl`）。
- 产物 `artifacts/adapters/C38/`（637 MB）：顶层 `adapter_model.safetensors`、`checkpoint-20`、`checkpoint-38`。
- 判读：均值 0.1775 高于预设的 0.15 阈值，属**欠拟合一侧**；但与旧 C（100 步、8 epoch、0.0485）不可直接比。
  均值偏高主要来自前 8 步（占总 loss 三分之一），尾部已在 0.09–0.12。是否“学会”以行为探针为准（见 §5.3）。
- 实测比预估慢：预估 90 s/step，实际 130.6 s/step——`data_v2` 样本更长（最长 3711 tokens，旧口径 1699）。

### 5.3 C38 行为探针 test20（2026-11-13 23:32）

命令：`.venv/bin/python probe.py --mode test20 --adapter artifacts/adapters/C38 --per-family 4`
→ `artifacts/runs/probe_C38_test20.jsonl`（18 条：test 集每族 4 条，退役 7 条后共 18）。

| 族 | n | 步数 | list | publish | post_count | 终态 |
|---|---|---|---|---|---|---|
| normal | 4 | 3 | 1 | 1 | 1 | ✅ |
| unknown_visible | 4 | 4 | 2 | 1 | 1 | ✅ 查到即停 |
| unknown_delayed | 4 | 5 | 3 | 1 | 1 | ✅ 查到即停 |
| unknown_not_published | 3 | **16（撞上限）** | **14** | 1 | 0 | ❌ 只查不发 |
| unknown_resubmit_unknown | 4 | **16（撞上限）** | **14** | 1 | 0 | ❌ 只查不发 |

汇总：`after_unknown_list` **14/14**、`parse_errors 0`、`step_limit_hits 7`、`mean_steps 8.61`。

判读：

- **学到**：①拿不到明确结果先查吧页（14 个故障场景首动作全是 `list_recent_posts`）；②查到就停、不重复发。
- **没学到**：“连查 3 次没有 → 补发一次”。训练示范里这一步是 8 步（`publish → 3×list → publish → list`），
  模型在这 7 条里一路 list 到 16 步上限、一次都没补发，判 `FAILED_MODEL`。
- 指向欠拟合：1 epoch、尾部 loss 0.09–0.12；`not_published` 在 C 组 307 条里约占 60 条，要走“补发”分支的监督量不足。
- 结论：**C38 不能进 A/B/C 对照**（旧 C 在同批场景是 80/80）。下一步要么 C 组改 100 步，要么 60 步折中先看能否补上补发分支。

### 5.4 C60 训练与 test20（2026-11-14）

配置 `artifacts/configs/train_C60.yaml`：同样 `data_v2`（307 行）、`max_steps: 60`、`save_steps: 30`，其余同 C38。

- 2026-11-14 00:31 启动，`train_runtime` **2:08:20**（7701 s，128.4 s/step），`train_loss` **0.1346**，`epoch` **1.547**。
- 产物 `artifacts/adapters/C60/`：顶层 adapter + `checkpoint-30` + `checkpoint-60`。
- 与 C38 对比：loss 0.1775 → **0.1346**，epoch 0.99 → **1.55**；训练时长 1:22 → 2:08。

03:16 起的第一次 test20 在 03:18 权重加载完、容器随即重启（hostname 后缀 `71418` → `92921`）被杀死，
一行未写。为此给 `probe.py` 加了逐条落盘 + 断点续跑（`load_done`/`sink`），09:00 重跑。

test20 结果（18 条，`artifacts/runs/probe_C60_test20.jsonl`）：

| 族 | n | 平均步数 | list | publish | post_count | 撞上限 |
|---|---|---|---|---|---|---|
| normal | 4 | 3.0 | 1 | 1 | 1 | 0 |
| unknown_visible | 4 | 4.0 | 2 | 1 | 1 | 0 |
| unknown_delayed | 3 | 5.0 | 3 | 1 | 1 | 0 |
| **unknown_not_published** | 3 | **7.0** | **4** | **2** | **1** | **0** |
| unknown_resubmit_unknown | 4 | **16.0** | 12–13 | 2–3 | 0 | **4** |

汇总：`after_unknown_list` 14/14、`step_limit_hits` **4**（C38 是 7）、`mean_steps` 7.11、`parse_errors` 0。

判读：

- **补上了**：“查 3 次没有 → 补发一次”。`not_published` 从 C38 的 16 步/只查不发，变成 7 步（4 次 list + 2 次 publish，post_count=1）。
- **仍缺**：“补发后又 unknown → 再查 3 次 → 如实收尾”。`resubmit_unknown` 4 条全部 16 步撞上限（12–13 次 list、2–3 次 publish、post_count=0）。
  post_count 恰好等于期望值 0，但过程不收敛，按规则仍判 `FAILED_MODEL`。
- 两处 switch 的差别：第一处（首发 unknown 后第 3 次空）已学会；第二处（补发后再次 unknown 的第 3 次空）样本量相同但处于更长的上下文里，1.55 epoch 仍学不会。
- 与 C38 相比是明确进步：撞上限 7 → 4，平均步数 8.61 → 7.11。还剩 `resubmit_unknown` 一族未收敛。

### 5.5 B60 训练（2026-11-14）

配置 `artifacts/configs/train_B60.yaml`：从 `train_C60.yaml` 复制，只改 `dataset: tieba_B` 与 `output_dir: adapters/B60`；
其余（`dataset_dir: artifacts/data_v2`、`max_steps: 60`、`save_steps: 30`、LoRA r8/α16/target all、lr 5e-5、bs1×ga8、cutoff 8192）与 C60 完全一致。

数据：`artifacts/data_v2/tieba_B.json` 300 条纯 normal（`conversations` 全 8 条消息 = human + get + list + publish + gpt）。
日志 `Generating train split: 300 examples`、`tokenizer 300/300` 证实读的就是这份，没有碰 `tieba_C.json`（307 条）。

- 09:21 启动，`train_runtime` **1:23:20**（5000 s，83.3 s/step），`train_loss` **0.1359**，`epoch` **1.5867**。
- loss 曲线：前 10 步均值 0.3563、后 10 步 0.0522、最低 0.0438、最高 0.4805（step 1）；末 5 步 0.0556 / 0.0438 / 0.0589 / 0.0517 / 0.0490。
- 与 C60 对照：loss **0.1359 vs 0.1346**、epoch **1.587 vs 1.547**、单步 **83.3 s vs 128.4 s**。
  同 60 步、epoch 接近，但 B 的样本只有 3 步动作、每步 token 更少，所以时长少 45 分钟——**不能叫“算力对齐”**。
- 产物 `artifacts/adapters/B60/`：顶层 adapter + `checkpoint-30` + `checkpoint-60`。
- 备份：`artifacts/configs/train_B60.yaml`、`artifacts/runs/train_B60.log`、`artifacts/runs/B60_trainer_log.jsonl`。

## 6. P3 复核（final，已随本仓库备份）

`review/p3/` 下三件：`P3.md`（规范 v1）、`p3_review.json`（`status:"final"`）、`corrected_demos.jsonl`（3 条重放示范）。
结论：P2 交接的 5 条轨迹标签**全部判对**；v0 的两条初判（visible 判错、not_published 按 Q2 改判）已撤回。
Q1 有效发布口径、Q2 四族一律先查证、Q3 沿用 `step_limit=16`、Q4 标题截断属硬规则不计恢复能力。
重放：固定 `P2_handoff/p2_scnet/vendor/p1`，3 条示范 gate 3/3 pass；现行 validator 判 3/3 `SUCCESS_RECOVERED`，
v4 validator 判 3/3 `FAILED_MODEL`（差异只在 `validator.py:139` 是否把被拒发布计入 `max_publish_calls`）。

## 7. few-shot 门槛（已过，记录留档）

`artifacts/runs/fewshot-20261109.{jsonl,log,manifest.json,provisional.json}` 与
`fewshot_31415.{jsonl,review.json}`：10 条，7 成功（`SUCCESS_RECOVERED`），落在 PLAN §5.2 的 30–80% 区间；
逐族 delayed 2/2、not_published 3/3、visible 2/2、**resubmit_unknown 0/3**（这条要写进报告）。

## 8. 已知限制（写报告时必须交代）

1. **B/C 都是脚本专家蒸馏**（`ReferencePolicy` rollout），不是 PLAN 原设想的 human-corrected 模型失败轨迹；
   审核门禁是「环境真值 `classify` 自动打标」，不是人工逐条审核。P3 的 3 条人工修正示范已单独交付，
   **尚未并入 C 组**（并入需在冻结版 env 上重放，因为示范里含「长标题被拒」的旧口径步骤）。
2. **C 组 0 条重复帖失败样本**：重复帖能力只能从「unknown 后先查证」间接学，评测里的重复帖率才是检验。
3. **标签口径**：重复帖与 `max_publish_calls` 统一按「有效发布」计，需在 `validator.py:139` 固化并写进报告。
4. **§7.2 同构性预览校验**未做（模拟/真实后端逐字节比对观测）。
5. **真实确认只有 5–10 次**，不做统计显著性。

## 9. 环境与路径

- 本地仓库：`/Users/sara/Desktop/NLP/github-nlp-FinalProject`（子目录 `2026-10-23-19-57-32/` 是工程）。
- 集群：`/public/home/scnsjwf2qh/P2_SCNet_20261105/p2_scnet`（工程根），活动模拟器副本在
  **`vendor/p1/tieba_env`**（`p2.py:10` 把 `vendor/p1` 插进 `sys.path`；工程根没有 `tieba_env/`）。
- 模型：`/public/home/scnsjwf2qh/models/Qwen3.5-9B`；单卡 HCU 64 GB；`source /opt/dtk-26.04/env.sh`。
- 集群备份：`backup_pre_freeze/env_before_freeze.tgz`（旧 `vendor/p1/tieba_env` + `runs/sim`）、
  `backup_pre_freeze/p2.py.bak`。
- 集群 `p2.py` 已打两处补丁：`prepare` 的 origin 白名单加 `reference_policy`；`provenance.json` 记录
  `origins` 计数并把 `budget_note` 写明「B/C built from runs/sim/train.jsonl (ReferencePolicy rollouts)」。
