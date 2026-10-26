"""模型可见的三个工具定义与 system prompt。

两条硬约束（PLAN.md 2.1 / 2.4）：
  - 只有三个工具。**不提供按请求 ID 查状态的接口**：真实工具没有这个能力，
    提供了会让模拟环境训出的策略在真实侧直接失效。
  - system prompt 只给任务目标和工具说明，**不得写入"状态不确定时应先查证"这类
    具体策略**。否则 C 组的提升可能只来自训练数据隐含的规则，而不是条件推理。

注意工具描述的分寸：只陈述"能做什么"，不暗示"什么时候该做什么"。
也不主动声明"没有查询状态的接口"——这是能力的缺失，不该被当成提示明说。
"""

from __future__ import annotations

TOOL_NAMES = ("get_candidates", "list_recent_posts", "publish")

# OpenAI / vLLM function-calling 格式，Qwen 系列可直接用
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_candidates",
            "description": "取候选热梗列表。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_posts",
            "description": "读取指定吧的最新帖子列表，返回帖子标题与正文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kw": {"type": "string", "description": "吧名，例如「测试」"},
                    "limit": {"type": "integer", "description": "最多返回多少条，默认 20"},
                },
                "required": ["kw"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish",
            "description": "在指定吧提交一个主题帖，返回本次提交的状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kw": {"type": "string", "description": "吧名"},
                    "title": {"type": "string", "description": "帖子标题，不超过 40 字"},
                    "content": {"type": "string", "description": "帖子正文"},
                },
                "required": ["kw", "title", "content"],
            },
        },
    },
]

SYSTEM_PROMPT = (
    "你是一个贴吧发帖助手，可以调用工具完成发帖任务。"
    "请在结束后用一两句话说明你做了什么、最终结论是什么。"
)

# 训练数据里的 system prompt 必须与此一致（PLAN.md 4.2「三组必须完全一致的条件」）
DEFAULT_TASK_TEMPLATE = "从候选中选一个热梗，发布到「{kw}」吧。发布前检查重复。"
