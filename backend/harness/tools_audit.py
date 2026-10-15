"""代码审计工具集。

按书中的分类法组织：
- **感知工具**：list_files / read_file / grep / git_diff / read_symbols / read_skill
- **执行工具**：run_tests / lint_file / apply_patch / run_command
- **协作与记录工具**：record_finding / update_todo / finish

工具描述的写法遵循第四章：核心是「什么时候用」，边界比能力更重要，
参数给具体例子，并把执行代价写清楚。

暴露策略（渐进披露）：
静态前缀只放少量核心工具 + discover_tools；其余工具由模型检索后按需加载，
schema 追加到轨迹末尾，静态前缀保持字节稳定、缓存不失效。
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sandbox import CommandRunner, PathEscape, Workspace
from .tools import Risk, Tool, ToolRegistry, truncate_output
from .types import RunState, ToolResult

# =============================================================================
# 文件类工具
# =============================================================================


def _tool_list_files(workspace: Workspace) -> Tool:
    def handler(
        path: str = ".",
        pattern: str = "*",
        recursive: bool = True,
        limit: int = 300,
    ) -> ToolResult:
        try:
            files, truncated = workspace.iter_files(path, pattern, recursive=recursive, limit=limit)
        except PathEscape as exc:
            return ToolResult.failure(
                "PathEscape", str(exc), {"path": path},
                suggestion="只能访问工作区内的路径，去掉 `..` 或改用相对路径。",
            )
        except FileNotFoundError as exc:
            return ToolResult.failure(
                "DirectoryNotFound", str(exc), {"path": path},
                suggestion="先 list_files('.') 看看目录结构。",
            )

        if not files:
            return ToolResult.success(
                f"目录 {path} 下没有匹配 `{pattern}` 的文件。"
                "（注意：默认跳过 .git / node_modules / __pycache__ 等目录）"
            )

        lines = []
        for item in files:
            try:
                size = item.stat().st_size
            except OSError:
                size = -1
            lines.append(f"{workspace.relative(item)}  ({size} B)")
        body = "\n".join(lines)
        if truncated:
            body += f"\n[... 已达到 {limit} 条上限，结果不完整；可缩小 path 或 pattern 再查]"
        return ToolResult.success(f"共 {len(files)} 个文件：\n{body}")

    return Tool(
        name="list_files",
        summary="列出目录下的文件（默认跳过 .git / node_modules 等）",
        description="""列出工作区内的文件路径与大小。

何时用：
- 需要先了解项目结构，再决定读哪些文件。

何时不用：
- 想按**内容**找东西时用 grep，本工具只看文件名与路径。
- 已经知道确切路径时直接 read_file。

边界：
- 只返回文件名与大小，不返回内容。
- 默认递归，且跳过 .git、node_modules、__pycache__、.venv、dist、build 等目录。
- 结果上限由 limit 控制，触顶时会在返回里明确标注不完整。

Returns: 每行一个 `相对路径 (字节数)`。""",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的目录，默认 '.'"},
                "pattern": {
                    "type": "string",
                    "description": "glob 模式，例如 '*.py'、'*.ts'、'test_*.py'。默认 '*'",
                },
                "recursive": {"type": "boolean", "description": "是否递归子目录，默认 true"},
                "limit": {"type": "integer", "description": "最多返回多少条，默认 300", "minimum": 1, "maximum": 2000},
            },
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{"path": "src", "pattern": "*.py"}, {"path": ".", "pattern": "*.md"}],
        keywords=("list", "files", "directory", "目录", "文件", "列出"),
    )


def _tool_read_file(workspace: Workspace) -> Tool:
    def handler(path: str, offset: int = 1, limit: int = 400) -> ToolResult:
        try:
            text, resolved = workspace.read_text(path)
        except PathEscape as exc:
            return ToolResult.failure(
                "PathEscape", str(exc), {"path": path},
                suggestion="只能读工作区内的文件。",
            )
        except FileNotFoundError as exc:
            return ToolResult.failure(
                "FileNotFound", str(exc), {"path": path},
                suggestion="用 list_files 或 grep 确认正确路径后再读。",
            )
        except ValueError as exc:
            return ToolResult.failure(
                "FileTooLarge", str(exc), {"path": path},
                suggestion="用 offset/limit 分段读取，例如 offset=1 limit=200。",
            )

        lines = text.splitlines()
        total = len(lines)
        start = max(1, offset)
        end = min(total, start + max(1, limit) - 1)
        chunk = lines[start - 1 : end] if start <= total else []
        numbered = "\n".join(f"{idx:>5} | {line}" for idx, line in enumerate(chunk, start=start))

        header = f"{workspace.relative(resolved)}  ·  第 {start}-{end} 行 / 共 {total} 行"
        if end < total:
            numbered += f"\n[... 还有 {total - end} 行未显示，用 offset={end + 1} 继续读取]"
        return ToolResult.success(f"{header}\n{numbered}")

    return Tool(
        name="read_file",
        summary="读取文件内容，带行号，支持分段",
        description="""读取工作区内一个文本文件，返回带行号的内容。

