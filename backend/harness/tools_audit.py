"""代码审查工具集。

本文件只放**审查特有**的工具（run_tests / lint_file / apply_patch / record_finding）。
公共的读写与检索工具在 ``tools_common``，两个 agent 共用同一份实现。

暴露策略（渐进披露）：
静态前缀只放少量核心工具 + discover_tools；其余工具由模型检索后按需加载，
schema 追加到轨迹末尾，静态前缀保持字节稳定、缓存不失效。
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass
from .sandbox import CommandRunner, PathEscape, Workspace
from .tools import Risk, Tool, ToolRegistry, truncate_output
from .types import RunState, ToolResult
from .tools_common import (
    _tool_finish,
    _tool_git_diff,
    _tool_grep,
    _tool_list_files,
    _tool_read_file,
    _tool_read_skill,
    _tool_read_symbols,
    _tool_run_command,
    _tool_update_todo,
)

# =============================================================================
# 执行工具（审查特有）
# =============================================================================

def _tool_run_tests(workspace: Workspace, runner: CommandRunner) -> Tool:
    def handler(target: str = "", keyword: str = "", max_failures: int = 5) -> ToolResult:
        argv = ["python", "-m", "pytest", "-q", "--no-header"]
        if keyword:
            argv.extend(["-k", keyword])
        if target:
            argv.append(target)
        argv.append(f"--maxfail={max_failures}")

        result = runner.run(argv)
        if not result.ok:
            return result
        rendered, truncated, saved = truncate_output(
            str(result.data or ""), name="pytest", workspace=workspace
        )
        return ToolResult.success(rendered, truncated=truncated, saved_to=saved)

    return Tool(
        name="run_tests",
        summary="运行 pytest，返回失败用例摘要",
        description="""在受控环境中运行 pytest。

何时用：
- 验证「改动是否真的破坏了行为」——这是可靠度最高的证据，优先于任何静态推断。
- 复现报告里的失败用例。

何时不用：
- 只是想看语法是否正确时用 lint_file，更快。
- 项目不是 Python 或不用 pytest 时，改用 run_command 显式指定命令。

边界：
- 命令固定为 `python -m pytest`，不接受自定义可执行文件。
- 默认最多跑 5 个失败就停（maxfail），避免刷屏。
- 输出过长会截断并落盘。
- 只返回退出码与文本，不做「通过/失败」的语义判断，请自行读输出。

Returns: pytest 的原始输出（含 exit code）。""",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "测试文件或目录，例如 'tests/test_rate_limit.py'。留空表示全量"},
                "keyword": {"type": "string", "description": "传给 pytest -k 的表达式，例如 'rate_limit'"},
                "max_failures": {"type": "integer", "description": "最多失败几个就停，默认 5", "minimum": 1, "maximum": 50},
            },
        },
        handler=handler,
        risk=Risk.MEDIUM,
        examples=[{"keyword": "rate_limit"}, {"target": "tests", "max_failures": 3}],
        keywords=("test", "pytest", "verify", "测试", "验证", "用例"),
    )

def _tool_lint_file(workspace: Workspace, runner: CommandRunner) -> Tool:
    def handler(path: str, tool: str = "auto") -> ToolResult:
        try:
            resolved = workspace.resolve(path)
        except PathEscape as exc:
            return ToolResult.failure("PathEscape", str(exc), {"path": path})

        if not resolved.is_file():
            return ToolResult.failure(
                "FileNotFound", f"文件不存在：{path}", {"path": path},
                suggestion="先用 list_files 确认路径。",
            )

        findings: list[str] = []
        if resolved.suffix == ".py":
            source = resolved.read_text(encoding="utf-8", errors="replace")
            try:
                ast.parse(source)
            except SyntaxError as exc:
                findings.append(
                    f"[语法错误] 第 {exc.lineno} 行第 {exc.offset} 列：{exc.msg}"
                )
            if tool in {"auto", "ruff"}:
                # --no-cache 是必须的：ruff 默认会把缓存写到被检查的项目里
                # （.ruff_cache/），于是「审计」这个只读动作在别人的仓库里留下了文件。
                # 审计工具不该有这种副作用，实测已经踩到过。
                ruff = runner.run(
                    ["ruff", "check", "--no-cache", "--output-format", "concise", path]
                )
                if ruff.ok and str(ruff.data or "").strip():
                    findings.append(str(ruff.data).strip()[:4000])

        if not findings:
            return ToolResult.success(f"{path} 未发现语法或静态检查问题（检查工具：{tool}）。")
        return ToolResult.success(f"{path} 的检查结果：\n" + "\n".join(findings))

    return Tool(
        name="lint_file",
        summary="对单个文件做语法与静态检查",
        description="""对一个文件运行语法解析与（可用时）ruff 静态检查。

