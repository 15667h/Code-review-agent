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
    """一条规则。

    ``languages`` 声明这条规则作用于哪些语言（用 ``languages.py`` 的 id）。
    运行时按文件后缀反查语言，再判断规则是否适用 —— 这样加语言只改
    ``languages.py``，不用动每条规则。

    ``suffixes`` 保留是给**显式覆写**用的（比如某条规则只想管 ``.pyi``）。
    留空表示「按 languages 推导」。两者都留是为了不破坏既有规则的写法。
    """

    id: str
    severity: Severity
    title: str
    detail: str
    suggestion: str
    detect: Callable[[str, str], Iterator[Hit]]
    #: 适用的语言 id。空 = 全部已注册语言（仅对词法安全的规则适用）。
    languages: tuple[str, ...] = ("python",)
    #: 显式后缀覆写。空 = 由 languages 推导。
    suffixes: tuple[str, ...] = ()


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
_RE_EVAL = re.compile(
    r"(?:\beval\s*\(|\bexec\s*\(|\bexecSync\s*\(|\bvm\.runIn"
    r"|\bFunction\s*\(|setTimeout\s*\(\s*['\"]|new\s+Function\s*\(|\bchild_process\b)"
)
#: 多语言赋值形态的密钥：``const api_key = "..."`` / ``var password = "..."`` /
#: ``$token = "..."`` / ``private String secret = "..."``。
#:
#: **判据拆成两步，不要写成一条大正则。** 上一版试图用一条正则同时完成
#: 「跳到标识符」+「检查标识符含敏感词」+「匹配字面量」，结果
#: ``[A-Za-z_$][\w$]*`` 贪婪吃掉了整个 ``api_key``，后面的敏感词分支永远匹配不上 ——
#: JS/TS/Go 的密钥全部漏报（实测发现）。改成：
#: ① 用一条简单的正则找出「赋值语句」并捕获左侧标识符与右侧字面量；
#: ② 在 Python 里判断标识符是否含敏感词。这样每条规则都简单可验证。
_RE_ASSIGN_LITERAL = re.compile(
    r"""^\s*(?:(?:const|let|var|final|val|private|public|protected|static|readonly|def)\s+)*"""
    r"""(?:[\w.<>\[\]]+\s+)*"""                      # 可能的类型声明（Java/C#：String key =）
    r"""([A-Za-z_$][\w$.]*)\s*[:=]\s*"""
    r"""(['"`])((?:(?!\2).){6,})\2""",
)

#: 标识符里出现这些词就算「疑似凭据名」。
_SECRET_NAME_WORDS = (
    "password", "passwd", "pwd", "secret", "token",
    "apikey", "api_key", "accesskey", "access_key",
    "privatekey", "private_key", "credential",
)

#: 危险反序列化：各语言都有等价物，输入可控时等同 RCE
_RE_DESERIALIZE = re.compile(
    r"(?:pickle\.loads?\s*\(|yaml\.load\s*\([^)]*(?!Safe)|marshal\.loads?\s*\("
    r"|\bunserialize\s*\(|\bObjectInputStream\b|JsonSerializer\.(?i:deserialize)\s*\()"
)
#: 命令注入：把变量拼进 shell 调用
_RE_CMD_INJECT = re.compile(
    r"(?:os\.system\s*\(|os\.popen\s*\(|subprocess\.[a-z_]+\s*\([^)]*shell\s*=\s*True"
    r"|child_process\.exec\s*\(|\bexec\s*\(\s*['\"`][^\"`']*\$)"
)
#: 弱哈希
_RE_WEAK_HASH = re.compile(
    r"\b(?:md5|sha1|MD5|SHA1)\s*\(|hashlib\.(?:md5|sha1)\s*\(|createHash\s*\(\s*['\"](?:md5|sha1)['\"]"
)
#: 调试残留：生产代码里的 console.log / print / fmt.Println / System.out.println
_RE_DEBUG_PRINT = re.compile(
    r"^\s*(?:console\.(?:log|debug)\s*\(|print\s*\(|System\.out\.print|fmt\.Print"
    r"|println!\s*\(|debugger\s*;?\s*$)"
)
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


def _lang_id(path: str) -> str:
    """文件路径 → 语言 id。未知语言退回 ``python``（沿用最通用的 ``#`` 剥离）。"""
    from .languages import for_path

    lang = for_path(path)
    return lang.id if lang else "python"


