"""diff 增量审查：只审「改动的那几行 + 被它影响的调用点」。

## 为什么需要这个模式

全仓审查在真实团队里几乎用不上：一个 2000 行的仓库，每次改动平均只有几十行，
让 Agent 从头读一遍既贵又慢，而且会把**改动之外的老问题**一并倒出来 ——
那些在评审里属于噪声（"这条不是这次改的"）。

商用产品的默认工作模式都是增量的：CodeRabbit 只审 PR 的 diff，
Greptile 在 PR 到达前就维护好了仓库图。这里做的是轻量版：
拿 diff 定出「变更行」，再用 grep 反查谁调用了被改的符号，
把这两部分合成一份**审查简报**交给模型。

## 一个必须守住的边界

本模块**只做定位，不做判断**。它输出「哪里变了、谁在用它」，
不输出「这里有没有缺陷」—— 判断留给模型，判定留给判官层。
确定性只用在它真正擅长的地方：算 diff、找引用。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Hunk:
    """一处变更：文件 + 新增/删除的行号区间。"""

    file: str
    added: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)

    @property
    def start(self) -> int:
        return min(self.added) if self.added else 0

    def describe(self) -> str:
        if self.added:
            return f"{self.file}: 新增/修改第 {_range_text(self.added)} 行"
        return f"{self.file}: 删除了第 {_range_text(self.removed)} 行"


@dataclass
class DiffReport:
    base: str
    hunks: list[Hunk] = field(default_factory=list)
    changed_symbols: list[str] = field(default_factory=list)
    callers: list[tuple[str, str, int]] = field(default_factory=list)  # (file, line_text, lineno)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def files(self) -> list[str]:
        return sorted({h.file for h in self.hunks})

    def brief(self) -> str:
        """给模型看的审查简报。措辞要明确「只看这些」，否则模型仍会全仓扫一遍。"""
        if not self.ok:
            return ""
        if not self.hunks:
            return ""

        lines: list[str] = [
            "## 本次改动范围（增量审查模式）",
            "",
            "请**只审查下面列出的变更行，以及被这些变更影响的调用点**。",
            "改动之外的历史问题不在本次范围内，不要报告。",
            "",
            f"基准：`{self.base}`",
            f"涉及 {len(self.files)} 个文件：",
            "",
        ]
        for hunk in self.hunks:
            lines.append(f"- {hunk.describe()}")

        if self.changed_symbols:
            lines += ["", "改动涉及的符号：", "", "、".join(f"`{s}`" for s in self.changed_symbols[:20])]

        if self.callers:
            lines += [
                "",
                f"这些符号在**改动之外的调用点**（共 {len(self.callers)} 处，最相关的几条）：",
                "",
            ]
            for path, text, lineno in self.callers[:12]:
                lines.append(f"- `{path}:{lineno}` — `{text.strip()[:100]}`")
            lines += [
                "",
                "改动一个导出符号时，真正的缺陷常常出现在**作者没打开过的调用点**上 ——",
                "请顺着这些调用点确认参数含义、返回值约定是否还成立。",
            ]

        return "\n".join(lines)


def _range_text(nums: list[int]) -> str:
    """把 [3,4,5,9,10] 压成 `3-5, 9-10`。"""
    if not nums:
        return "—"
    nums = sorted(set(nums))
    spans: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = n
    spans.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(spans)


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$")

# 从新增行里抽「可能被外部引用的符号」：类型 / 函数 / 常量 / 方法定义
_SYMBOL = re.compile(
    r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)"
    r"|^\s*([A-Z][A-Z0-9_]{2,})\s*="
)


def parse_unified_diff(text: str) -> tuple[list[Hunk], list[str]]:
    """解析 `git diff --unified=0` 的输出。

    只认新增行号：删除行在「当前工作树」里已经不存在，拿它去核对会一律失败。
    """
    hunks: list[Hunk] = []
    symbols: list[str] = []
    current: Hunk | None = None
    new_line = 0

    for raw in text.splitlines():
        if raw.startswith("+++ "):
            match = _DIFF_FILE.match(raw)
            if match:
                current = Hunk(file=match.group(1))
                hunks.append(current)
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue

        header = _HUNK_HEADER.match(raw)
        if header:
            new_line = int(header.group(1))
            continue

        if current is None:
            continue

        if raw.startswith("+"):
            current.added.append(new_line)
            body = raw[1:]
            found = _SYMBOL.match(body)
            if found:
                name = found.group(1) or found.group(2)
                if name and name not in symbols:
                    symbols.append(name)
            new_line += 1
        elif raw.startswith("-"):
            current.removed.append(new_line)  # 仅作展示，不参与核对
        elif raw.startswith(" "):
            new_line += 1

    return [h for h in hunks if h.added or h.removed], symbols


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def collect_diff(
    root: Path,
    base: str = "HEAD",
    *,
    max_callers: int = 20,
) -> DiffReport:
    """收集 `base` 到工作树的变更，并反查被改符号的调用点。

    ``base`` 的常见取值：
    - ``HEAD``（默认）：只看未提交的改动；
    - ``HEAD~1``：看最近一次提交；
    - 任意 commit / 分支名。
    """
    report = DiffReport(base=base)
    root = Path(root).resolve()

    if not (root / ".git").exists():
        check = _git(root, "rev-parse", "--is-inside-work-tree")
        if check.returncode != 0:
            report.error = f"不是 git 仓库，无法做增量审查：{root}"
            return report

    # --unified=0 只给变更行本身，不给上下文 —— 我们要的正是「精确到行」。
    #
    # 比较对象的选择（这个区别在 CI 里是致命的）：
    # - 本地开发：``git diff <base>`` 比的是 base 与**工作树**，能包含未提交改动。这是默认行为。
    # - CI（PR）：代码已 checkout，工作树干净。``git diff <base>`` 会返回**空** ——
    #   于是过滤逻辑认为「没有改动」，把所有发现都丢掉，PR 上永远显示 0 条。
    #   所以 CI 场景必须比 base 与 **HEAD**。
    #
    # 判据：工作树有未提交改动 → 按本地语义（含工作树）；否则 → 比到 HEAD。
    # 这样两种场景都对，且不要求调用方多传一个参数。
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    has_worktree_changes = bool(dirty.stdout.strip())
    if has_worktree_changes:
        result = _git(root, "diff", "--unified=0", "--no-color", base)
    else:
        result = _git(root, "diff", "--unified=0", "--no-color", f"{base}..HEAD")
    if result.returncode != 0:
        report.error = f"git diff 失败：{result.stderr.strip()[:200]}"
        return report

    hunks, symbols = parse_unified_diff(result.stdout)
    report.hunks = hunks
    report.changed_symbols = symbols

    if not hunks:
        return report

    changed_files = set(report.files)
    for symbol in symbols[:8]:
        # -w 跳过空白差异，-F 把符号当字面量（避免 a.b 里的点被当通配）
        grep = _git(root, "grep", "-nF", "-w", "--", symbol)
        if grep.returncode != 0:
            continue
        for line in grep.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            path, lineno, text = parts
            # 自己文件里那几行不算「改动之外的调用点」
            if path in changed_files and int(lineno) in {
                n for h in hunks if h.file == path for n in h.added
            }:
                continue
            report.callers.append((path, text, int(lineno)))
            if len(report.callers) >= max_callers:
                break
        if len(report.callers) >= max_callers:
            break

    return report
