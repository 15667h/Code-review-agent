"""仓库卫生检查：找死代码与未使用的导入。

开源项目最容易积累的两类垃圾：

- **死函数**：定义了、曾经调用过、后来调用点删了但定义留着。
  读代码的人会花时间理解它为什么存在，改的时候还不敢删。
- **未使用导入**：重构后残留，误导人以为这个模块依赖了某个东西。

两者都难靠 review 发现（大文件里一眼看不出来），所以做成可重复执行的检查。

用法：

    cd backend
    python scripts/check_hygiene.py          # 报告问题，有问题时退出码 1
    python scripts/check_hygiene.py --fix    # 只打印建议，不自动改（保守）

## 判据的局限（必须知道）

`importlib` 与 `getattr` 这类动态引用扫不到，所以：

- 框架回调（`do_GET`、`_on_tool`）永远「看起来没人用」—— 按命名约定排除
- 测试文件里被 pytest 收集的 `test_*` 同理

因此这个工具只报**可疑项**，不自动删除。判断「是不是真死了」仍然需要人看一眼。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 扫描时跳过的目录：样本仓库与上游克隆不属于本项目的代码
SKIP_DIRS = {
    "node_modules",
    ".workbuddy",
    ".runs",
    "__pycache__",
    ".venv",
    "examples",
    "arch-cases",
    ".pytest_cache",
}

#: 按约定永远「没人调用」但必须存在的方法名
CALLBACK_PATTERN = re.compile(
    r"^(do_(GET|POST|PUT|DELETE|HEAD|OPTIONS)|_on_[a-z_]+|__[a-z_]+__|"
    r"handler|main|post_init|setup|teardown|log_message)$"
)


def _iter_files() -> list[Path]:
    out: list[Path] = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        out.append(path)
    return sorted(out)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def check_dead_functions(files: list[Path]) -> list[tuple[str, int, str]]:
    """找出定义了但全项目只出现一次的函数/方法。"""
    sources = {p: p.read_text(encoding="utf-8", errors="replace") for p in files}
    corpus = "\n".join(sources.values())
    suspects: list[tuple[str, int, str]] = []

    for path, text in sources.items():
        if path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            if CALLBACK_PATTERN.match(name) or name.startswith("test_"):
                continue
            if len(re.findall(rf"\b{re.escape(name)}\b", corpus)) <= 1:
                suspects.append((_rel(path), node.lineno, name))
    return suspects


def check_unused_imports(files: list[Path]) -> list[tuple[str, int, str]]:
    """找出导入了但正文里没再出现的名字。"""
    out: list[tuple[str, int, str]] = []
    for path in files:
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        imported: list[tuple[str, int]] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.append((alias.asname or alias.name.split(".")[0], node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    imported.append((alias.asname or alias.name, node.lineno))

        import_lines = {line for _, line in imported}
        body = "\n".join(
            line
            for index, line in enumerate(src.splitlines(), start=1)
            if index not in import_lines
        )
        for name, line in imported:
            if not re.search(rf"\b{re.escape(name)}\b", body):
                out.append((_rel(path), line, name))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="仓库卫生检查（死代码 / 未用导入）")
    parser.add_argument("--fix", action="store_true", help="保留参数：目前只报告，不自动修改")
    args = parser.parse_args(argv)

    files = _iter_files()
    print(f"扫描 {len(files)} 个 Python 文件（跳过 examples / arch-cases / 虚拟环境）\n")

    dead = check_dead_functions(files)
    unused = check_unused_imports(files)

    if dead:
        print(f"可疑死函数 {len(dead)} 个（可能是动态调用，需人工确认）：")
        for path, line, name in dead:
            print(f"  {path}:{line}  {name}()")
        print()
    else:
        print("没有可疑的死函数。\n")

    if unused:
        print(f"未使用导入 {len(unused)} 个：")
        for path, line, name in unused:
            print(f"  {path}:{line}  {name}")
        print()
    else:
        print("没有未使用的导入。\n")

    # 未使用导入是确定性问题，直接判失败；死函数是可疑项，只报告
    if args.fix:
        print("（--fix 目前不自动修改：删除死函数的风险由人判断更稳妥）")

    return 1 if unused else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] if len(sys.argv) > 1 else None))