def _iter_python_lines(source: str) -> Iterator[tuple[int, str]]:
    """逐行产出 ``(行号, 已剥离注释的代码行)``（Python 专用，保持既有行为）。"""
    for lineno, line in enumerate(source.splitlines(), start=1):
        yield lineno, _code_part(line)


def _iter_lines(source: str, language: str = "python") -> Iterator[tuple[int, str]]:
    """跨语言的逐行迭代：按目标语言剥离注释。

    ## 为什么必须按语言剥离，而不是共用一套

    ``//`` 在 Python 里是地板除运算符，``#`` 在 Ruby/PHP 里是注释但在 C# 里是预处理指令。
    共用一套剥离规则会同时造成两类错误：
    ① 把 Python 的 ``a // b`` 当成注释砍掉后半行 → 漏报；
    ② 把 JS 的 ``// comment`` 当成代码 → 注释里的假匹配被报成缺陷。

    所以 ``language`` 决定用哪套前缀。未知语言退回 ``python`` 的规则
    （``#`` 是最常见的注释前缀，误剥比漏剥更安全 —— 漏剥会把注释当代码报出来）。
    """
    from .languages import LANGUAGES

    # 参数既接受**语言 id**（typescript）也接受**后缀**（.ts）：
    # 这两类调用方都有（规则用 id、read_symbols 拿到的是后缀），
    # 之前只认后缀，导致传 id 时静默退回 "#" —— 注释没被剥离，
    # 实测后果是「// function commented() {}」被当成真实函数定义报出来。
    lang = LANGUAGES.get(language)
    if lang is None:
        from .languages import for_path

        key = language if language.startswith(".") else f"x.{language}"
        lang = for_path(key)
    prefixes = lang.line_comment if lang else ("#",)
    for lineno, line in enumerate(source.splitlines(), start=1):
        yield lineno, _code_part(line, prefixes)


def _is_test_file(path: str) -> bool:
    """测试文件豁免：夹具里的假凭据、断言、open 都不该按生产标准报告。

    委托给 ``languages.is_test_file`` —— 那份实现覆盖所有已注册语言的测试文件
    命名约定（``_test.go`` / ``*.spec.ts`` / ``src/test/java/`` …）。
    自己写一份只会漏掉别的语言。
    """
    from .languages import is_test_file

    return is_test_file(path)


def _code_part(line: str, comment_prefixes: tuple[str, ...] = ("#",)) -> str:
    """剥离行尾注释，只留代码部分。

    **规则里凡是「判断这行有没有出现某个关键字」的地方都必须用它。**
    直接对整行做关键词搜索会被注释里的同名词骗过 —— 本项目在 finally、
    with、SQL 关键字上各栽过一次，代价是整类漏报或误报。

    ``comment_prefixes`` 按语言给（Python ``#``、JS/Go ``//``、Lua ``--`` …）。
    单字符前缀走原来那套逐字符状态机（快且已验证）；多字符前缀先做子串定位，
    但**必须落在字符串之外**才算注释 —— 否则 ``const u = "http://x" // c``
    会在 ``//`` 处被误判。
    """
    # 单字符前缀（``#``）：保持原有的逐字符状态机，行为一字不改
    if comment_prefixes == ("#",):
        return _code_part_single(line, "#")

    # 多字符前缀：先扫描字符串边界，再在边界外找注释前缀
    out: list[str] = []
    quote: str | None = None
    escaped = False
    i = 0
    n = len(line)
    # 长的前缀优先匹配（``--`` 要先于 ``-``；这里没有单 ``-``，但留出扩展余地）
    ordered = sorted(comment_prefixes, key=len, reverse=True)
    while i < n:
        ch = line[i]
        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            i += 1
            continue
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        matched = False
        for prefix in ordered:
            if line.startswith(prefix, i):
                matched = True
                break
        if matched:
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _code_part_single(line: str, marker: str) -> str:
    """单字符注释前缀的原始实现（保持既有行为不变）。"""
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


