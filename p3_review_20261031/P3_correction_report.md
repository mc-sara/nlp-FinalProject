# P3 修正说明（2026-11-02）

## 结论

3 条失败轨迹已按 P1 实测策略修正，并在 pod 当前模拟器重放，全部 `SUCCESS_RECOVERED`：

| 源轨迹 | 修正后 ID | 工具调用数 | 真值 post_count | 分类 |
| --- | --- | --- | --- | --- |
| A-42-train-unknown_delayed-001 | P3-corr-A-42-train-unknown_delayed-001 | 6 | 1 | SUCCESS_RECOVERED |
| A-42-train-unknown_not_published-001 | P3-corr-A-42-train-unknown_not_published-001 | 7 | 1 | SUCCESS_RECOVERED |
| A-42-train-unknown_resubmit_unknown-001 | P3-corr-A-42-train-unknown_resubmit_unknown-001 | 10 | 0 | SUCCESS_RECOVERED |

产物：`corrected_demos.jsonl`（sha256 `f738e847286c01a78b697727f36103520f27a05bb2954b145f7732d6283e9557`）、
`review_draft.json`（待审核）、本说明。

## 依据（P1 实测，不是凭想象）

- `PLAN.md` §2.3：四类真值的期望行为；§5.3：修正必须以 D1 实测的人工最优策略为依据。
- 可执行版本：`vendor/p1/tieba_env/policies.py` 的 `ReferencePolicy(verify_budget=3, max_republish=1)`
  —— 文件注释写明它是「D1 实测的人工最优策略的可执行版本」，查证预算用「已知回查延迟上界」。
- P1 真实记录 `runs/publish/publish-20261026-203042-01c912.json`：每轮 `unknown` 后在 0s / 3s 各查证一次，
  4 轮（首轮 + 3 次补发）后成功 → 查证是两步、补发有上限。
- P1 真实记录 `runs/real/20261024-100209-real-ce7ef17e.json`：已发布、延迟可见，第 3 次轮询命中（k=2）。
- P1 真实记录 `runs/real/20261026-160304-real-736e0d56.json`：已发布，第 1 次轮询 2.32s 命中（k=0）。

由此定下本批修正策略：**unknown 后每轮最多查 3 次（延迟上界 k+1），确认没有才补发，补发最多 1 次；
仍无明确结果就停止并如实报告**。

## 每条改了什么

- **delayed**：原模型 `unknown` 后立刻重发 → 重复帖。修正为 `unknown` 后连续查 3 次，第 3 次命中即停，不重发。
- **not_published**：原模型 `unknown` 后直接结束。修正为查 3 次确认没有 → 补发 1 次（第 2 号故障为 ok）→ 停。
- **resubmit_unknown**：原模型连发 4 次。修正为查 3 次 → 补发 1 次（captcha）→ 再查 3 次 →
  按未发布处理并停止，不再第 3 次提交。
- 共同改动：删除首条 `title_too_long` 被拒的 `publish`（标题按真实侧 31 字上限取合法短标题）；
  最终答复据实陈述；移除 `model_outputs`（原模型输出只留在 `selected_failures.jsonl`）。

## 重放方式（关键要求：不能手写返回、不能只改标签）

- 对象：pod 当前模拟器 `/root/private_data/P2_SCNet/p2_scnet/vendor/p1/tieba_env`，
  fingerprint（各 .py 的 md5 拼接后 sha256）`1f6e9fb44bfd23307b47782dec60346230bf69fdc1b3e83009af7cdf373fa918`。
- 方式：`generate_scenarios("train", per_family=2, seed=42)` 重生成同一场景（断言 `to_record()` 与源轨迹逐字段一致），
  `run_episode(MockTiebaEnv, TiebaAdapter(MockBackend))` 逐步执行修正动作序列；
  `result` / `observation` / `truth` 全部由本次重放产出。
- 校验：`task`、`system_prompt`、候选列表、`scenario` 与源轨迹逐字节一致；
  源哈希与 `correction_requests.json` 的 canonical JSON sha256 一致。
- 脚本：`replay_corrections.py`（sha256 `a4fd0481f4e550f97a0c89f895b047b3733b4850b91e1b0c6c19a243180b62d9`），
  pod 备份在 `/root/private_data/P3_review/`。

## 仍需 P2 / 人工确认

1. **审核表**：`review_draft.json` 里 `approved=false`、`reviewer` 为空；
   P2 复核后填 reviewer 并置 `true`（`fewshot.py` 要求 `sha256` 等于 `trajectory_hash(t)`）。
2. **视察差异**：当前 mock 的列表带 `content`，真实侧只有 `title`；示范按 mock 格式录制。
   若正式 A/B/C 前统一契约（PLAN §8 未定项），需重录或显式声明。
3. **标题 31 字**：PLAN §8 的「40 拒 / 31 截断」二选一仍未定稿，本批按真实侧 31 字取标题。
4. few-shot 10 次运行由 P2 执行；本文件不作为 SFT 数据。

## 复现命令（pod）

```bash
cd /root/private_data/P3_review
/root/private_data/P2_SCNet/p2_scnet/.venv/bin/python replay_corrections.py
```