何时用：
- 已定位到目标文件，需要看实现细节。
- 需要引用具体行号作为证据。

何时不用：
- 不知道在哪个文件时，先用 grep 搜索。
- 只想看结构（函数/类清单）时用 read_symbols 更省 token。

边界：
- 单文件读取上限约 2 MB，超过请用 offset/limit 分段。
- 返回内容带 5 位宽行号前缀 `  12 | code`，引用证据时直接用这个行号。
- 超出 limit 的尾部不会返回，但会明确告诉你还剩多少行、怎么继续读。

Returns: 首行是 `路径 · 第 a-b 行 / 共 n 行`，其后是带行号的正文。""",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作区的文件路径，例如 'src/rate_limit.py'"},
                "offset": {"type": "integer", "description": "起始行号（从 1 开始），默认 1", "minimum": 1},
                "limit": {"type": "integer", "description": "最多读多少行，默认 400", "minimum": 1, "maximum": 3000},
            },
            "required": ["path"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{"path": "src/middleware/rate_limit.py"}, {"path": "README.md", "offset": 1, "limit": 60}],
        keywords=("read", "file", "open", "读取", "文件", "查看"),
    )


def _tool_grep(workspace: Workspace) -> Tool:
    def handler(
        pattern: str,
        path: str = ".",
        glob: str = "*",
        max_results: int = 80,
        ignore_case: bool = False,
    ) -> ToolResult:
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return ToolResult.failure(
                "InvalidRegex", f"正则表达式无法编译：{exc}", {"pattern": pattern},
                suggestion="检查括号、方括号是否闭合；搜索字面量时记得转义 . 和 (。",
            )

        try:
            files, _ = workspace.iter_files(path, glob or "*", limit=4000)
        except PathEscape as exc:
            return ToolResult.failure("PathEscape", str(exc), {"path": path})

        hits: list[str] = []
        scanned = 0
        hit_files: set[str] = set()
        capped = False

        for item in files:
            try:
                content = item.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            rel = workspace.relative(item)
            for lineno, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()[:220]}")
                    hit_files.add(rel)
                    if len(hits) >= max_results:
                        capped = True
                        break
            if capped:
                break

        if not hits:
            return ToolResult.success(
                f"在 {scanned} 个文件里没有匹配 `{pattern}` 的内容。\n"
                "可以尝试：放宽正则、改 glob（例如 '*.py'）、或确认 path 是否正确。"
            )

        header = f"匹配 {len(hits)} 处，分布在 {len(hit_files)} 个文件（已扫描 {scanned} 个文件）"
        if capped:
            header += f"\n[已达 {max_results} 条上限，结果不完整；请缩小 path/glob 或提高 max_results]"
        return ToolResult.success(f"{header}\n" + "\n".join(hits))

    return Tool(
        name="grep",
        summary="按正则搜索文件内容，返回 路径:行号:内容",
        description=r"""在工作区内按正则表达式搜索文本内容。

何时用：
- 找某个函数、变量、字符串在哪些地方出现。
- 追踪调用点、检查是否有遗漏的引用。