何时用：
- 写完补丁后立刻自检，这是最便宜的验证手段。
- 怀疑某文件有语法错误导致无法导入。

何时不用：
- 需要验证运行时行为时用 run_tests。
- 想查跨文件的类型问题，用 run_command 跑 pyright/mypy。

边界：
- 只检查语法与 lint，**不检查逻辑正确性**——「没有 lint 错误」不等于「改对了」。
- ruff 未安装时会静默跳过，只保留语法检查结果。
- 非 Python 文件目前只能给出很有限的结论。

Returns: 问题列表；没有问题则明确说「未发现」。""",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件相对路径"},
                "tool": {
                    "type": "string",
                    "description": "检查工具：auto / ruff / syntax",
                    "enum": ["auto", "ruff", "syntax"],
                },
            },
            "required": ["path"],
        },
        handler=handler,
        risk=Risk.MEDIUM,
        examples=[{"path": "src/middleware/rate_limit.py"}],
        keywords=("lint", "syntax", "check", "ruff", "检查", "语法", "静态"),
    )

def _tool_apply_patch(workspace: Workspace) -> Tool:
    def handler(path: str, old_string: str, new_string: str, expected_count: int = 1) -> ToolResult:
        try:
            resolved = workspace.resolve(path)
        except PathEscape as exc:
            return ToolResult.failure("PathEscape", str(exc), {"path": path})

        if not resolved.is_file():
            return ToolResult.failure(
                "FileNotFound", f"文件不存在：{path}", {"path": path},
                suggestion="补丁只能改已存在的文件。",
            )

        original = resolved.read_text(encoding="utf-8")
        occurrences = original.count(old_string)

        # 防呆：不唯一就拒绝，绝不「猜」改哪一处（书中禁止静默修正）
        if occurrences != expected_count:
            preview = old_string[:120].replace("\n", "\\n")
            return ToolResult.failure(
                "AmbiguousMatch",
                f"`old_string` 在文件中出现 {occurrences} 次，期望 {expected_count} 次。",
                {"path": path, "expected_count": expected_count, "actual_count": occurrences},
                suggestion="把 old_string 扩到足够长的上下文使其唯一，或显式设置 expected_count。"
                f"片段预览：{preview}",
            )

        updated = original.replace(old_string, new_string, expected_count)
        resolved.write_text(updated, encoding="utf-8")

        diff_lines = list(
            difflib.unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
                n=3,
            )
        )
        diff_text = "\n".join(diff_lines[:200])
        if len(diff_lines) > 200:
            diff_text += f"\n[... diff 共 {len(diff_lines)} 行，此处只显示前 200 行]"

        return ToolResult.success(
            f"已修改 {path}（替换 {expected_count} 处）。\n{diff_text}\n\n"
            "下一步建议：用 lint_file 做语法自检，必要时用 run_tests 验证行为。",
            params={"path": path},
        )

    return Tool(
        name="apply_patch",
        summary="对文件做精确的字符串替换补丁（不唯一则拒绝）",
        description="""用一个精确匹配的字符串替换来修改文件。

何时用：
- 修复已定位的问题，且改动是局部的、可精确描述的。

何时不用：
- 还没确认问题根因时不要改代码——先读代码、跑测试确认。
- 需要新增文件时本工具做不到（它只改已存在的文件）。

边界：
- `old_string` 在文件中必须**恰好**出现 expected_count 次；否则整个操作被拒绝，不会猜测位置。
- 不做任何格式化或自动修复，只做字符串替换。
- 改完不会自动验证；改完请自行跑 lint_file / run_tests。

