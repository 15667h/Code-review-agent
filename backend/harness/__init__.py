"""Margin Harness —— 自研代码审计 / 架构分析 Agent 的运行时。

设计依据：**Agent = Model + Harness**
    Harness = 上下文管理 + 工具接口 + 约束 + 验证 + 纠正

## 模块地图

**入口**
    __main__     命令行（审查 / 架构分析 / diff 增量 / 离线模式）
    server       HTTP 服务：同源托管前端 + API + SSE 事件流

**运行时内核**
    loop         ReAct 主循环；收尾按「位置核验 → 判官复核 → 证据核验」依次做
    types        数据结构（消息 / 工具调用 / 发现 / 蓝图 / 运行状态）
    model        OpenAI 兼容端点的调用与重试（不绑定任何厂商）
    config       配置加载：接口地址 / API Key / 模型名 三项
    context      上下文构造（静态前缀 + 轨迹 + 状态栏）与压缩
    budget       token 估算
    pacing       轮次预算：按剩余比例提醒模型收尾

**工具**
    tools        工具基类与注册表（含按需检索 discover_tools）
    tools_common 两个助手共用的只读工具（read_file / grep / list_files / sketch…）
    tools_audit  审查助手特有（record_finding / lint_file…）
    tools_design 架构助手特有（record_layer / record_component / record_decision…）
    sketch       一次 ast 扫描产出仓库结构清单（分层前的中间制品）
    sandbox      路径围栏与命令白名单

**质量约束（三层，按成本从低到高）**
    guardrails   确定性核验：引用位置、行号内容、完成声明的自证
    judge        独立判官：只删不加，逐条问「这段代码能证明这个结论吗」
    arch_check   架构产出的证据核验 + 权衡真实性（稻草人备选 / 只写好处）

**助手声明**
    agents       可插拔 Agent 声明（AgentSpec）：加一个助手 = 写一份 spec
    diffmode     增量审查：算变更行 → 抽符号 → 反查改动之外的调用点
    offline      离线规则模型（无 API Key 时的确定性降级）
    rules        规则集，兼作 LLM 结果的交叉验证器

**输出与账号**
    report       报告渲染（按 outcome 分派，未知形态有兜底）
    auth         scrypt 加盐哈希 + 服务端内存会话（零依赖）
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