何时不用：
- 只按文件名找文件时用 list_files（本工具不搜文件名）。
- 已知文件与行号时直接 read_file。

边界：
- 逐行匹配，不支持跨行模式（默认不开启 DOTALL）。
- 默认只在文本文件内搜索；二进制文件会被当作乱码跳过。
- 结果有上限，触顶时会明确标注不完整。
- 搜索字面量里的 `.` `(` `)` `[` `]` 需要转义（例如 `acquire\(`）。

Returns: 每行 `相对路径:行号: 该行内容（截断到 220 字符）`。""",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python 正则，例如 'acquire\\(|release\\('"},
                "path": {"type": "string", "description": "搜索范围目录，默认 '.'"},
                "glob": {"type": "string", "description": "文件名过滤，例如 '*.py'。默认 '*'"},
                "max_results": {"type": "integer", "description": "最多返回多少条，默认 80", "minimum": 1, "maximum": 500},
                "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 false"},
            },
            "required": ["pattern"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{"pattern": "def check", "glob": "*.py"}, {"pattern": "TODO|FIXME", "max_results": 40}],
        keywords=("grep", "search", "regex", "find", "搜索", "查找", "匹配"),
    )


def _tool_git_diff(workspace: Workspace, runner: CommandRunner) -> Tool:
    def handler(rev_range: str = "HEAD", staged: bool = False, path: str = "") -> ToolResult:
        argv = ["git", "diff", "--no-color"]
        if staged:
            argv.append("--staged")
        if rev_range:
            argv.extend(rev_range.split())
        if path:
            argv.extend(["--", path])

        result = runner.run(argv)
        if not result.ok:
            return result
        text = str(result.data or "")
        if len(text.strip().splitlines()) <= 1:
            return ToolResult.success(
                f"`{' '.join(argv)}` 没有输出：该范围没有差异，或这不是一个 git 仓库。\n"
                "可尝试：staged=true 看暂存区，或 rev_range='origin/main...HEAD' 看分支差异。"
            )
        rendered, truncated, saved = truncate_output(text, name="git-diff")
        return ToolResult.success(rendered, truncated=truncated, saved_to=saved)

    return Tool(
        name="git_diff",
        summary="查看 git 差异（默认工作区未提交改动）",
        description="""查看代码变更。

何时用：
- 审计的目标是一段改动（PR / 提交）时，先看 diff 缩小范围。
- 确认自己刚才的补丁是否改成了预期样子。

何时不用：
- 需要的是文件的完整上下文时用 read_file；diff 只给变更片段，容易漏掉周边逻辑。

边界：
- 需要目标目录是 git 仓库，否则会报错。
- 输出过长时按「头 50 行 + 尾 50 行」截断，完整内容会落盘并给出路径。
- 默认看的是**未提交的工作区改动**；看已暂存用 staged=true；看分支差异用 rev_range。

Returns: 标准 unified diff 文本（可能被截断）。""",
        parameters={
            "type": "object",
            "properties": {
                "rev_range": {
                    "type": "string",
                    "description": "git 版本范围，例如 'HEAD~1'、'origin/main...HEAD'。默认 'HEAD'",
                },
                "staged": {"type": "boolean", "description": "是否看暂存区（--staged），默认 false"},
                "path": {"type": "string", "description": "限定路径，例如 'src/'。留空表示全部"},
            },
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{}, {"staged": True}, {"rev_range": "origin/main...HEAD", "path": "src"}],
        keywords=("git", "diff", "change", "patch", "变更", "差异", "改动"),
    )


def _tool_read_symbols(workspace: Workspace) -> Tool:
    def handler(path: str) -> ToolResult:
        try:
            resolved = workspace.resolve(path)
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except PathEscape as exc:
            return ToolResult.failure("PathEscape", str(exc), {"path": path})
        except OSError as exc:
            return ToolResult.failure("FileNotFound", str(exc), {"path": path})

        if resolved.suffix != ".py":
            return ToolResult.failure(
                "UnsupportedLanguage",
                f"read_symbols 目前只支持 Python 文件，收到 {resolved.suffix or '无扩展名'}。",
                {"path": path},
                suggestion="对非 Python 文件请直接用 read_file 或 grep。",
            )

        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            return ToolResult.success(
                f"{path} 解析失败（第 {exc.lineno} 行）：{exc.msg}\n"
                "这本身可能就是一个发现：文件存在语法错误，无法被导入。"
            )

        lines: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                lines.append(f"L{node.lineno:<5} {prefix} {node.name}{_signature(node)}")
            elif isinstance(node, ast.ClassDef):
                bases = ", ".join(_name_of(b) for b in node.bases)
                lines.append(f"L{node.lineno:<5} class {node.name}({bases})" if bases else f"L{node.lineno:<5} class {node.name}")
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                        lines.append(f"L{child.lineno:<5}   {prefix} {child.name}{_signature(child)}")

        if not lines:
            return ToolResult.success(f"{path} 里没有顶层函数或类定义。")
        return ToolResult.success(
            f"{path} 的符号清单（{len(lines)} 项）：\n" + "\n".join(lines)
        )

    return Tool(
        name="read_symbols",
        summary="列出 Python 文件的函数/类及签名（比读全文省 token）",
        description="""用 AST 解析 Python 文件，只输出顶层函数、类及其方法的名字、行号与参数签名。

