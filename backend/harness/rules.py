"""确定性审计规则。

书里的原则：**凡是能写成程序化断言的检查，就应当继续用断言**，不要交给 LLM 判断。
这些规则有三个用途：

1. 作为离线模式的驱动逻辑（无需 API Key 也能跑通全链路）；
2. 作为 LLM 审计结果的交叉验证器（规则命中但模型没报告 → 疑似漏报）；
3. 作为评估数据集里的「标准答案」来源之一。

规则刻意保持**高精度**：宁可少报，也不要制造噪音 ——
误报会稀释报告价值，还会把评估指标带偏。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Severity = Literal["blocker", "warn", "note"]
Hit = tuple[int, str]  # (行号, 证据行)


@dataclass(frozen=True)
class Rule:
    id: str
    severity: Severity
    title: str
    detail: str
    suggestion: str
    detect: Callable[[str, str], Iterator[Hit]]
    suffixes: tuple[str, ...] = (".py",)


# =============================================================================
# 规则实现
# =============================================================================

_RE_BARE_EXCEPT = re.compile(r"^\s*except\s*:")
_RE_EXCEPT_PASS = re.compile(r"^\s*except[^\n]*:\s*(?:#.*)?$")
_RE_PASS_NEXT = re.compile(r"^\s*pass\s*(?:#.*)?$")
_RE_MUTABLE_DEFAULT = re.compile(r"^\s*def\s+\w+\s*\([^)]*=\s*(\[\]|\{\}|set\(\)|\{\s*\})")
_RE_SECRET = re.compile(
    r"""^\s*(?:[A-Za-z_]*?(?:password|passwd|secret|token|api_?key|access_?key)[A-Za-z_]*)\s*=\s*['"][^'"]{6,}['"]""",
    re.IGNORECASE,
)
_RE_EVAL = re.compile(r"\b(eval|exec)\s*\(")
_RE_OPEN_NO_WITH = re.compile(r"^\s*(?!.*\bwith\b).*\bopen\s*\([^)]*\)\s*(?!.*\bwith\b)")
# SQL 拼接的两种形态：f-string 内联插值，以及字符串用 + / % / .format 拼接。
# 关键：不能只看 execute(...) 的参数 —— 真实代码常常先把 SQL 赋给变量再执行，
# 只看调用点会整类漏报。
_RE_SQL_KEYWORD = re.compile(
    r"\b(?:SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|JOIN)\b", re.IGNORECASE
)
# 判据之一是「字符串字面量之后紧跟拼接运算符」。
# 最初写成「在字符串内部匹配 SQL 关键字、再要求后面有运算符」，实测会因贪婪
# 量词的回溯而漏报（`"SELECT ... = '" + name + "'"` 这类行匹配不上）。
# 把判据拆开、每一条都简单可验证，比堆一个复杂的正则更可靠。
_RE_SQL_FSTRING = re.compile(
    r"""\bf['"][^'"]*\b(?:SELECT|INSERT|UPDATE|DELETE|FROM|WHERE)\b""", re.IGNORECASE
)
_RE_STRING_THEN_CONCAT = re.compile(r"""['"]\s*(?:\+|%|\.format\s*\()""")
_RE_ASSERT = re.compile(r"^\s*assert\s+\S")
_RE_COMPARE_TRUE = re.compile(r"(==|!=)\s*(True|False)\b")
_RE_ACQUIRE = re.compile(r"\b(acquire|checkout|get_connection|borrow)\s*\(")


def _iter_python_lines(source: str) -> Iterator[tuple[int, str]]:
    """逐行产出 ``(行号, 已剥离注释的代码行)``。

    所有规则统一基于剥离后的代码行做判断**和取证**：
    注释里出现的关键词不该影响判定，更不该出现在证据里 ——
    否则报告会把「说明缺陷的注释」当成缺陷代码本身展示出来。
    """
    for lineno, line in enumerate(source.splitlines(), start=1):
        yield lineno, _code_part(line)


def _is_test_file(path: str) -> bool:
    """测试文件豁免：夹具里的假凭据、断言、open 都不该按生产标准报告。"""
    normalized = path.replace("\\", "/")
    return "/tests/" in normalized or Path(normalized).name.startswith("test_")


def _code_part(line: str) -> str:
    """剥离行尾注释，只留代码部分。

    **规则里凡是「判断这行有没有出现某个关键字」的地方都必须用它。**
    直接对整行做关键词搜索会被注释里的同名词骗过 —— 本项目在 finally、
    with、SQL 关键字上各栽过一次，代价是整类漏报或误报。
    """
    out: list[str] = []
    quote: str | None = None
    escaped = False

    for ch in line:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)

    return "".join(out)


def _detect_bare_except(path: str, source: str) -> Iterator[Hit]:
    for lineno, line in _iter_python_lines(source):
        if _RE_BARE_EXCEPT.match(line):
            yield lineno, line.rstrip()


