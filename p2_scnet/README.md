# P2 代码备份（2026-10-29）

这是服务器当前代码的备份，用于协作和跨平台部署排查。已完成 Qwen3.5-9B 加载与100条模拟器轨迹采集，尚未完成 LoRA 微调或正式评测。不是可直接开始正式训练的最终版本。

## 模型下载

本仓库不上传权重。原始模型是 **Qwen/Qwen3.5-9B**：

- [ModelScope 下载页](https://modelscope.cn/models/Qwen/Qwen3.5-9B)
- [Hugging Face 下载页](https://huggingface.co/Qwen/Qwen3.5-9B)

环境配置完成后，在本目录运行 `bash run_scnet.sh download`，默认从 ModelScope 下载。已有完整模型可通过 `MODEL_DIR` 指定目录。默认 revision 为 master，严格复现须另外确认具体 revision；当前没有已核实的精确模型版本。尚未产出 B/C LoRA adapter。

## 如何使用

先阅读 [部署与迁移说明](DEPLOYMENT_20261029.txt)。该说明以原始交付压缩包为对象，其中提到的旧 README、versions、校验清单问题已在本 Git 备份目录中处理：重写 README、更新版本记录、移除过期清单和旧启动路径模板。原始代码包不在本仓库重复上传。

实测环境：SCNet 单卡64GB、PyTorch2.7.1、Python3.11、Ubuntu22.04、DTK26.04；实际推理使用 Hugging Face 后端。不要使用旧的 PyTorch2.9.0 镜像配置。其他平台按当地硬件安装 PyTorch，不要照搬 DTK 专用包。

`environment_reference.txt` 仅为环境比对参考，不应直接 `pip install -r`。其中 editable 路径已改为本目录相对路径。不同服务器不能通过复制 `.venv` 来复现驱动及平台环境。

模型、虚拟环境、缓存、原始轨迹、adapter 和备份环境均未上传，并通过本目录 `.gitignore` 排除。

## 已知问题：正式训练前必须处理

- 第一次标题超长被拒绝，却消耗故障注入位置。首轮80条故障场景中60条未触发 unknown，被分类器记为 FAILED_MODEL，不能据此推断模型恢复能力。
- 任务和候选列表重复，数据多样性不足。
- `qwen3_5_nothink` 模板下实际仍输出非空思考段，需确认并统一思考模式。
- 需补齐 P3 轨迹复核及3条示范/10次诊断的 few-shot 门槛。

先修复并进行小批量验证，再采集正式数据。B组仅正常成功，C组正常成功加恢复；三组统一模型、提示、工具、解码参数与测试集。

## 代码与来源

- `p2.py`：推理采集、审核清单、数据转换和训练配置。
- `run_scnet.sh`：setup/download/collect/fewshot/prepare/train-B/train-C/evaluate 入口。
- `vendor/p1`：项目模拟器相关源码快照。
- `vendor/LlamaFactory`：固定上游提交源码，保留许可证；含音频延迟导入的本地修改。
- `versions.json`：源提交和当前执行状态。

`VALIDATION.txt` 保留早期本地测试记录，只描述当时验证范围。最新项目状态以本 README 和 versions.json 为准。