何时用：
- 快速建立对一个大文件的结构认知，再决定读哪一段。
- 审计时判断「某个函数是否被改过签名」。

何时不用：
- 需要看实现逻辑时用 read_file。
- 非 Python 文件不支持，会直接报 UnsupportedLanguage。

边界：
- 只覆盖**顶层**定义与类的方法，不递归函数内部嵌套定义。
- 文件有语法错误时会如实报告，并把它当作一条可能的发现。
- 不解析类型注解的深层结构，只做文本级签名还原。

Returns: 每行 `L行号  def 名称(参数…)`，类的方法多缩进两格。""",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Python 文件相对路径"}},
            "required": ["path"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{"path": "src/lib/redis.py"}],
        keywords=("symbol", "ast", "function", "class", "signature", "符号", "函数", "类", "结构"),
    )


def _signature(node: ast.AST) -> str:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "()"
    args = node.args
    parts: list[str] = [a.arg for a in args.posonlyargs]
    if args.posonlyargs:
        parts.append("/")
    parts.extend(a.arg for a in args.args)
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    parts.extend(a.arg for a in args.kwonlyargs)
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    return f"({', '.join(parts)})"


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name_of(node.value)}.{node.attr}"
    return "?"


# =============================================================================
# 执行类工具
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
        rendered, truncated, saved = truncate_output(str(result.data or ""), name="pytest")
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
                ruff = runner.run(["ruff", "check", "--output-format", "concise", path])
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


def _tool_run_command(workspace: Workspace, runner: CommandRunner) -> Tool:
    def handler(argv: list[str], cwd: str = "") -> ToolResult:
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            return ToolResult.failure(
                "InvalidArguments",
                "argv 必须是字符串数组，例如 ['git','status','--short']。",
                {"argv": argv},
                suggestion="不要把整条命令写成单个字符串；本工具不经过 shell。",
            )
        result = runner.run(argv, cwd=cwd or None)
        if not result.ok:
            return result
        rendered, truncated, saved = truncate_output(str(result.data or ""), name="command")
        return ToolResult.success(rendered, truncated=truncated, saved_to=saved)

    return Tool(
        name="run_command",
        summary="执行白名单内的命令（不走 shell）",
        description="""执行一条命令并返回输出。用于 run_tests / lint_file 未覆盖的场景。

何时用：
- 需要跑 pyright、mypy、git log、npm test 等不在专用工具里的命令。
- 需要组合查看仓库状态。

何时不用：
- 跑测试优先用 run_tests，做语法检查优先用 lint_file（参数更省心）。

