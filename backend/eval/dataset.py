"""评估数据集。

标准答案来自**人工标注**，而不是检测规则 —— 否则就是「用规则评估规则」，
指标会虚高且没有诊断价值。

标注写在样本代码的注释里，格式：

    # 缺陷 SEC001-a：凭据硬编码在源码里
    # 缺陷 RES002-b：同样缺少 finally

解析这些标注得到 ground truth。这样标注与实现待在同一个地方，
改代码时不容易忘记同步。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

LABEL_RE = re.compile(
    r"#\s*缺陷\s+(?P<code>[A-Z]+\d+(?:-[a-z])?)[：:]\s*(?P<desc>.+)"
)


@dataclass(frozen=True)
class ExpectedFinding:
    code: str  # 形如 SEC001-a：规则号 + 序号
    file: str
    line: int
    description: str

    @property
    def rule_id(self) -> str:
        return self.code.split("-", 1)[0]

    @property
    def severity_hint(self) -> str:
        prefix = self.rule_id[:3]
        if prefix in {"SEC"} and self.rule_id in {"SEC001", "SEC002"}:
            return "blocker"
        if prefix in {"ERR", "RES", "PY"} and self.rule_id in {"ERR001", "ERR002", "RES001", "PY001", "SEC003"}:
            return "warn"
        return "note"


@dataclass
class EvalCase:
    id: str
    repo: Path
    goal: str
    expected: list[ExpectedFinding] = field(default_factory=list)

    def expected_in(self, file_suffix: str) -> list[ExpectedFinding]:
        return [item for item in self.expected if item.file.endswith(file_suffix)]

    def describe(self) -> str:
        by_file: dict[str, int] = {}
        for item in self.expected:
            by_file[item.file] = by_file.get(item.file, 0) + 1
        detail = ", ".join(f"{name}×{count}" for name, count in sorted(by_file.items()))
        return f"{self.id}: {len(self.expected)} 条标注（{detail}）"


def extract_labels(root: Path, *, suffixes: tuple[str, ...] = (".py",)) -> list[ExpectedFinding]:
    """从目录树里解析所有 ``# 缺陷 XXX-n：`` 标注。"""
    findings: list[ExpectedFinding] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if any(part in {".git", "__pycache__", ".venv"} for part in path.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = path.relative_to(root).as_posix()
        for lineno, line in enumerate(content.splitlines(), start=1):
            match = LABEL_RE.search(line)
            if match:
                findings.append(
                    ExpectedFinding(
                        code=match.group("code").strip(),
                        file=relative,
                        line=lineno,
                        description=match.group("desc").strip(),
                    )
                )
    return findings


def load_case(case_id: str, repo: Path, goal: str) -> EvalCase:
    return EvalCase(id=case_id, repo=repo, goal=goal, expected=extract_labels(repo))


DEFAULT_GOAL = (
    "审查这个仓库中的缺陷，重点是会导致错误行为、资源泄漏或安全问题的实现。"
    "逐文件检查，确认后记录发现。"
)


def load_default_cases(examples_root: Path) -> list[EvalCase]:
    """把 examples 下的每个子目录当作一个评估样本。"""
    cases: list[EvalCase] = []
    if not examples_root.is_dir():
        return cases
    for child in sorted(examples_root.iterdir()):
        if child.is_dir() and not child.name.startswith((".", "_")):
            cases.append(load_case(child.name, child, DEFAULT_GOAL))
    return cases


# =============================================================================
# 匹配
# =============================================================================


@dataclass
class MatchOutcome:
    matched: list[tuple[ExpectedFinding, object]] = field(default_factory=list)
    missed: list[ExpectedFinding] = field(default_factory=list)
    spurious: list[object] = field(default_factory=list)
    # 与某条 matched 同属一个缺陷组、被同一条预测一并覆盖的其它实例。
    # 仅在分组口径（grouping="file_rule"）下非空；逐实例口径下恒为空。
    collateral: list[ExpectedFinding] = field(default_factory=list)
    # 落在一个已被消费的缺陷单元上的额外预测：属于**冗余**，不是误报。
    # 典型来源：规则集把同一个模式的 3 个 assert 报成 3 条。
    duplicates: list[object] = field(default_factory=list)

    @property
    def covered(self) -> int:
        return len(self.matched) + len(self.collateral)

    @property
    def precision(self) -> float:
        """精确率只由「命中 / 误报」决定，冗余单列 —— 重复报同一缺陷不等于报错。"""
        total = len(self.matched) + len(self.spurious)
        return len(self.matched) / total if total else 0.0

    @property
    def recall(self) -> float:
        total = self.covered + len(self.missed)
        return self.covered / total if total else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def describe(self) -> str:
        extra = ""
        if self.collateral:
            extra += f" / 并覆盖 {len(self.collateral)}"
        if self.duplicates:
            extra += f" / 冗余 {len(self.duplicates)}"
        return (
            f"命中 {len(self.matched)} / 漏报 {len(self.missed)} / 误报 {len(self.spurious)}{extra} "
            f"→ P={self.precision:.1%} R={self.recall:.1%} F1={self.f1:.1%}"
        )


# 匹配口径。两种都不是「唯一真理」，报告指标时必须写清楚用的是哪一种。
# - instance：每条标注独立计分。漏报任何一条都算漏。
#   代价：模型把「同文件同规则的多个实例」合并成一条报告时会被判漏报 ——
#   而这在代码审查里是合理甚至更好的写法（同一模式的 3 个 assert 本就该一起说）。
# - file_rule：同文件 + 同规则号的标注视为**一个缺陷单元**，一条预测覆盖任一实例
#   即算整个单元被覆盖。更贴近「审查者有没有发现这个问题」。
#   代价：会掩盖「只发现 3 处同类问题中的 1 处」这种部分覆盖。
GROUPINGS = ("instance", "file_rule")


def _build_groups(
    expected: list[ExpectedFinding], grouping: str
) -> list[tuple[tuple, list[ExpectedFinding]]]:
    if grouping == "file_rule":
        buckets: dict[tuple, list[ExpectedFinding]] = {}
        for label in expected:
            buckets.setdefault((label.file, label.rule_id), []).append(label)
        return list(buckets.items())
    # instance：每条标注自成一个单元
    return [((index,), [label]) for index, label in enumerate(expected)]


def match_findings(
    predicted: list,
    expected: list[ExpectedFinding],
    *,
    line_tolerance: int = 2,
    grouping: str = "instance",
) -> MatchOutcome:
    """把审计产出的发现与人工标注对齐。

    匹配规则：同一文件、行号在容差内，并且**每个缺陷单元只能被消费一次**。
    不要求规则号一致 —— 模型可能用完全不同的措辞描述同一个问题，
    用规则号匹配会大幅低估真实能力。

    三次踩过的坑都记在这里：
    - 早期版本漏了「消费一次」这一步：同一文件里多条同类型标注时，
      预测会反复匹配到第一条标注，后面的被误判成漏报（召回率被虚低 10 个百分点）。
    - 只按逐实例计分时，模型合并报告同类实例会被当作漏报，
      所以加了 grouping 口径，让「合并」与「漏掉」能被区分开。
    - 分组口径下，第二、三条落在同一单元上的预测曾被算成**误报**，
      把规则集的精确率从 100% 压到 58% —— 它们其实是冗余，不是报错，现已单列。
    """
    if grouping not in GROUPINGS:
        raise ValueError(f"未知的匹配口径：{grouping}（可选 {', '.join(GROUPINGS)}）")

    groups = _build_groups(expected, grouping)
    group_map = dict(groups)
    outcome = MatchOutcome()
    consumed: set[tuple] = set()

    for item in predicted:
        item_file = getattr(item, "file", "")
        item_line = getattr(item, "line", -999)

        best_key: tuple | None = None
        best_distance = line_tolerance + 1

        for key, members in groups:
            distances = [abs(m.line - item_line) for m in members if m.file == item_file]
            if not distances:
                continue
            distance = min(distances)
            if distance <= line_tolerance and distance < best_distance:
                best_key = key
                best_distance = distance
                if distance == 0:
                    break

        if best_key is None:
            outcome.spurious.append(item)
            continue

        if best_key in consumed:
            # 同一个缺陷单元被重复报告 —— 冗余，不扣精确率
            outcome.duplicates.append(item)
            continue

        consumed.add(best_key)
        members = sorted(group_map[best_key], key=lambda m: abs(m.line - item_line))
        outcome.matched.append((members[0], item))
        outcome.collateral.extend(members[1:])

    outcome.missed = [
        label for key, members in groups if key not in consumed for label in members
    ]
    return outcome


def case_passed(outcome: MatchOutcome, *, min_recall: float = 1.0, min_precision: float = 0.5) -> bool:
    """判定一个样本是否算「通过」。

    这是评估口径里最关键的选择，所以给两个阈值都设了默认值：
    - recall=1.0：漏报任何一个标注缺陷就算失败。审计场景下漏报代价最高。
    - precision=0.5：允许一半误报 —— 要求零误报会让指标对措辞差异过度敏感。

    改这两个阈值会显著改变 Pass@k，报告里必须一并说明。
    """
    return outcome.recall >= min_recall and outcome.precision >= min_precision