def _detect_except_pass(path: str, source: str) -> Iterator[Hit]:
    """`except ...:` 之后（允许中间有注释与空行）紧跟 `pass`。"""
    lines = [_code_part(raw) for raw in source.splitlines()]
    for index, line in enumerate(lines):
        if not _RE_EXCEPT_PASS.match(line):
            continue
        cursor = index + 1
        while cursor < len(lines):
            stripped = lines[cursor].strip()
            if stripped and not stripped.startswith("#"):
                break
            cursor += 1
        if cursor < len(lines) and _RE_PASS_NEXT.match(lines[cursor]):
            yield cursor + 1, f"{line.rstrip()} → {lines[cursor].strip()}"


def _detect_mutable_default(path: str, source: str) -> Iterator[Hit]:
    for lineno, line in _iter_python_lines(source):
        if _RE_MUTABLE_DEFAULT.match(line):
            yield lineno, line.rstrip()


def _detect_hardcoded_secret(path: str, source: str) -> Iterator[Hit]:
    if _is_test_file(path):
        return
    for lineno, line in _iter_python_lines(source):
        if _RE_SECRET.match(line):
            redacted = re.sub(r"""(['"])[^'"]{6,}\1""", r"\1***\1", line.rstrip())
            yield lineno, redacted


def _detect_eval(path: str, source: str) -> Iterator[Hit]:
    for lineno, line in _iter_python_lines(source):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if _RE_EVAL.search(line):
            yield lineno, stripped


def _detect_open_without_with(path: str, source: str) -> Iterator[Hit]:
    if _is_test_file(path):
        return
    for lineno, line in _iter_python_lines(source):
        code = _code_part(line)
        if not code.strip():
            continue
        # 只看代码部分：注释里写「用 with 包起来」不该让这一行免检
        if re.search(r"\bwith\b", code):
            continue
        if re.search(r"=\s*open\s*\(", code):
            yield lineno, code.strip()


def _detect_sql_concat(path: str, source: str) -> Iterator[Hit]:
    for lineno, line in _iter_python_lines(source):
        code = _code_part(line).strip()
        if not code or not _RE_SQL_KEYWORD.search(code):
            continue
        if _RE_SQL_FSTRING.search(code) or _RE_STRING_THEN_CONCAT.search(code):
            yield lineno, code


def _detect_assert_in_production(path: str, source: str) -> Iterator[Hit]:
    normalized = path.replace("\\", "/")
    if "/tests/" in normalized or Path(normalized).name.startswith("test_"):
        return
    for lineno, line in _iter_python_lines(source):
        if _RE_ASSERT.match(line):
            yield lineno, line.strip()


def _detect_compare_to_boolean(path: str, source: str) -> Iterator[Hit]:
    for lineno, line in _iter_python_lines(source):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if _RE_COMPARE_TRUE.search(line):
            yield lineno, stripped


def _detect_acquire_without_finally(path: str, source: str) -> Iterator[Hit]:
    """资源获取之后在附近没有 finally 也没有 with。

    这条规则刻意保守：只做「提示」，因为静态看缩进不足以完全确定控制流。
    它的价值在于把「资源生命周期」这一高风险区域标出来给人复核，
    而真正的判定应当来自 read_file 顺着异常路径读一遍。
    """
    lines = source.splitlines()
    for index, line in enumerate(lines):
        code = _code_part(line)
        if not code.strip() or "def " in code:
            continue
        if not _RE_ACQUIRE.search(code):
            continue
        if re.search(r"\bwith\b", code):
            continue
        # 只看窗口里「以 finally 开头的语句行」，并且同样要先剥掉注释 ——
        # 直接搜 "finally" 这个词会被注释里的同名词骗到。
        if any(
            _code_part(candidate).strip().startswith("finally")
            for candidate in lines[index : index + 18]
        ):
            continue
        yield index + 1, code.strip()


# =============================================================================
# 规则表
# =============================================================================

