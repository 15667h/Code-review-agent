"""沙箱：路径约束与受控命令执行。

书中第四章的安全要点：
- 输入验证要**快速失败**，不尝试“智能修正”；
- 命令执行用白名单 + 不经过 shell（避免分号/管道注入）；
- 明确一点：**Python venv 不是沙盒**。这里的隔离是「路径边界 + 命令白名单 + 超时 + 输出上限」
  的进程级隔离，够用于本地审计，但不是容器级隔离。
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .types import ToolResult


class PathEscape(PermissionError):
    """路径越出工作区。"""


DEFAULT_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".idea",
    ".vscode",
}


@dataclass
class Workspace:
    """工作区：所有文件操作都必须落在这个根目录之内。"""

    root: Path
    skip_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_SKIP_DIRS))
    max_read_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser().resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(f"工作区不存在或不是目录：{self.root}")

    # ------------------------------------------------------------------ #

    def resolve(self, path: str) -> Path:
        """把相对路径解析为绝对路径，并确保没有越界。

        ``Path.resolve()`` 会展开 ``..`` 与符号链接，所以下面的
        ``relative_to`` 检查同时防住了目录遍历和软链接逃逸。
        """
        if path is None:
            raise PathEscape("路径不能为空")
        candidate = Path(path)
        if candidate.is_absolute():
            # 绝对路径也允许，但必须落在工作区内
            resolved = candidate.resolve()
        else:
            resolved = (self.root / candidate).resolve()

        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise PathEscape(
                f"路径越出工作区：{path} -> {resolved}（工作区根目录 {self.root}）"
            ) from exc
        return resolved

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    # ------------------------------------------------------------------ #

    def iter_files(
        self,
        subdir: str = ".",
        pattern: str = "*",
        *,
        recursive: bool = True,
        limit: int = 400,
    ) -> tuple[list[Path], bool]:
        """列出文件。返回 ``(文件列表, 是否被 limit 截断)``。"""
        base = self.resolve(subdir)
        if base.is_file():
            return [base], False

        results: list[Path] = []
        truncated = False
        iterator = base.rglob(pattern) if recursive else base.glob(pattern)

        for item in iterator:
            if any(part in self.skip_dirs for part in item.parts):
                continue
            if item.is_file():
                results.append(item)
                if len(results) >= limit:
                    truncated = True
                    break

        results.sort(key=lambda p: self.relative(p))
        return results, truncated

    def read_text(self, path: str) -> tuple[str, Path]:
        resolved = self.resolve(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"文件不存在：{path}")
        if resolved.stat().st_size > self.max_read_bytes:
            raise ValueError(
                f"文件过大（{resolved.stat().st_size} 字节，上限 {self.max_read_bytes}）。"
                "请用 offset/limit 分段读取。"
            )
        return resolved.read_text(encoding="utf-8", errors="replace"), resolved


# =============================================================================
# 命令执行
# =============================================================================

_SHELL_META = set(";|&><`$\n\r")


@dataclass
class CommandRunner:
    """受控命令执行器：白名单 + 不走 shell + 超时 + 输出上限。"""

    workspace: Workspace
    allow: set[str]
    timeout: float = 120.0
    max_output_chars: int = 60_000

    def run(self, argv: list[str], cwd: str | None = None) -> ToolResult:
        if not argv:
            return ToolResult.failure(
                "EmptyCommand", "argv 不能为空。", suggestion="给出至少一个可执行程序名。"
            )

        program = argv[0]
        base = os.path.basename(program).lower()
        if base not in self.allow:
            return ToolResult.failure(
                "CommandNotAllowed",
                f"`{program}` 不在白名单内。",
                {"argv": argv},
                suggestion=f"允许的命令：{', '.join(sorted(self.allow))}。"
                "若确实需要别的命令，请让使用者调整 AUDIT_ALLOW_COMMANDS。",
            )

        for token in argv:
            if any(ch in _SHELL_META for ch in token):
                return ToolResult.failure(
                    "SuspiciousArgument",
                    f"参数包含 shell 元字符：{token!r}。",
                    {"argv": argv},
                    suggestion="本执行器不经过 shell。请把命令拆成独立参数，例如用 ['grep','-rn','foo','.']。",
                )

        try:
            workdir = self.workspace.resolve(cwd) if cwd else self.workspace.root
        except PathEscape as exc:
            return ToolResult.failure("PathEscape", str(exc), {"cwd": cwd})

        if not workdir.is_dir():
            return ToolResult.failure(
                "BadWorkingDirectory",
                f"工作目录不存在：{cwd}",
                {"cwd": cwd},
                suggestion="确认目录存在，或省略 cwd 用工作区根目录。",
            )

        try:
            completed = subprocess.run(  # noqa: S603 - argv 已白名单校验且 shell=False
                argv,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failure(
                "Timeout",
                f"命令执行超过 {self.timeout:.0f} 秒已被终止。",
                {"argv": argv, "cwd": cwd},
                suggestion="缩小测试范围（例如只跑受影响的用例），或提高超时上限。",
            )
        except FileNotFoundError:
            return ToolResult.failure(
                "ExecutableNotFound",
                f"找不到可执行文件 `{program}`。",
                {"argv": argv},
                suggestion="确认该命令已安装并在 PATH 中。",
            )
        except OSError as exc:
            return ToolResult.failure("OSError", str(exc), {"argv": argv})

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        combined = stdout
        if stderr:
            combined = f"{stdout}\n[stderr]\n{stderr}" if stdout else stderr
        if len(combined) > self.max_output_chars:
            combined = combined[: self.max_output_chars] + "\n[输出已按上限截断]"

        header = f"$ {shlex.join(argv)}  (exit={completed.returncode})"
        return ToolResult.success(
            f"{header}\n{combined}",
            params={"argv": argv},
        )