def _looks_like_secret_assign(line: str) -> bool:
    """判断这行是不是「把字面量赋给一个疑似凭据名」。

    两步判据（拆开写比一条大正则可靠，见 ``_RE_ASSIGN_LITERAL`` 的注释）：
    ① 正则确认这是「标识符 = '字面量'」的形态；
    ② 标识符名里含敏感词（password / token / api_key …）。

    排除两类常见误报：
    - 右侧是**环境变量读取**而不是字面量时不会命中（正则要求引号）；
    - 名字含 ``_field`` / ``_label`` 这种纯 UI 文案的场景仍可能命中 ——
      所以定级是 blocker 但只在证据里显示脱敏后的原文，由人复核。
    """
    m = _RE_ASSIGN_LITERAL.match(line)
    if not m:
        return False
    name = m.group(1).lower()
    # 取最后一段：this.apiKey / self.token / config.secret
    leaf = name.rsplit(".", 1)[-1]
    return any(word in leaf for word in _SECRET_NAME_WORDS)


def _detect_hardcoded_secret(path: str, source: str) -> Iterator[Hit]:
    """硬编码密钥。**词法安全，全语言可用** —— 各语言的赋值语法这里都覆盖得到。

    取证时把字面量脱敏成 ``***``：报告里绝不能再出现一次真密钥。
    """
    if _is_test_file(path):
        return
    lang = _lang_id(path)
    for lineno, line in _iter_lines(source, lang):
        if not (_RE_SECRET.match(line) or _looks_like_secret_assign(line)):
            continue
        redacted = re.sub(r"""(['"`])[^'"`]{6,}\1""", r"\1***\1", line.rstrip())
        yield lineno, redacted


def _detect_eval(path: str, source: str) -> Iterator[Hit]:
    """动态执行字符串代码（eval / exec 及等价物）。**词法安全，全语言可用**。"""
    lang = _lang_id(path)
    for lineno, line in _iter_lines(source, lang):
        if _RE_EVAL.search(line):
            yield lineno, line.strip()


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




def _detect_deserialize(path: str, source: str) -> Iterator[Hit]:
    """危险反序列化。**全语言可用**。"""
    lang = _lang_id(path)
    for lineno, line in _iter_lines(source, lang):
        if _RE_DESERIALIZE.search(line):
            yield lineno, line.strip()


def _detect_cmd_inject(path: str, source: str) -> Iterator[Hit]:
    """把变量拼进 shell 调用。**全语言可用**。"""
    lang = _lang_id(path)
    for lineno, line in _iter_lines(source, lang):
        if _RE_CMD_INJECT.search(line):
            yield lineno, line.strip()


def _detect_weak_hash(path: str, source: str) -> Iterator[Hit]:
    """弱哈希用于安全场景。**全语言可用**。

    注意：只报「用了 md5/sha1」，不判断是否用于安全目的 ——
    静态看不出来。所以定级 note 而不是 blocker，措辞也写「若用于安全场景」。
    """
    lang = _lang_id(path)
    for lineno, line in _iter_lines(source, lang):
        if _RE_WEAK_HASH.search(line):
            yield lineno, line.strip()


def _detect_debug_print(path: str, source: str) -> Iterator[Hit]:
    """生产代码里的调试输出。**全语言可用**。

    测试文件与脚本（``scripts/``、``tools/``、``examples/``）豁免 ——
    那些地方的 print 是正常输出，不是遗留调试。
    """
    if _is_test_file(path):
        return
    normalized = path.replace("\\", "/").lower()
    if any(seg in normalized for seg in ("/scripts/", "/tools/", "/examples/", "/bin/", "/cli")):
        return
    lang = _lang_id(path)
    for lineno, line in _iter_lines(source, lang):
        if _RE_DEBUG_PRINT.search(line):
            yield lineno, line.strip()


#: 「适用于全部已注册语言」。给词法安全的规则用。
#:
#: **必须从 languages.py 动态取**，不能在这里抄一份硬编码列表 ——
#: 抄一份的话，往 languages.py 加语言时这里不会跟着变，
#: 结果新语言「注册了但规则不跑」，而且不会有任何报错。
from .languages import LANGUAGES as _LANGUAGES  # noqa: E402

