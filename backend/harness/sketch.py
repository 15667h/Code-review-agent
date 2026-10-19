"""架构速写：一次机械扫描产出「仓库结构清单」，供架构助手在分层之前先看全貌。

## 为什么需要

ROS2 架构恢复那篇（arXiv 2605.20055）的做法值得抄：**先产出结构化的中间制品
（节点清单 / launch 依赖），再基于制品做分层合成**。理由很实在 ——
让模型一边读文件一边画架构，读到一半就撞轮次上限；而先拿到一份紧凑的
符号清单，它可以在很少的轮次里看到全局，把预算留给「判断」而不是「收集」。

这与本项目的一条既有原则一致：**能确定性判定的不交给模型**。
文件里有哪些类、哪些函数、import 了什么，用 ast 就能算准，没必要让模型
逐文件读一遍再自己汇总。

## 边界

只做结构提取，**不做任何架构判断**：不猜分层、不猜组件职责。
输出的是清单，不是方案。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

# 扫描时跳过的目录：与 Workspace 的默认跳过保持一致，
# 否则清单里会出现 .venv / node_modules 这类噪声
SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "dist", ".tox",
}

MAX_FILES = 400


@dataclass
class FileSketch:
    path: str
    lines: int = 0
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    doc: str = ""


def _module_doc(tree: ast.Module) -> str:
    doc = ast.get_docstring(tree) or ""
    return doc.strip().splitlines()[0][:80] if doc.strip() else ""


def sketch_file(path: Path, root: Path) -> FileSketch | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # 语法错误本身是信息，但不在速写的职责里 —— 由审查助手去管
        return None

    rel = str(path.relative_to(root)).replace("\\", "/")
    out = FileSketch(path=rel, lines=len(text.splitlines()), doc=_module_doc(tree))

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.functions.append(node.name)
        elif isinstance(node, ast.Import):
            out.imports.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                out.imports.append(node.module.split(".")[0])

    # 去重但保序：import 顺序有时能读出「谁先被依赖」
    seen: set[str] = set()
    out.imports = [i for i in out.imports if not (i in seen or seen.add(i))]
    return out


def sketch_repo(root: Path, *, limit: int = MAX_FILES) -> list[FileSketch]:
    """扫描仓库里的 Python 文件，按路径排序返回。"""
    root = Path(root).resolve()
    out: list[FileSketch] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        item = sketch_file(path, root)
        if item is not None:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def render(sketches: list[FileSketch], *, max_files: int = 120) -> str:
    """把速写渲染成紧凑文本。

    格式刻意做成一目了然的缩进清单：模型要能快速扫过并形成「谁依赖谁」的印象，
    所以每个文件只占 1–3 行，且把 import 缩到最必要的几个。
    """
    if not sketches:
        return "（这个仓库里没有可解析的 Python 文件）"

    # 只保留被多个文件共同引用的内部模块，避免把 requests/typing 这类噪声全列出来
    internal = {s.path.split("/")[0] for s in sketches}
    lines: list[str] = [f"共 {len(sketches)} 个 Python 文件。", ""]

    for item in sketches[:max_files]:
        head = f"{item.path}  ({item.lines} 行)"
        if item.doc:
            head += f"  — {item.doc}"
        lines.append(head)

        if item.classes:
            lines.append("  类：" + "、".join(item.classes[:10]))
        if item.functions:
            names = item.functions[:12]
            more = "" if len(item.functions) <= 12 else f" 等 {len(item.functions)} 个"
            lines.append("  函数：" + "、".join(names) + more)
        deps = [i for i in item.imports if i in internal]
        if deps:
            lines.append("  内部依赖：" + "、".join(deps[:8]))

    if len(sketches) > max_files:
        lines.append("")
        lines.append(f"（还有 {len(sketches) - max_files} 个文件未列出，可用 list_files 查看）")

    return "\n".join(lines)
