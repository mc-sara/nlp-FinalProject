"""贴吧 Agent 工具恢复实验：模拟器与适配层。

模块划分：
    types      工具契约常量、FaultSpec、Post / PublishRecord / PublishResult
    env        MockTiebaEnv —— 内存帖子库 + 故障注入 + 回查延迟
    tools      模型可见的三个工具 schema 与 system prompt（不含任何策略暗示）
    adapter    TiebaAdapter —— 把 mock / real 两个后端归一到同一套观测字符串
    scenarios  五种场景族生成器（训练 / 测试隔离）
    validator  按环境真值做五类轨迹分类与指标聚合
    runner     轨迹采集循环（Policy 协议 + run_episode）
    policies   参考策略 / 各类坏策略，用于自检与基线对照
"""

from .types import (
    ALL_STATUSES,
    ERROR_REASONS,
    REJECTED_REASONS,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    UNKNOWN_REASONS,
    FaultRates,
    FaultSpec,
    Post,
    PublishRecord,
    PublishResult,
)
from .env import MockTiebaEnv
from .adapter import (
    BackendContractError,
    MockBackend,
    ModelToolCallError,
    RealBackend,
    TiebaAdapter,
    dumps_observation,
)
from .scenarios import (
    FAMILIES,
    FAMILY_NORMAL,
    FAMILY_UNKNOWN_DELAYED,
    FAMILY_UNKNOWN_NOT_PUBLISHED,
    FAMILY_UNKNOWN_RESUBMIT_UNKNOWN,
    FAMILY_UNKNOWN_VISIBLE,
    Scenario,
    generate_scenarios,
)
from .validator import (
    CLASS_DUPLICATE_POST,
    CLASS_FAILED_MODEL,
    CLASS_FAILED_TOOL,
    CLASS_SUCCESS_NORMAL,
    CLASS_SUCCESS_RECOVERED,
    classify,
    episode_metrics,
    summarize,
)
from .runner import FinalAction, Policy, ToolAction, run_episode

__all__ = [
    "ALL_STATUSES",
    "UNKNOWN_REASONS",
    "REJECTED_REASONS",
    "ERROR_REASONS",
    "STATUS_OK",
    "STATUS_UNKNOWN",
    "STATUS_REJECTED",
    "STATUS_ERROR",
    "FaultSpec",
    "FaultRates",
    "Post",
    "PublishRecord",
    "PublishResult",
    "MockTiebaEnv",
    "TiebaAdapter",
    "MockBackend",
    "RealBackend",
    "dumps_observation",
    "ModelToolCallError",
    "BackendContractError",
    "Scenario",
    "generate_scenarios",
    "FAMILIES",
    "FAMILY_NORMAL",
    "FAMILY_UNKNOWN_VISIBLE",
    "FAMILY_UNKNOWN_DELAYED",
    "FAMILY_UNKNOWN_NOT_PUBLISHED",
    "FAMILY_UNKNOWN_RESUBMIT_UNKNOWN",
    "classify",
    "episode_metrics",
    "summarize",
    "CLASS_SUCCESS_NORMAL",
    "CLASS_SUCCESS_RECOVERED",
    "CLASS_FAILED_MODEL",
    "CLASS_DUPLICATE_POST",
    "CLASS_FAILED_TOOL",
    "run_episode",
    "Policy",
    "ToolAction",
    "FinalAction",
]