边界：
- `argv` 是数组，不走 shell；参数里出现 `; | & > < ` $` 等字符会被直接拒绝。
- 首元素必须在白名单内，默认包含：python, pytest, ruff, pyright, mypy, eslint, npm, node, go, git。
- 有超时限制；输出过长会截断并落盘。
- 这是**高风险**工具：不要用它做破坏性操作。

Returns: `$ 命令 (exit=N)` 加标准输出/错误。""",
        parameters={
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "命令与参数数组，例如 ['git','log','--oneline','-5']",
                },
                "cwd": {"type": "string", "description": "相对工作区的工作目录，留空用根目录"},
            },
            "required": ["argv"],
        },
        handler=handler,
        risk=Risk.HIGH,
        examples=[{"argv": ["git", "log", "--oneline", "-5"]}, {"argv": ["python", "-c", "print(1)"]}],
        keywords=("command", "exec", "shell", "cli", "命令", "执行", "运行"),
    )


# =============================================================================
# 记录与协作类工具
# =============================================================================


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


def _tool_update_todo(state: RunState) -> Tool:
    def handler(todos: list[dict], summary: str = "") -> ToolResult:
        from .types import Todo

        if not isinstance(todos, list):
            return ToolResult.failure(
                "InvalidArguments", "todos 必须是数组。", {"todos": todos},
                suggestion='格式：[{"text":"...","status":"pending"}]',
            )

        valid = {"pending", "in_progress", "completed", "cancelled"}
        rebuilt: list[Todo] = []
        for index, item in enumerate(todos, start=1):
            if not isinstance(item, dict) or "text" not in item:
                return ToolResult.failure(
                    "InvalidArguments",
                    f"第 {index} 项缺少 text 字段。",
                    {"item": item},
                    suggestion='每项形如 {"text":"读变更","status":"in_progress"}。',
                )
            status = str(item.get("status", "pending"))
            if status not in valid:
                return ToolResult.failure(
                    "InvalidArguments",
                    f"第 {index} 项的 status 非法：{status}",
                    {"item": item},
                    suggestion=f"status 只能是 {sorted(valid)} 之一。",
                )
            rebuilt.append(Todo(text=str(item["text"]), status=status))  # type: ignore[arg-type]

        state.todos = rebuilt
        listing = "\n".join(todo.to_line() for todo in state.todos) or "（空）"
        done = sum(1 for todo in state.todos if todo.status == "completed")
        head = f"任务列表已更新：{done}/{len(state.todos)} 完成"
        if summary:
            head += f"\n备注：{summary}"
        return ToolResult.success(f"{head}\n{listing}")

    return Tool(
        name="update_todo",
        summary="重写任务列表（每项含 text 与 status）",
        description="""用一份完整的新列表替换当前任务列表。

何时用：
- 开始审计时先列计划，中途更新进度，结束时收尾。
- 发现自己跑偏时，重排优先级。

何时不用：
- 不要用它做「追加」，每次都要给**完整列表**（这是有意的：迫使你重新审视全局）。

边界：
- 每次调用都会整体覆盖，之前的状态不会保留（除非你在新列表里写出来）。
- 状态取值：pending / in_progress / completed / cancelled。
- 建议保持 3~7 项；过长会稀释注意力。

Returns: 更新后的完整列表与完成计数。""",
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "任务描述"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                            },
                        },
                        "required": ["text"],
                    },
                    "description": "完整的任务列表",
                },
                "summary": {"type": "string", "description": "可选备注"},
            },
            "required": ["todos"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[
            {
                "todos": [
                    {"text": "读 PR 变更", "status": "completed"},
                    {"text": "追踪连接生命周期", "status": "in_progress"},
                    {"text": "运行测试验证", "status": "pending"},
                ]
            }
        ],
        keywords=("todo", "plan", "task", "计划", "任务", "进度"),
    )


def _tool_finish(state: RunState) -> Tool:
    def handler(summary: str) -> ToolResult:
        state.stop_reason = "finished"
        counts = state.severity_counts()
        return ToolResult.success(
            f"审计已结束。\n总结：{summary}\n"
            f"共 {len(state.findings)} 条发现（阻断 {counts['blocker']} / 警告 {counts['warn']} / 建议 {counts['note']}）"
        )

    return Tool(
        name="finish",
        summary="结束本次审计并给出总结",
        description="""声明审计完成，循环随即结束。

何时用：
- 目标已经达成：该看的地方都看过，发现都已记录。

何时不用：
- 还有没检查的改动、或还有没验证的假设时，不要为了收尾而调用它。
- 如果发现数为 0，也要在 summary 里说清楚「你检查了哪些范围、为什么足以判定没有问题」。

边界：
- 调用后循环立即结束，之后不会再执行工具。
- 不允许把它当作「放弃」的出口；无法完成时应在 summary 里如实说明卡在哪里。

Returns: 收尾确认与发现统计。""",
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "面向人的结论：检查了什么范围、主要发现、以及未覆盖的部分",
                }
            },
            "required": ["summary"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{"summary": "已审查 PR #482 的 7 个文件，发现 1 个阻断项（连接泄漏）与 2 个警告，未覆盖并发压测。"}],
        keywords=("finish", "done", "complete", "end", "结束", "完成", "收尾"),
    )


def _tool_read_skill(workspace: Workspace) -> Tool:
    skills_root = Path(__file__).parent / "skills"

    def handler(name: str, section: str = "") -> ToolResult:
        safe = re.sub(r"[^A-Za-z0-9_/-]", "", name)
        target = skills_root / safe
        if target.is_dir():
            target = target / "SKILL.md"
        if not target.suffix:
            target = target.with_suffix(".md")

        if not target.is_file():
            available = sorted(p.parent.relative_to(skills_root).as_posix()
                               for p in skills_root.rglob("*.md"))
            return ToolResult.failure(
                "SkillNotFound", f"没有名为 `{name}` 的技能文档。", {"name": name},
                suggestion=f"可用文档：{', '.join(available) or '（尚未放置技能文档）'}",
            )

        text = target.read_text(encoding="utf-8", errors="replace")
        if section:
            text = _extract_section(text, section) or text

        rendered, truncated, saved = truncate_output(
            text, name=f"skill-{safe.replace('/', '-')}"
        )
        return ToolResult.success(rendered, truncated=truncated, saved_to=saved)

    def _extract_section(text: str, heading: str) -> str:
        pattern = re.compile(
            rf"^#{{1,6}}\s*{re.escape(heading)}.*?$(.*?)(?=^#{{1,6}}\s|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(text)
        return match.group(0).strip() if match else ""

    return Tool(
        name="read_skill",
        summary="按需读取审计方法论文档（分级披露）",
        description="""读取本项目内置的审计方法论与规则文档。

何时用：
- 需要确认「这类问题该怎么判、按什么标准定级」时。
- 报告格式、定级口径不确定时。

何时不用：
- 常规代码阅读不要调用；这是方法论文档，不是代码。

边界：
- 只能读内置 skills 目录下的文档，不读任意文件。
- 文档较长时会截断并落盘。
- 可以先用 name 只看目录（例如 name='audit' 读 SKILL.md），再用 section 定位某一节。

Returns: 文档正文（可能截断）。""",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "文档名，例如 'audit' 或 'audit/references/severity'"},
                "section": {"type": "string", "description": "只取某个小节的标题文本（可选）"},
            },
            "required": ["name"],
        },
        handler=handler,
        risk=Risk.LOW,
        examples=[{"name": "audit"}, {"name": "audit/references/report-format", "section": "输出格式"}],
        keywords=("skill", "doc", "methodology", "rule", "技能", "文档", "方法论", "规则", "定级"),
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

    @property
    def core_names(self) -> tuple[str, ...]:
        return CORE_TOOLS


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


def tool_report(registry: ToolRegistry) -> str:
    """人类可读的工具清单，用于 CLI 展示。"""
    lines = []
    for tool in registry.all():
        tag = {Risk.LOW: "只读", Risk.MEDIUM: "写/执行", Risk.HIGH: "高风险"}[tool.risk]
        core = "核心" if tool.name in CORE_TOOLS else "按需"
        lines.append(f"  {tool.name:<16} [{tag:<6}] [{core}]  {tool.summary}")
    return "\n".join(lines)


__all__ = ["AuditToolbox", "build_toolbox", "risk_of", "tool_report", "CORE_TOOLS"]