Returns: 成功时返回 unified diff 与后续建议；失败时说明匹配了几次、如何修正。""",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目标文件相对路径"},
                "old_string": {
                    "type": "string",
                    "description": "要被替换的原文，必须逐字符匹配（含缩进）",
                },
                "new_string": {"type": "string", "description": "替换后的文本"},
                "expected_count": {
                    "type": "integer",
                    "description": "期望 old_string 出现的次数，默认 1",
                    "minimum": 1,
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=handler,
        risk=Risk.MEDIUM,
        examples=[
            {
                "path": "src/rate_limit.py",
                "old_string": "    await pool.release(conn);\n    return result",
                "new_string": "    return result\n  finally:\n    await pool.release(conn)",
            }
        ],
        keywords=("patch", "edit", "modify", "fix", "write", "修改", "补丁", "修复", "改写"),
    )

def _tool_record_finding(state: RunState) -> Tool:
    def handler(
        severity: str,
        file: str,
        line: int,
        title: str,
        detail: str,
        evidence: str,
        suggestion: str = "",
    ) -> ToolResult:
        from .types import Finding

        finding = Finding(
            severity=severity,  # type: ignore[arg-type]
            file=file,
            line=line,
            title=title,
            detail=detail,
            evidence=evidence,
            suggestion=suggestion,
        )
        is_new = state.add_finding(finding)
        counts = state.severity_counts()
        summary = f"当前共 {len(state.findings)} 条（阻断 {counts['blocker']} / 警告 {counts['warn']} / 建议 {counts['note']}）"
        if is_new:
            return ToolResult.success(f"已记录 {finding.id}：{title}\n{summary}")
        return ToolResult.success(f"该问题已存在（指纹重复），未重复记录。\n{summary}")

    return Tool(
        name="record_finding",
        summary="记录一条审计发现（severity/file/line/title/evidence 必填）",
        description="""把一条确认过的问题登记进审计报告。

何时用：
- 你已经在代码里看到了问题，且能给出**文件 + 行号 + 证据**。

何时不用：
- 只是怀疑、还没读到具体代码时，先继续调查再记录。
- 同一问题不要重复记录（工具会按「文件:行号:标题」去重）。

边界：
- `evidence` 必须是**代码或命令输出中的具体内容**，不接受「可能存在风险」这类主观推测。
- `line` 必须是真实行号，后续会被引用到报告里。
- severity 取值：blocker（必须修）/ warn（应该修）/ note（可以改进）。

Returns: 记录结果与当前累计条数。""",
        parameters={
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["blocker", "warn", "note"]},
                "file": {"type": "string", "description": "文件相对路径"},
                "line": {"type": "integer", "description": "问题所在行号", "minimum": 1},
                "title": {"type": "string", "description": "一句话结论，说明问题是什么"},
                "detail": {"type": "string", "description": "问题机理：为什么会出错、什么条件下触发"},
                "evidence": {"type": "string", "description": "直接证据：代码片段、测试输出或命令结果"},
                "suggestion": {"type": "string", "description": "修复建议（可选）"},
            },
            "required": ["severity", "file", "line", "title", "detail", "evidence"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[
            {
                "severity": "blocker",
                "file": "src/middleware/rate_limit.py",
                "line": 47,
                "title": "异常路径不归还连接，连接池会被耗尽",
                "detail": "acquire 之后的 await 抛错时 release 被跳过。",
                "evidence": "L45 acquire() 之后无 try/finally，L55 的 release 在错误路径上不可达。",
                "suggestion": "把 acquire/release 收进 try/finally。",
            }
        ],
        keywords=("finding", "record", "report", "bug", "记录", "发现", "问题", "上报"),
    )

# =============================================================================
# 组装
# =============================================================================

CORE_TOOLS = (
    "list_files",
    "read_file",
    "grep",
    "git_diff",
    "record_finding",
    "update_todo",
    "finish",
    "discover_tools",
)

@dataclass
class AuditToolbox:
    registry: ToolRegistry
    workspace: Workspace
    runner: CommandRunner
def build_toolbox(workspace: Workspace, state: RunState, runner: CommandRunner) -> AuditToolbox:
    """构建审计工具箱。核心工具进静态前缀，其余按需检索。"""
    from .tools import bind_discovery

    registry = ToolRegistry()

    registry.register(bind_discovery(registry), core=True)
    registry.register(_tool_list_files(workspace), core=True)
    registry.register(_tool_read_file(workspace), core=True)
    registry.register(_tool_grep(workspace), core=True)
    registry.register(_tool_git_diff(workspace, runner), core=True)
    registry.register(_tool_record_finding(state), core=True)
    registry.register(_tool_update_todo(state), core=True)
    registry.register(_tool_finish(state), core=True)

    registry.register(_tool_read_symbols(workspace))
    registry.register(_tool_run_tests(workspace, runner))
    registry.register(_tool_lint_file(workspace, runner))
    registry.register(_tool_apply_patch(workspace))
    registry.register(_tool_run_command(workspace, runner))
    registry.register(_tool_read_skill(workspace))

    return AuditToolbox(registry=registry, workspace=workspace, runner=runner)

def risk_of(registry: ToolRegistry, name: str) -> Risk:
    tool = registry.get(name)
    return tool.risk if tool else Risk.HIGH

__all__ = ["AuditToolbox", "build_toolbox", "risk_of", "CORE_TOOLS"]
