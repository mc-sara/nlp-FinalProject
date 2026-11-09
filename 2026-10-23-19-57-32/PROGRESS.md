# 进度备份（2026-11-09 21:50）

本文件记录**当前真实状态**，包括已作废的实验，避免后面误当结论用。

## 一句话状态

口径（标题 31 字截断 / 吧名 agenttest / origin 标注）已冻结并落地到代码与数据；训练数据已按新口径重建；
**C 组 adapter 是旧口径数据训出来的，作废**，只当管线验证；A/B/C 正式评测尚未开始。

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

## 2. 数据（新口径，已重建并上传）

- `runs/sim/{train,test}.jsonl`：各 100 条（5 族 × 20；正常 20 / 恢复 80），kw 全 `agenttest`，
  标题全部 ≤31 字，`origin` 全 `reference_policy`。生成命令 `python demo.py --build --per-family 20`
  （`BUILD_SEEDS = {"train": 20261109, "test": 777001}`）。配套 `_sharegpt.jsonl`、`_metrics.json` 已重写。
- `artifacts/data_v2/`：`tieba_B.json` 20 条 / `tieba_C.json` 100 条 / `dataset_info.json` /
  `provenance.json`（`origins {"reference_policy": 100}`）。审核表 `artifacts/runs/sim_train_review_v2.json`
  （100 条，`approved=true`，label 由环境真值 `classify` 得出，reviewer 记 `P2 auto-check 2026-11-09 (env-truth classify); P3 sign-off pending`）。
- 旧 `artifacts/data`（集群上，未入仓）是旧口径数据，保留未删；新训练一律用 `artifacts/data_v2`。

## 3. 训练：C 组已跑完但作废

- 运行：2026-11-09 20:12:05 → 21:24（100 步，`train_runtime` 1:08:15 ≈ 41 s/step，
  `train_loss` 0.04848，`epoch` 7.72）。日志 `artifacts/runs/train_C.log`，配置 `artifacts/configs/train_C.yaml`。
- 产物（集群 `artifacts/adapters/C/`，637 MB）：`adapter_model.safetensors` 86 MB，
  sha256 前 24 位 `0fbe1a61438a84d2f04ffd69`。
- **作废理由**：训练数据是旧口径 `artifacts/data`——超长标题被 mock 判 `rejected`、吧名 `测试`，
  与刚冻结的口径不一致；且 100 步 × bs1 × ga8 对 100 条样本是 **8 epoch**，样本本身又全是
  `ReferencePolicy` 首跑正确的蒸馏轨迹（0 条重复帖失败样本、0 条模型失败修正样本）。
- 因此：该 adapter 只能说明「LoRA 管线能跑通」，不构成任何关于恢复策略的结论。
- 未训练 B 组、未跑 A/B/C 评测；`chain_after_C.sh` 已于 20:49 kill（`artifacts/runs/chain.log`）。

## 4. P3 复核（final，已随本仓库备份）

`review/p3/` 下三件：`P3.md`（规范 v1）、`p3_review.json`（`status:"final"`）、`corrected_demos.jsonl`（3 条重放示范）。
结论：P2 交接的 5 条轨迹标签**全部判对**；v0 的两条初判（visible 判错、not_published 按 Q2 改判）已撤回。
Q1 有效发布口径、Q2 四族一律先查证、Q3 沿用 `step_limit=16`、Q4 标题截断属硬规则不计恢复能力。
重放：固定 `P2_handoff/p2_scnet/vendor/p1`，3 条示范 gate 3/3 pass；现行 validator 判 3/3 `SUCCESS_RECOVERED`，
v4 validator 判 3/3 `FAILED_MODEL`（差异只在 `validator.py:139` 是否把被拒发布计入 `max_publish_calls`）。

## 5. few-shot 门槛（已过，记录留档）

`artifacts/runs/fewshot-20261109.{jsonl,log,manifest.json,provisional.json}` 与
`fewshot_31415.{jsonl,review.json}`：10 条，7 成功（`SUCCESS_RECOVERED`），落在 PLAN §5.2 的 30–80% 区间；
逐族 delayed 2/2、not_published 3/3、visible 2/2、**resubmit_unknown 0/3**（这条要写进报告）。

## 6. 已知缺口（训练/评测前必须处理）

1. **C 组人工修正轨迹不足**：PLAN §5.3/D4 要求 10 条人工修正轨迹，目前只有 P3 的 3 条（且 review 仍 `approved:false`，未并入）。
2. **B 组定义未定**：20 条 × 100 步 = 40 epoch 会过拟合；对齐 8 epoch 需要 100 条正常轨迹（`= steps × bs × ga ÷ epochs = 100×1×8÷8`）。
3. **标签口径**：正式评测的重复帖计数用「有效发布」，需在 `validator.py:139` 固化并写进报告。
4. **§7.2 同构性预览校验**、**D6 训后真实确认 5–10 次** 均未做。

## 7. 环境与路径

- 本地仓库：`/Users/sara/Desktop/NLP/github-nlp-FinalProject`（子目录 `2026-10-23-19-57-32/` 是工程）。
- 集群：`/public/home/scnsjwf2qh/P2_SCNet_20261105/p2_scnet`（工程根），活动模拟器副本在
  **`vendor/p1/tieba_env`**（`p2.py:10` 把 `vendor/p1` 插进 `sys.path`；工程根没有 `tieba_env/`）。
- 集群备份：`backup_pre_freeze/env_before_freeze.tgz`（旧 `vendor/p1/tieba_env` + `runs/sim`）、
  `backup_pre_freeze/p2.py.bak`。
- 集群 `p2.py` 已打两处补丁：`prepare` 的 origin 白名单加 `reference_policy`；`provenance.json` 记录
  `origins` 计数并把 `budget_note` 写明「B/C built from runs/sim/train.jsonl (ReferencePolicy rollouts)」。