ALL_LANGS: tuple[str, ...] = tuple(_LANGUAGES)


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
        # 词法安全：各语言的赋值形态都在正则里，全语言可用
        languages=ALL_LANGS,
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
        # eval/exec 的等价物在各语言都存在（见 _RE_EVAL）
        languages=ALL_LANGS,
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
        id="SEC004",
        severity="blocker",
        title="危险反序列化",
        detail="对不可信输入做反序列化，攻击者可构造对象或指令，等同远程代码执行。",
        suggestion="换用安全格式（JSON / 明确声明的 schema）；Python 的 pickle 不要处理外部数据。",
        detect=_detect_deserialize,
        languages=ALL_LANGS,
    ),
    Rule(
        id="SEC005",
        severity="blocker",
        title="命令拼接后交给 shell 执行",
        detail="把变量拼进 shell 命令字符串，输入可控时可注入任意命令。",
        suggestion="改用参数数组直接调用（不经 shell），例如 subprocess.run([...], shell=False)。",
        detect=_detect_cmd_inject,
        languages=ALL_LANGS,
    ),
    Rule(
        id="SEC006",
        severity="note",
        title="使用弱哈希算法",
        detail="md5 / sha1 已不适合安全用途（碰撞可构造）。若用于口令或签名，等同无保护。",
        suggestion="安全场景改用 sha256 / bcrypt / argon2；校验文件完整性也建议换掉 md5。",
        detect=_detect_weak_hash,
        languages=ALL_LANGS,
    ),
    Rule(
        id="QUAL001",
        severity="note",
        title="生产代码残留调试输出",
        detail="调试用的打印语句会污染日志、泄露内部变量，且在高频路径上拖慢性能。",
        suggestion="改用正式日志设施（分级 + 可控开关），或删除。",
        detect=_detect_debug_print,
        languages=ALL_LANGS,
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
def rule_applies(rule: Rule, path: str) -> bool:
    """这条规则是否适用于这个文件。

    解析顺序：
    ① 规则的 ``suffixes`` 显式给了 → 只看后缀（精确覆写）；
    ② 否则按 ``languages`` 反查语言的 suffixes；
    ③ 文件语言未知 → 不适用（未注册语言我们不知道该怎么安全地剥离注释，
       硬跑规则会产生大量假阳/假阴）。
    """
    from .languages import for_path

    lang = for_path(path)
    if not lang:
        return False
    if rule.suffixes:
        return Path(path).suffix.lower() in {x.lower() for x in rule.suffixes}
    if not rule.languages:
        return True
    return lang.id in rule.languages


def scan_source(path: str, source: str) -> list[RuleHit]:
    """对单个文件跑全部适用规则。

    按文件语言分派：只有 ``rule_applies`` 通过的规则才跑。
    """
    hits: list[RuleHit] = []
    for rule in RULES:
        if not rule_applies(rule, path):
            continue
        for line, evidence in rule.detect(path, source):
            hits.append(RuleHit(rule=rule, path=path, line=line, evidence=evidence.strip()[:200]))
    return hits


def scan_tree(
    root: Path,
    *,
    suffixes: tuple[str, ...] | None = None,
    limit: int = 400,
) -> ScanReport:
    """扫描一棵目录树。

    ``suffixes`` 默认取**所有已注册语言**的后缀，而不是只扫 ``.py``。
    之前默认 ``(".py",)`` 意味着对非 Python 仓库完全失明 ——
    一个 JS 仓库跑规则扫描会得到「0 个命中」，看起来像「没问题」，
    实际是「根本没看」。这种静默失明比误报更危险。
    """
    from .languages import all_suffixes
    from .sandbox import Workspace

    want = {x.lower() for x in (suffixes if suffixes is not None else all_suffixes())}
    workspace = Workspace(root=root)
    report = ScanReport()
    files, _ = workspace.iter_files(".", "*", recursive=True, limit=limit)

    for file in files:
        if file.suffix.lower() not in want:
            continue


def scan_tree(
    root: Path,
    *,
    suffixes: tuple[str, ...] | None = None,
    limit: int = 400,
) -> ScanReport:
    """扫描一棵目录树。

    ``suffixes`` 默认取**所有已注册语言**的后缀，而不是只扫 ``.py``。
    上一版默认 ``(".py",)``，意味着对非 Python 仓库完全失明 ——
    一个混合语言仓库跑规则扫描只报了 Python 的命中，
    TS/Go 里的硬编码密钥一条都没报，而输出看起来「扫完了、没问题」。
    这种静默失明比误报危险得多。
    """
    from .languages import all_suffixes
    from .sandbox import Workspace

    want = {x.lower() for x in (suffixes if suffixes is not None else all_suffixes())}
    workspace = Workspace(root=root)
    report = ScanReport()
    files, _ = workspace.iter_files(".", "*", recursive=True, limit=limit)

    for file in files:
        if file.suffix.lower() not in want:
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