RULES: tuple[Rule, ...] = (
    Rule(
        id="SEC001",
        severity="blocker",
        title="硬编码密钥",
        detail="源码中直接写入了密码、令牌或 API Key。一旦仓库公开或进入交付物，凭据即泄露。",
        suggestion="改为从环境变量或密钥管理服务读取，并把该凭据立即轮换。",
        detect=_detect_hardcoded_secret,
    ),
    Rule(
        id="SEC002",
        severity="blocker",
        title="SQL 语句字符串拼接",
        detail="把变量直接拼进 SQL 文本，输入可控时会形成注入点。",
        suggestion="改用参数化查询（占位符 + 参数元组），不要用 f-string 或 % 拼接 SQL。",
        detect=_detect_sql_concat,
    ),
    Rule(
        id="ERR001",
        severity="warn",
        title="裸 except",
        detail="`except:` 会同时捕获 KeyboardInterrupt 与 SystemExit，使程序无法被正常中断，也掩盖真实错误类型。",
        suggestion="捕获明确的异常类型；若确实需要兜底，用 `except Exception:` 并记录日志。",
        detect=_detect_bare_except,
    ),
    Rule(
        id="ERR002",
        severity="warn",
        title="异常被静默吞掉",
        detail="`except ...: pass` 把错误完全丢弃，故障会以更晚、更难定位的形式出现。",
        suggestion="至少记录一条日志，或把异常向上抛出。",
        detect=_detect_except_pass,
    ),
    Rule(
        id="RES001",
        severity="warn",
        title="文件句柄未使用 with 管理",
        detail="直接 `open()` 赋值给变量时，异常路径上不会关闭文件描述符，长期运行会耗尽句柄。",
        suggestion="改用 `with open(...) as fh:`；若必须手动管理，请放进 try/finally。",
        detect=_detect_open_without_with,
    ),
    Rule(
        id="PY001",
        severity="warn",
        title="可变对象作为默认参数",
        detail="默认参数在函数定义时求值一次并在调用间共享，多次调用会累积同一个列表/字典。",
        suggestion="默认值用 None，在函数体内 `if x is None: x = []`。",
        detect=_detect_mutable_default,
    ),
    Rule(
        id="SEC003",
        severity="warn",
        title="使用 eval / exec",
        detail="动态执行字符串代码，输入可控时等同于任意代码执行。",
        suggestion="改用显式解析（ast.literal_eval、json.loads）或白名单分派表。",
        detect=_detect_eval,
    ),
    Rule(
        id="PY002",
        severity="note",
        title="生产代码中的 assert",
        detail="Python 以 -O 运行时 assert 会被整体移除，用它做参数校验会失效。",
        suggestion="改成显式的 if + raise，或用类型/校验库。",
        detect=_detect_assert_in_production,
    ),
    Rule(
        id="PY003",
        severity="note",
        title="与布尔常量直接比较",
        detail="`x == True` 是值比较而非真值判断，传入 1、'yes' 等真值时行为与直觉不符。",
        suggestion="直接写 `if x:` / `if not x:`；若必须区分 True 与其他真值，用 `is True`。",
        detect=_detect_compare_to_boolean,
    ),
    Rule(
        id="RES002",
        severity="note",
        title="资源获取后未见 finally 归还",
        detail="acquire/checkout 之后如果异常路径没有归还，资源占用会随异常频率单调增长。"
        "静态上看不出完整控制流，这里只做提示。",
        suggestion="把获取与归还收进 try/finally，或改用上下文管理器（with）。",
        detect=_detect_acquire_without_finally,
    ),
)

RULES_BY_ID = {rule.id: rule for rule in RULES}


# =============================================================================
# 执行
# =============================================================================


@dataclass
class RuleHit:
    rule: Rule
    path: str
    line: int
    evidence: str

    @property
    def fingerprint(self) -> str:
        return f"{self.path}:{self.line}:{self.rule.id}"


@dataclass
class ScanReport:
    hits: list[RuleHit] = field(default_factory=list)
    scanned_files: int = 0
    skipped_files: int = 0
def scan_source(path: str, source: str) -> list[RuleHit]:
    """对单个文件跑全部适用规则。"""
    suffix = Path(path).suffix
    hits: list[RuleHit] = []
    for rule in RULES:
        if suffix not in rule.suffixes:
            continue
        for line, evidence in rule.detect(path, source):
            hits.append(RuleHit(rule=rule, path=path, line=line, evidence=evidence.strip()[:200]))
    return hits


def scan_tree(root: Path, *, suffixes: tuple[str, ...] = (".py",), limit: int = 400) -> ScanReport:
    """扫描一棵目录树。"""
    from .sandbox import Workspace

    workspace = Workspace(root=root)
    report = ScanReport()
    files, _ = workspace.iter_files(".", "*", recursive=True, limit=limit)

    for file in files:
        if file.suffix not in suffixes:
            continue
        try:
            source = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            report.skipped_files += 1
            continue
        report.scanned_files += 1
        report.hits.extend(scan_source(workspace.relative(file), source))

    report.hits.sort(key=lambda hit: (hit.path, hit.line, hit.rule.id))
    return report


def to_findings(report: ScanReport) -> list:
    """把规则命中转成审计发现（供交叉验证或离线报告使用）。"""
    from .types import Finding

    return [
        Finding(
            severity=hit.rule.severity,
            file=hit.path,
            line=hit.line,
            title=f"{hit.rule.title}（{hit.rule.id}）",
            detail=hit.rule.detail,
            evidence=hit.evidence,
            suggestion=hit.rule.suggestion,
        )
        for hit in report.hits
    ]


def cross_check(findings: list, report: ScanReport) -> list[str]:
    """交叉验证：**规则命中但模型没报**的位置，疑似漏报。

    只报告规则命中的位置（高精度），不会因为模型报了规则没覆盖的问题而报警 ——
    规则覆盖面本来就比模型窄，反向告警全是噪音。
    """
    reported = {(item.file, item.line) for item in findings}
    missed: list[str] = []
    for hit in report.hits:
        if hit.rule.severity == "note":
            continue
        if (hit.path, hit.line) not in reported:
            missed.append(f"{hit.path}:{hit.line} {hit.rule.id} {hit.rule.title}")
    return missed
