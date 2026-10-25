"""编程语言注册表：一处声明「支持哪些语言、支持到什么程度」。

## 为什么必须先有这个模块

在这之前，语言假设散落在四处、而且互相不一致：

| 位置 | 原来的硬编码 |
|---|---|
| ``rules.py`` | ``suffixes = (".py",)``，``scan_tree`` 也只扫 ``.py`` |
| ``codegraph.py`` | ``PY_SUFFIX = ".py"``，只用 ``ast`` 解析 |
| ``tools_common.read_symbols`` | 非 ``.py`` 直接拒绝 |
| 测试文件识别 | 只认 ``test_*.py`` / ``*_test.py`` |

四处各写一份，加一种语言要改四个地方，改漏了不会报错 —— 只会静默失明。
所以把语言定义收成**一张表**，各模块都从这张表取。

## 能力分级（这是本模块最重要的设计）

**不假装支持所有语言。** 我们没有 tree-sitter（项目定位是零依赖，只有 httpx），
所以不同语言能做的事完全不同，必须如实分级：

| 级别 | 含义 | 谁能做到 | 能做什么 |
|---|---|---|---|
| ``deep`` | 有完整 AST | Python（标准库 ``ast``） | 结构图、符号抽取、跨文件调用、测试覆盖 |
| ``shallow`` | 只有词法/正则 | JS/TS/Java/Go/Rust/Ruby/PHP/C# … | 规则扫描（危险调用、硬编码密钥）、括号配对、行号定位 |
| ``text`` | 只当文本 | 其余一切 | grep / read_file |

**为什么这个分级必须暴露给模型**：如果 ``who_calls`` 在 JS 仓库返回空列表，
模型会理解成「没人调用」—— 那是错误的事实，会导出「可以随便改」的结论。
分级让工具能明确回答「这个语言我只能做到这一步」，而不是给出虚假的空结果。

## 新增语言只需改这里

往 ``LANGUAGES`` 加一项 + （可选）在 ``rules.py`` 加该语言的规则。
所有 agent 自动获得这种语言的支持，不需要改任何 agent 代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field


# =============================================================================
# 能力级别
# =============================================================================

LEVEL_DEEP = "deep"
LEVEL_SHALLOW = "shallow"
LEVEL_TEXT = "text"

#: 级别排序，用于「至少要到某一级」的判断
_LEVEL_RANK = {LEVEL_TEXT: 0, LEVEL_SHALLOW: 1, LEVEL_DEEP: 2}


@dataclass(frozen=True, slots=True)
class Language:
    """一种编程语言的能力声明。"""

    id: str                          # 规范 id：python / javascript / go ...
    display: str                     # 给人看的名字
    suffixes: tuple[str, ...]        # 扩展名，含点
    level: str                       # deep / shallow / text
    #: 注释前缀（用于判断「这行是不是注释」，浅层分析要用）
    line_comment: tuple[str, ...] = ("//",)
    #: 块注释的起止（可选）
    block_comment: tuple[tuple[str, str], ...] = ()
    #: 测试文件的识别规则（正则字符串）。空表示「按通用约定猜」。
    test_patterns: tuple[str, ...] = ()
    #: 该语言的结构关键字。浅层分析靠它定位函数/类定义。
    func_keywords: tuple[str, ...] = ()
    class_keywords: tuple[str, ...] = ()
    #: 字符串定界符（用于在扫描时跳过字符串里的假匹配）
    string_delims: tuple[str, ...] = ('"', "'")
    #: 说明该语言**做不到**什么。会出现在工具返回里，防止模型误读空结果。
    limits: str = ""

    @property
    def level_rank(self) -> int:
        return _LEVEL_RANK.get(self.level, 0)


# =============================================================================
# 语言表
# =============================================================================
#
# 顺序即「同分时优先」的顺序：Python 在前（我们能力最强）。
# 每个浅层语言都必须写 ``limits`` —— 写明做不到什么，比声称做到什么更重要。

LANGUAGES: dict[str, Language] = {}


def _add(lang: Language) -> Language:
    LANGUAGES[lang.id] = lang
    return lang


# ---------------------------------------------------------------- deep

_add(Language(
    id="python",
    display="Python",
    suffixes=(".py", ".pyi"),
    level=LEVEL_DEEP,
    line_comment=("#",),
    block_comment=(('"""', '"""'), ("'''", "'''")),
    test_patterns=(
        r"^test_.*\.py$", r"^.*_test\.py$", r"^conftest\.py$",
        r"(^|/)tests?/", r"(^|/)testing/",
    ),
    func_keywords=("def", "async def"),
    class_keywords=("class",),
    string_delims=('"""', "'''", '"', "'"),
    limits="",
))


