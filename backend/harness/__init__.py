"""Margin Harness —— 自研代码审计 Agent 的运行时。

设计依据：《深入理解 AI Agent：设计原理与工程实践》
    Agent = Model + Harness
    Harness = 上下文管理 + 工具接口 + 约束 + 验证 + 纠正

分层：
    types      数据结构（消息 / 工具调用 / 发现 / 运行状态）
    budget     token 估算与预算
    model      Provider 抽象与 DeepSeek 实现
    tools      工具基类、注册表、审计工具集
    context    上下文构造（静态前缀 + 轨迹 + 状态栏）与压缩
    guardrails 约束 / 验证 / 纠正三件套
    loop       ReAct 主循环
    audit      发现模型与报告渲染
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