# ---------------------------------------------------------------- shallow
# 这一组的能力边：**规则扫描 + 行号定位 + 括号配对**。
# 做不了：符号图、跨文件调用、测试覆盖。

_add(Language(
    id="javascript",
    display="JavaScript",
    suffixes=(".js", ".jsx", ".mjs", ".cjs"),
    level=LEVEL_SHALLOW,
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    test_patterns=(
        r"\.(test|spec)\.[cm]?jsx?$", r"(^|/)__tests__/", r"(^|/)tests?/",
    ),
    func_keywords=("function", "async function", "=>", "async ", "get ", "set "),
    class_keywords=("class",),
    string_delims=("`", '"', "'"),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="typescript",
    display="TypeScript",
    suffixes=(".ts", ".tsx", ".mts", ".cts"),
    level=LEVEL_SHALLOW,
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    test_patterns=(
        r"\.(test|spec)\.[cm]?tsx?$", r"(^|/)__tests__/", r"(^|/)tests?/",
    ),
    func_keywords=("function", "async function", "=>", "async ", "get ", "set "),
    class_keywords=("class", "interface", "type ", "enum"),
    string_delims=("`", '"', "'"),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="java",
    display="Java",
    suffixes=(".java",),
    level=LEVEL_SHALLOW,
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    test_patterns=(r"(^|/)src/test/", r"Test\.java$", r"Tests\.java$"),
    # 修饰词（public/private/static）既用于方法也用于字段，拿它当函数关键字
    # 会把 `private String name = "x";` 报成函数（实测踩过）。
    # 所以只留明确的返回类型关键字，其余交给 `_looks_like_method` 的
    # 「以 { 结尾」判据 —— 字段声明以 ; 结尾，天然被排除。
    func_keywords=("void ",),
    class_keywords=("class ", "interface ", "enum ", "record "),
    string_delims=('"', "'"),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="go",
    display="Go",
    suffixes=(".go",),
    level=LEVEL_SHALLOW,
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    test_patterns=(r"_test\.go$",),
    func_keywords=("func ",),
    class_keywords=("type ", "struct {", "interface {"),
    string_delims=("`", '"'),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="rust",
    display="Rust",
    suffixes=(".rs",),
    level=LEVEL_SHALLOW,
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    test_patterns=(r"(^|/)tests/", r"#\[test\]"),
    func_keywords=("fn ",),
    class_keywords=("struct ", "enum ", "trait ", "impl "),
    string_delims=('"',),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="ruby",
    display="Ruby",
    suffixes=(".rb",),
    level=LEVEL_SHALLOW,
    line_comment=("#",),
    block_comment=(("=begin", "=end"),),
    test_patterns=(r"_spec\.rb$", r"(^|/)spec/", r"test_.*\.rb$"),
    func_keywords=("def ",),
    class_keywords=("class ", "module "),
    string_delims=('"', "'"),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="php",
    display="PHP",
    suffixes=(".php",),
    level=LEVEL_SHALLOW,
    line_comment=("//", "#"),
    block_comment=(("/*", "*/"),),
    test_patterns=(r"Test\.php$", r"(^|/)tests?/"),
    func_keywords=("function ",),
    class_keywords=("class ", "interface ", "trait "),
    string_delims=('"', "'"),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="csharp",
    display="C#",
    suffixes=(".cs",),
    level=LEVEL_SHALLOW,
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    test_patterns=(r"Tests?\.cs$", r"(^|/)Tests?/"),
    # 同 Java：修饰词不是函数关键字（字段也用它）
    func_keywords=("void ",),
    class_keywords=("class ", "interface ", "struct ", "record "),
    string_delims=('"', "'"),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="kotlin",
    display="Kotlin",
    suffixes=(".kt", ".kts"),
    level=LEVEL_SHALLOW,
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    test_patterns=(r"(^|/)src/test/", r"Test\.kt$"),
    func_keywords=("fun ",),
    class_keywords=("class ", "interface ", "object "),
    string_delims=('"""', '"', "'"),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="swift",
    display="Swift",
    suffixes=(".swift",),
    level=LEVEL_SHALLOW,
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    test_patterns=(r"Tests?\.swift$", r"(^|/)Tests?/"),
    func_keywords=("func ",),
    class_keywords=("class ", "struct ", "protocol ", "extension ", "enum "),
    string_delims=('"""', '"'),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="c",
    display="C",
    suffixes=(".c", ".h"),
    level=LEVEL_SHALLOW,
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    test_patterns=(r"test_.*\.c$", r"(^|/)tests?/"),
    func_keywords=(),
    class_keywords=("struct ", "union "),
    string_delims=('"', "'"),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="cpp",
    display="C++",
    suffixes=(".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"),
    level=LEVEL_SHALLOW,
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    test_patterns=(r"test_.*\.(cc|cpp)$", r"(^|/)tests?/"),
    func_keywords=(),
    class_keywords=("class ", "struct ", "namespace "),
    string_delims=('"', "'"),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="scala",
    display="Scala",
    suffixes=(".scala",),
    level=LEVEL_SHALLOW,
    line_comment=("//",),
    block_comment=(("/*", "*/"),),
    test_patterns=(r"(^|/)src/test/", r"Spec\.scala$", r"Test\.scala$"),
    func_keywords=("def ",),
    class_keywords=("class ", "object ", "trait "),
    string_delims=('"""', '"'),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

_add(Language(
    id="lua",
    display="Lua",
    suffixes=(".lua",),
    level=LEVEL_SHALLOW,
    line_comment=("--",),
    block_comment=(("--[[", "]]"),),
    test_patterns=(r"_spec\.lua$", r"(^|/)spec/"),
    func_keywords=("function ",),
    class_keywords=(),
    string_delims=('"', "'", "[[", "]]"),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))

#: Shell 单独列出来 —— 它是最容易被漏掉的一类「代码」。
#:
#: 漏掉它的代价很具体：CI 配置、部署脚本、容器启动脚本都是 shell，
#: 而这里恰恰反复出现 `eval`、`curl ... | sh`、明文 token、未引用的变量。
#: 本项目的第一次语言清点就漏了 .sh —— 一个含 `eval "$CMD"` 的部署脚本
#: 在扫描报告里完全不存在，看起来像「这个仓库没有 shell 脚本」。
_add(Language(
    id="shell",
    display="Shell",
    suffixes=(".sh", ".bash", ".zsh", ".ksh"),
    level=LEVEL_SHALLOW,
    line_comment=("#",),
    test_patterns=(r"(^|/)tests?/", r"\.bats$", r"test_.*\.sh$"),
    func_keywords=("function ",),
    class_keywords=(),
    string_delims=('"', "'"),
    limits="无 AST：不支持符号图、跨文件调用追踪、测试覆盖分析。",
))


# =============================================================================
# 后缀索引与查询
# =============================================================================

_SUFFIX_INDEX: dict[str, Language] = {}
for _lang in LANGUAGES.values():
    for _suf in _lang.suffixes:
        _SUFFIX_INDEX[_suf.lower()] = _lang


def for_suffix(suffix: str) -> Language | None:
    """按扩展名找语言。``suffix`` 可带点也可不带。"""
    s = (suffix or "").strip().lower()
    if s and not s.startswith("."):
        s = "." + s
    return _SUFFIX_INDEX.get(s)


def for_path(path: str) -> Language | None:
    """按文件路径找语言。"""
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    for lang in LANGUAGES.values():
        for suf in lang.suffixes:
            if name.endswith(suf.lower()):
                return lang
    return None


def all_suffixes() -> tuple[str, ...]:
    """所有已知后缀。给「扫描全部代码文件」的场景用。"""
    return tuple(sorted(_SUFFIX_INDEX))


def suffixes_at_least(level: str) -> tuple[str, ...]:
    """能力至少达到某一级的语言的后缀。"""
    want = _LEVEL_RANK.get(level, 0)
    out: list[str] = []
    for lang in LANGUAGES.values():
        if lang.level_rank >= want:
            out.extend(lang.suffixes)
    return tuple(sorted(set(out)))


def level_of(path: str) -> str:
    """某个文件的分析能力级别。未知语言返回 ``text``。"""
    lang = for_path(path)
    return lang.level if lang else LEVEL_TEXT


def describe(path_or_lang: str) -> str:
    """给模型看的一句话能力说明。未知语言也要有话说。"""
    lang = LANGUAGES.get(path_or_lang) or for_path(path_or_lang)
    if lang is None:
        return "未知语言：只能按纯文本处理（grep / read_file），没有结构分析。"
    if lang.level == LEVEL_DEEP:
        return f"{lang.display}：完整 AST 支持（符号、跨文件调用、测试覆盖）。"
    return f"{lang.display}：{lang.limits}"


def by_level(level: str) -> tuple[Language, ...]:
    return tuple(lang for lang in LANGUAGES.values() if lang.level == level)


# =============================================================================
# 测试文件识别（跨语言）
# =============================================================================


def is_test_file(path: str) -> bool:
    """判断是否测试文件。**跨语言统一入口** —— 不要在各处自己写正则。

    这里把所有语言的 ``test_patterns`` 一起试。刻意不做「先判断语言再匹配」：
    同一个仓库里可能 JS 和 Python 测试混放，按语言分流会漏。
    """
    import re

    p = str(path).replace("\\", "/")
    # 目录级的通用约定先快速命中
    parts = p.split("/")[:-1]
    if any(part in {"test", "tests", "spec", "specs", "__tests__", "testing"} for part in parts):
        return True
    for lang in LANGUAGES.values():
        for pattern in lang.test_patterns:
            try:
                if re.search(pattern, p):
                    return True
            except re.error:
                continue
    return False


def prompt_section() -> str:
    """给 Agent system prompt 用的「语言能力」段落。

    ## 为什么要做成 prompt 片段而不是只写在文档里

    模型不知道自己「在某种语言上只能做到浅层」。结果就是：在 JS 仓库里
    ``who_calls`` 返回空，它理解成「没人调用」—— 那是错误的事实。

    把能力矩阵写进 prompt 后，它至少知道「这个语言我只有规则扫描」，
    于是会把结论表述成「规则层面没发现」，而不是「没有问题」。

    ## 为什么可以进静态前缀（不破坏 KV 缓存）

    这段文字**不随运行变化**（只随代码版本变），所以它与 system prompt 的其余部分
    一样是稳定前缀。绝不能把「本次仓库的语言构成」放进来 —— 那才是每轮变的东西，
    应该在工具返回里给。
    """
    deep = by_level(LEVEL_DEEP)
    shallow = by_level(LEVEL_SHALLOW)
    deep_names = "、".join(l.display for l in deep)
    shallow_names = "、".join(l.display for l in shallow)

    return f"""# 语言能力（重要：按语言分级，不要假设全都支持）

本系统的分析能力**按编程语言分级**，因为项目零依赖、没有通用解析器：

- **{deep_names}**：完整 AST 支持 —— 符号图、跨文件调用追踪（who_calls / impact）、
  测试覆盖（tests_for）都可用。
- **{shallow_names}**：只做**词法级规则扫描**（硬编码密钥、危险反序列化、
  命令注入、弱哈希、调试输出、eval 类调用）。**没有符号图、没有跨文件调用、
  没有测试覆盖**，read_symbols 只给启发式的定义清单。
- 其他语言：只能 read_file / grep 当纯文本处理。

## 因此你必须遵守三条

1. **不要把「查不到」说成「不存在」。** 在浅层语言上调 who_calls / impact 会返回空结果，
   那只说明「本工具在这个语言上做不了」，**不说明没有调用方**。
   正确表述：「在该语言上只能做规则扫描，调用关系需人工核对或用 grep 辅助」。
2. **给结论时标注语言与能力级别。** 「Python 部分做了跨文件调用分析」
   和「JS 部分只做了规则扫描」是两种强度的结论，混在一起会误导评审。
3. **报告命中时，先确认语言支持。** 规则扫描在浅层语言上误报率更高
   （没有 AST，只能靠正则）—— 一条备注在注释或字符串里的匹配要额外核对上下文。

首次接触一个仓库时，``build_graph`` 会同时返回**语言清点**（有哪些语言、各多少文件、
各是什么能力级别）。**先看它，再决定这次能做到什么程度。**
"""


def summary() -> str:
    """一段给人/模型看的支持矩阵。用于工具返回与文档。"""
    deep = by_level(LEVEL_DEEP)
    shallow = by_level(LEVEL_SHALLOW)
    lines = [
        f"深层分析（AST）：{', '.join(l.display for l in deep) or '（无）'}",
        f"浅层分析（规则扫描）：{', '.join(l.display for l in shallow) or '（无）'}",
        "其他语言：按纯文本处理（grep / read_file）",
    ]
    return "\n".join(lines)


__all__ = [
    "LANGUAGES", "LEVEL_DEEP", "LEVEL_SHALLOW", "LEVEL_TEXT",
    "Language", "all_suffixes", "by_level", "describe", "for_path",
    "for_suffix", "is_test_file", "level_of", "suffixes_at_least", "summary",
]
