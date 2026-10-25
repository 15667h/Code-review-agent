"""多语言支持的行为测试。

守四件事：

1. **语言注册表是单一事实来源** —— 各处（规则、codegraph、read_symbols）不能各写一份。
2. **能力分级必须如实** —— 深层（AST）与浅层（词法）不能混同，且必须暴露给模型。
3. **跨语言注释剥离不能互相污染** —— ``//`` 在 Python 里是地板除，``#`` 在 Ruby 里是注释。
4. **新增 agent 自动获得语言能力** —— 语言说明在 loop 里统一注入，不靠每个 prompt 抄一遍。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import languages as L
from harness.rules import RULES, _code_part, _iter_lines, scan_source
from harness.sandbox import Workspace
from harness.tools_common import _tool_read_symbols


# =============================================================================
# 注册表
# =============================================================================


def test_registry_covers_the_common_languages() -> None:
    expected = {
        "python", "javascript", "typescript", "java", "go", "rust",
        "ruby", "php", "csharp", "kotlin", "swift", "c", "cpp", "scala", "lua",
    }
    assert expected <= set(L.LANGUAGES)


def test_only_python_is_deep() -> None:
    """零依赖意味着只有标准库 ``ast`` 能给的 Python 是 deep。

    这条守的是「不假装支持」：如果哪天有人把 JS 也标成 deep，
    说明他引入了 tree-sitter 之类的依赖，或者标错了。
    """
    assert {x.id for x in L.by_level(L.LEVEL_DEEP)} == {"python"}
    assert len(L.by_level(L.LEVEL_SHALLOW)) >= 10


def test_every_shallow_language_declares_its_limits() -> None:
    """浅层语言必须写明做不到什么。

    空 ``limits`` 会让模型以为它和 Python 同等支持。
    """
    for lang in L.by_level(L.LEVEL_SHALLOW):
        assert lang.limits.strip(), f"{lang.id} 没有声明能力边界"


def test_unknown_extension_is_text_level() -> None:
    assert L.level_of("README.md") == L.LEVEL_TEXT
    assert L.level_of("data.json") == L.LEVEL_TEXT
    assert L.for_path("README.md") is None


@pytest.mark.parametrize("path,expected", [
    ("app/service.py", "python"),
    ("src/App.tsx", "typescript"),
    ("src/main.js", "javascript"),
    ("pkg/handler.go", "go"),
    ("src/lib.rs", "rust"),
    ("Main.java", "java"),
    ("a.rb", "ruby"),
    ("index.php", "php"),
    ("Foo.cs", "csharp"),
    ("x.kt", "kotlin"),
    ("y.swift", "swift"),
    ("z.c", "c"),
    ("w.cpp", "cpp"),
])
def test_suffix_mapping(path: str, expected: str) -> None:
    lang = L.for_path(path)
    assert lang is not None
    assert lang.id == expected


# =============================================================================
# 测试文件识别（跨语言）
# =============================================================================


@pytest.mark.parametrize("path", [
    "tests/test_a.py", "test_b.py",
    "src/a.test.ts", "src/a.spec.tsx",
    "pkg/x_test.go",
    "src/test/java/A.java",
    "spec/foo_spec.rb",
    "app/__tests__/x.js",
])
def test_test_files_are_recognized_across_languages(path: str) -> None:
    assert L.is_test_file(path) is True


@pytest.mark.parametrize("path", [
    "app/service.py", "src/main.ts", "pkg/handler.go", "Main.java",
])
def test_normal_files_are_not_tests(path: str) -> None:
    assert L.is_test_file(path) is False


# =============================================================================
# 注释剥离：跨语言不能互相污染
# =============================================================================


def test_python_double_slash_is_not_a_comment() -> None:
    """``//`` 在 Python 里是地板除运算符，不能被当成注释砍掉。

    这是最容易犯的错：一套通用的「剥 ``//`` 注释」逻辑会让
    ``total = a // b + c`` 变成 ``total = a``，整行后半段的规则检测全部失效。
    """
    got = _code_part("total = a // b + c  # real comment", ("#",))
    assert "//" in got
    assert "real comment" not in got


def test_js_double_slash_is_a_comment() -> None:
    got = _code_part("x = 1 // comment", ("//",))
    assert "comment" not in got
    assert "x = 1" in got


def test_double_slash_inside_string_is_not_comment() -> None:
    """字符串里的 ``//``（URL）不能被当注释截断。"""
    got = _code_part('const u = "http://example.com" // tail', ("//",))
    assert "http://example.com" in got
    assert "tail" not in got


def test_lua_double_dash_comment() -> None:
    assert _code_part("-- pure comment", ("--",)).strip() == ""
    got = _code_part('s = "a--b" -- tail', ("--",))
    assert "a--b" in got
    assert "tail" not in got


def test_iter_lines_accepts_language_id_or_suffix() -> None:
    """``_iter_lines`` 两种入参都要认。

    之前只认后缀，传语言 id 时静默退回 ``#`` —— 结果 TS 的
    ``// function commented() {}`` 被当成真实定义报出来。
    """
    src = "code // tail"
    for key in ("typescript", ".ts"):
        got = [c for _, c in _iter_lines(src, key)]
        assert "tail" not in got[0], f"{key} 没剥掉注释"


def test_iter_lines_python_keeps_double_slash() -> None:
    got = [c for _, c in _iter_lines("a = b // c", "python")]
    assert "//" in got[0]


# =============================================================================
# 规则：语言分派
# =============================================================================


def test_python_only_rules_do_not_fire_on_other_languages() -> None:
    """Python 专属规则（裸 except / 可变默认参数）不该在 JS 上跑。"""
    js = "try { x() } catch (e) { }\nfunction f(a = []) { }"
    hits = scan_source("app.js", js)
    ids = {h.rule.id for h in hits}
    assert "ERR001" not in ids
    assert "PY001" not in ids


def test_cross_language_rules_fire_on_multiple_languages() -> None:
    """词法安全的规则要在多种语言上都命中。"""
    cases = {
        "a.js": 'const api_key = "sk_live_1234567890";',
        "a.ts": 'let token = "ghp_abcdefghijklmn";',
        "a.go": 'var password = "hunter2secret"',
        "A.java": 'private String secret = "mysecretvalue123";',
        "a.rb": 'password = "rubysecret123"',
        "a.php": '$api_key = "phpkey123456789";',
        "a.kt": 'val accessKey = "ak1234567890abc"',
        "a.cs": 'public string ApiKey = "cskey123456789";',
    }
    for path, src in cases.items():
        ids = {h.rule.id for h in scan_source(path, src)}
        assert "SEC001" in ids, f"{path} 漏掉硬编码密钥"


def test_secret_evidence_is_redacted() -> None:
    """证据里绝不能出现真密钥 —— 报告会流出工作区。"""
    hits = [h for h in scan_source("a.js", 'const token = "sk_live_secret_value";')
            if h.rule.id == "SEC001"]
    assert hits
    assert "sk_live_secret_value" not in hits[0].evidence
    assert "***" in hits[0].evidence


@pytest.mark.parametrize("path,src", [
    ("a.js", "const token = process.env.TOKEN;"),
    ("a.py", 'api_key = os.environ["KEY"]'),
    ("a.js", "const maxItems = 100;"),
    ("a.go", "var count = 42"),
])
def test_secret_rule_does_not_false_positive(path: str, src: str) -> None:
    """环境变量读取、普通变量不该被判成硬编码密钥。"""
    ids = {h.rule.id for h in scan_source(path, src)}
    assert "SEC001" not in ids


def test_eval_rule_covers_language_equivalents() -> None:
    cases = {
        "a.py": "eval(user_input)",
        "a.js": "eval(code)",
        "b.js": "const f = new Function('return 1');",
        "a.php": "$x = eval($code);",
    }
    for path, src in cases.items():
        ids = {h.rule.id for h in scan_source(path, src)}
        assert "SEC003" in ids, f"{path} 漏掉动态执行"


def test_all_langs_matches_registry() -> None:
    """跨语言规则的 ``languages`` 必须与注册表一致。

    硬编码一份列表会让「新语言注册了但规则不跑」，且不会报错。
    """
    from harness.rules import ALL_LANGS

    assert set(ALL_LANGS) == set(L.LANGUAGES)


def test_rule_count_and_language_split() -> None:
    """规则总数与跨语言条数。改规则时如果这个数变了，说明意图要更新。

    当前：14 条规则，其中 6 条跨全部 15 种语言
    （SEC001 硬编码密钥 / SEC003 动态执行 / SEC004 危险反序列化 /
     SEC005 命令注入 / SEC006 弱哈希 / QUAL001 调试输出）。
    其余 8 条是 Python 语法专属（裸 except、可变默认参数等），
    强行跨语言只会误报。
    """
    cross = [r for r in RULES if set(r.languages) == set(L.LANGUAGES)]
    assert len(RULES) >= 14
    assert len(cross) >= 6, f"跨语言规则只有 {len(cross)} 条"


# =============================================================================
# read_symbols：非 Python 走浅层
# =============================================================================


@pytest.fixture()
def mixed_repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.ts").write_text(
        "export class UserService {\n"
        "  async getUser(id: string) {\n"
        "    return db.find(id);\n"
        "  }\n"
        "}\n"
        "export function helper(x: number) { return x; }\n"
        "// function commented() {}\n",
        encoding="utf-8",
    )
    (root / "A.java").write_text(
        "public class A {\n"
        "    private String name = \"x\";\n"
        "    public void run() {\n"
        "        helper();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    return root


def test_read_symbols_works_on_typescript(mixed_repo: Path) -> None:
    tool = _tool_read_symbols(Workspace(mixed_repo))
    text = tool.handler(path="a.ts").render()
    assert "浅层扫描" in text
    assert "UserService" in text
    assert "helper" in text


def test_read_symbols_does_not_report_commented_out_code(mixed_repo: Path) -> None:
    """注释里的 ``function`` 不能被当成定义。"""
    tool = _tool_read_symbols(Workspace(mixed_repo))
    text = tool.handler(path="a.ts").render()
    assert "commented" not in text


def test_read_symbols_does_not_report_java_fields_as_methods(mixed_repo: Path) -> None:
    """``private String name = "x";`` 是字段，不是方法。

    实测踩过：把 ``private `` 当函数关键字，字段全被报成函数。
    """
    tool = _tool_read_symbols(Workspace(mixed_repo))
    text = tool.handler(path="A.java").render()
    assert "run()" in text
    assert '"x"' not in text or "name =" not in text


def test_read_symbols_declares_it_is_heuristic(mixed_repo: Path) -> None:
    """必须标注「不是 AST」，否则模型会当成权威结果。"""
    tool = _tool_read_symbols(Workspace(mixed_repo))
    text = tool.handler(path="a.ts").render()
    assert "不是 AST" in text
    assert "能力边界" in text


def test_read_symbols_rejects_unregistered_language(tmp_path: Path) -> None:
    (tmp_path / "x.zzz").write_text("whatever", encoding="utf-8")
    tool = _tool_read_symbols(Workspace(tmp_path))
    r = tool.handler(path="x.zzz")
    assert r.ok is False
    assert r.error_type == "UnsupportedLanguage"


def test_read_symbols_python_still_uses_ast(tmp_path: Path) -> None:
    """Python 走 AST 的行为不能因为多语言改造而退化。"""
    from harness.tools_common import _tool_read_symbols as t

    (tmp_path / "m.py").write_text("def f(a, b=1):\n    pass\n", encoding="utf-8")
    text = t(Workspace(tmp_path)).handler(path="m.py").render()
    assert "def f" in text
    assert "浅层扫描" not in text


# =============================================================================
# prompt 注入：新增 agent 自动获得
# =============================================================================


def test_prompt_section_mentions_levels() -> None:
    text = L.prompt_section()
    assert "Python" in text
    assert "TypeScript" in text
    assert "浅层" in text or "词法" in text


def test_prompt_section_warns_against_overclaiming() -> None:
    """必须写明「不要把查不到说成不存在」。

    否则模型在 JS 仓库拿到空的 who_calls 结果，会得出「没人调用」的错误结论。
    """
    text = L.prompt_section()
    assert "查不到" in text and "不存在" in text


def test_prompt_section_is_stable() -> None:
    """语言说明必须稳定（进静态前缀，不能破坏 KV 缓存）。"""
    assert L.prompt_section() == L.prompt_section()


def test_loop_injects_language_section(tmp_path: Path) -> None:
    """``run_agent`` 必须把语言段落拼进 system prompt。

    这是「以后新增 agent 自动支持多语言」的机制：不在每个 prompt 里抄，
    而在 loop 里统一追加。这条测试守的就是这个机制本身。
    """
    from harness.config import load_config
    from harness.loop import run_agent
    from harness.offline import HeuristicModel

    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    cfg = load_config()
    result = run_agent(
        goal="g", workspace=Workspace(repo), model=HeuristicModel(max_files=1),
        config=cfg, agent="review", judge=False, max_model_retries=0,
    )
    # prefix_trace 里带着实际进入前缀的 system prompt 摘录或哈希
    assert result.prefix_trace


# =============================================================================
# scan_tree：默认必须扫描所有语言
# =============================================================================


def test_scan_tree_scans_all_registered_languages(tmp_path: Path) -> None:
    """**默认扫描所有语言**，不能只扫 ``.py``。

    实测踩过的 bug：``scan_tree`` 的 ``suffixes`` 默认值是 ``(".py",)``，
    一个混合语言仓库只报了 Python 的命中，TS/Go 里的硬编码密钥一条都没报 ——
    而输出看起来是「扫完了、没问题」。**静默失明比误报危险得多。**

    这个 bug 还差点被漏掉：我第一次改这个函数时字符串替换静默失败了
    （文档字符串不匹配），代码看起来"改过了"其实没变。所以这里用显式断言守死。
    """
    from harness.rules import scan_tree

    (tmp_path / "a.py").write_text('API_KEY = "sk_live_python_123456"\n', encoding="utf-8")
    (tmp_path / "b.ts").write_text('const token = "ghp_typescript_123456";\n', encoding="utf-8")
    (tmp_path / "c.go").write_text('var password = "go_secret_123456"\n', encoding="utf-8")

    report = scan_tree(tmp_path)
    assert report.scanned_files == 3, f"只扫了 {report.scanned_files} 个文件"
    paths = {h.path for h in report.hits}
    assert any(p.endswith(".py") for p in paths)
    assert any(p.endswith(".ts") for p in paths)
    assert any(p.endswith(".go") for p in paths)


def test_scan_tree_respects_explicit_suffixes(tmp_path: Path) -> None:
    """显式给 ``suffixes`` 时只扫那些（保持可收窄的能力）。"""
    from harness.rules import scan_tree

    (tmp_path / "a.py").write_text('API_KEY = "sk_live_python_123456"\n', encoding="utf-8")
    (tmp_path / "b.ts").write_text('const token = "ghp_typescript_123456";\n', encoding="utf-8")

    report = scan_tree(tmp_path, suffixes=(".py",))
    assert report.scanned_files == 1
    assert all(h.path.endswith(".py") for h in report.hits)


def test_prompt_section_is_shared_by_all_agents() -> None:
    """所有 agent 共用同一份语言段落 —— 这是「新增 agent 自动支持」的机制。

    不在每个 prompt 里各写一遍，而是 loop 里统一追加。
    """
    from harness import agents
    from harness.languages import prompt_section

    section = prompt_section()
    assert section == prompt_section()
    assert "语言能力" in section
    for aid in agents.ids():
        spec = agents.get(aid)
        # prompt 里不该各自硬编码一份语言说明（只会在 loop 里追加）
        assert section not in spec.system_prompt


def test_offline_review_does_not_hardcode_python(tmp_path: Path) -> None:
    """离线模式不能只扫 ``*.py``。

    实测踩过的 bug：它硬编码 ``list_files(pattern="*.py")``，
    在混合语言仓库上只报了 Python 的命中，TS/Go 的硬编码密钥全部漏掉，
    而输出看起来是「检查完了、没问题」。
    """
    from harness.config import load_config
    from harness.loop import run_agent
    from harness.offline import HeuristicModel

    (tmp_path / "a.py").write_text('API_KEY = "sk_live_python_123456"\n', encoding="utf-8")
    (tmp_path / "b.ts").write_text('const token = "ghp_typescript_123456";\n', encoding="utf-8")

    cfg = load_config()
    result = run_agent(
        goal="检查", workspace=Workspace(tmp_path),
        model=HeuristicModel(max_files=5), config=cfg,
        agent="review", judge=False, max_model_retries=0,
    )
    files = {f.file for f in result.artifacts.get("finding", [])}
    assert any(f.endswith(".py") for f in files), "Python 没被检查"
    assert any(f.endswith(".ts") for f in files), "TS 没被检查 —— 离线模式仍在只认 .py"


def test_is_source_file_filters_non_code(tmp_path: Path) -> None:
    from harness.offline import _is_source_file

    assert _is_source_file("app/main.py")
    assert _is_source_file("src/App.tsx")
    assert not _is_source_file("README.md")
    assert not _is_source_file("package.json")
    assert not _is_source_file("node_modules/x/index.js")


def test_offline_review_does_not_hardcode_python(tmp_path: Path) -> None:
    """离线模式不能只扫 ``*.py``。

    实测踩过的 bug：它硬编码 ``list_files(pattern="*.py")``，
    在混合语言仓库上只报了 Python 的命中，TS/Go 的硬编码密钥全部漏掉，
    而输出看起来是「检查完了、没问题」。
    """
    from harness.config import load_config
    from harness.loop import run_agent
    from harness.offline import HeuristicModel

    (tmp_path / "a.py").write_text('API_KEY = "sk_live_python_123456"\n', encoding="utf-8")
    (tmp_path / "b.ts").write_text('const token = "ghp_typescript_123456";\n', encoding="utf-8")

    cfg = load_config()
    result = run_agent(
        goal="检查", workspace=Workspace(tmp_path),
        model=HeuristicModel(max_files=5), config=cfg,
        agent="review", judge=False, max_model_retries=0,
    )
    files = {f.file for f in result.artifacts.get("finding", [])}
    assert any(f.endswith(".py") for f in files), "Python 没被检查"
    assert any(f.endswith(".ts") for f in files), "TS 没被检查 —— 离线模式仍在只认 .py"


def test_is_source_file_filters_non_code(tmp_path: Path) -> None:
    from harness.offline import _is_source_file

    assert _is_source_file("app/main.py")
    assert _is_source_file("src/App.tsx")
    assert not _is_source_file("README.md")
    assert not _is_source_file("package.json")
    assert not _is_source_file("node_modules/x/index.js")


# =============================================================================
# 规则误报：真实仓库上跑出来的教训
# =============================================================================


def test_secret_rule_ignores_ui_label_maps() -> None:
    """``password_empty: 'password'`` 是 UI 映射表的键，不是密钥。

    **实测事故**：把 ``:`` 也当赋值后，本项目 ``scripts/auth.js`` 的文案映射表
    被报成 **204 条**假硬编码密钥 —— 噪音大到让人直接关掉整个检查。

    判据要两条同时成立：① 必须是 ``=`` 赋值；② 变量名不能是描述/标签类
    （``_empty`` / ``_label`` / ``_msg`` …）。
    """
    from harness.rules import scan_source

    negatives = [
        "    password_empty: 'password',",
        "    password_short: 'password',",
        "    token_label: '令牌',",
        "    password_min = '8'",
        "    token_url = 'https://x'",
    ]
    for line in negatives:
        ids = {h.rule.id for h in scan_source("app.js", line)}
        assert "SEC001" not in ids, f"误报：{line}"


def test_secret_rule_still_catches_real_assignments() -> None:
    """排除描述名后，真实赋值不能一起被误杀。"""
    from harness.rules import scan_source

    positives = [
        ("a.js", "const api_key = 'sk_live_real_123456'"),
        ("a.py", "API_KEY = 'sk_python_real_123456'"),
        ("a.go", "var password = 'goreal123456'"),
        ("a.ts", "let token = 'ghp_real123456789'"),
    ]
    for path, line in positives:
        ids = {h.rule.id for h in scan_source(path, line)}
        assert "SEC001" in ids, f"漏报：{line}"


def test_sql_rule_ignores_markdown_tables() -> None:
    """Markdown 表格里的 ``from`` 不是 SQL。

    **实测事故**：原判据是「行内有 SELECT/FROM/WHERE 任一 + 有拼接」，
    结果本项目 ``eval/make_report.py`` 的 Markdown 表格拼接被报成
    **34 条**假 SQL 注入。``FROM`` 是普通英文词，单独出现毫无区分度。

    修法：要求 SQL **语句形态**（``SELECT ... FROM`` / ``INSERT INTO`` /
    ``UPDATE x SET`` / ``DELETE FROM`` / ``WHERE col =``），而不是关键字命中。
    """
    from harness.rules import scan_source

    negatives = [
        ('    f"| **平均 F1** | " + " | ".join(x)', "Markdown 表格"),
        ('    label = "| from | to |" + row', "表头"),
        ('    msg = "received from " + sender', "英文句子"),
    ]
    for line, note in negatives:
        ids = {h.rule.id for h in scan_source("eval/make_report.py", line)}
        assert "SEC002" not in ids, f"误报（{note}）：{line}"


def test_sql_rule_still_catches_real_injection() -> None:
    from harness.rules import scan_source

    positives = [
        '    q = "SELECT * FROM users WHERE id = " + uid',
        '    q = f"DELETE FROM orders WHERE id={oid}"',
        '    q = "INSERT INTO t (a) VALUES (" + v + ")"',
        '    q = "UPDATE users SET name = " + n',
    ]
    for line in positives:
        ids = {h.rule.id for h in scan_source("app.py", line)}
        assert "SEC002" in ids, f"漏报：{line}"


def test_debug_print_rule_ignores_entry_points() -> None:
    """CLI 入口与评估脚本里的 print 是**主要功能**，不是调试残留。

    **实测事故**：只豁免 ``scripts/`` ``tools/`` ``examples/`` 时，
    本项目跑出 106 条 QUAL001（占全部命中 54%），
    其中 ``__main__.py`` 一个文件 38 条、``eval/run_eval.py`` 31 条。
    """
    from harness.rules import scan_source

    ok_paths = [
        "backend/harness/__main__.py",
        "backend/eval/run_eval.py",
        "backend/eval/make_report.py",
        "scripts/deploy.py",
    ]
    for path in ok_paths:
        ids = {h.rule.id for h in scan_source(path, "    print('done')")}
        assert "QUAL001" not in ids, f"{path} 的 print 不该报"

    # 库代码里的 print 仍然要报
    ids = {h.rule.id for h in scan_source("backend/harness/core.py", "    print('debug x')")}
    assert "QUAL001" in ids


# =============================================================================
# 规则误报：真实仓库上跑出来的教训
# =============================================================================


def test_secret_rule_ignores_ui_label_maps() -> None:
    """``password_empty: 'password'`` 是 UI 映射表的键，不是密钥。

    **实测事故**：把 ``:`` 也当赋值后，本项目 ``scripts/auth.js`` 的文案映射表
    被报成 **204 条**假硬编码密钥 —— 噪音大到让人直接关掉整个检查。

    判据要两条同时成立：① 必须是 ``=`` 赋值；② 变量名不能是描述/标签类
    （``_empty`` / ``_label`` / ``_msg`` …）。
    """
    from harness.rules import scan_source

    negatives = [
        "    password_empty: 'password',",
        "    password_short: 'password',",
        "    token_label: '令牌',",
        "    password_min = '8'",
        "    token_url = 'https://x'",
    ]
    for line in negatives:
        ids = {h.rule.id for h in scan_source("app.js", line)}
        assert "SEC001" not in ids, f"误报：{line}"


def test_secret_rule_still_catches_real_assignments() -> None:
    """排除描述名后，真实赋值不能一起被误杀。"""
    from harness.rules import scan_source

    positives = [
        ("a.js", "const api_key = 'sk_live_real_123456'"),
        ("a.py", "API_KEY = 'sk_python_real_123456'"),
        ("a.go", "var password = 'goreal123456'"),
        ("a.ts", "let token = 'ghp_real123456789'"),
    ]
    for path, line in positives:
        ids = {h.rule.id for h in scan_source(path, line)}
        assert "SEC001" in ids, f"漏报：{line}"


def test_sql_rule_ignores_markdown_tables() -> None:
    """Markdown 表格里的 ``from`` 不是 SQL。

    **实测事故**：原判据是「行内有 SELECT/FROM/WHERE 任一 + 有拼接」，
    结果本项目 ``eval/make_report.py`` 的 Markdown 表格拼接被报成
    **34 条**假 SQL 注入。``FROM`` 是普通英文词，单独出现毫无区分度。

    修法：要求 SQL **语句形态**（``SELECT ... FROM`` / ``INSERT INTO`` /
    ``UPDATE x SET`` / ``DELETE FROM`` / ``WHERE col =``），而不是关键字命中。
    """
    from harness.rules import scan_source

    negatives = [
        ('    f"| **平均 F1** | " + " | ".join(x)', "Markdown 表格"),
        ('    label = "| from | to |" + row', "表头"),
        ('    msg = "received from " + sender', "英文句子"),
    ]
    for line, note in negatives:
        ids = {h.rule.id for h in scan_source("eval/make_report.py", line)}
        assert "SEC002" not in ids, f"误报（{note}）：{line}"


def test_sql_rule_still_catches_real_injection() -> None:
    from harness.rules import scan_source

    positives = [
        '    q = "SELECT * FROM users WHERE id = " + uid',
        '    q = f"DELETE FROM orders WHERE id={oid}"',
        '    q = "INSERT INTO t (a) VALUES (" + v + ")"',
        '    q = "UPDATE users SET name = " + n',
    ]
    for line in positives:
        ids = {h.rule.id for h in scan_source("app.py", line)}
        assert "SEC002" in ids, f"漏报：{line}"


def test_debug_print_rule_ignores_entry_points() -> None:
    """CLI 入口与评估脚本里的 print 是**主要功能**，不是调试残留。

    **实测事故**：只豁免 ``scripts/`` ``tools/`` ``examples/`` 时，
    本项目跑出 106 条 QUAL001（占全部命中 54%），
    其中 ``__main__.py`` 一个文件 38 条、``eval/run_eval.py`` 31 条。
    """
    from harness.rules import scan_source

    ok_paths = [
        "backend/harness/__main__.py",
        "backend/eval/run_eval.py",
        "backend/eval/make_report.py",
        "scripts/deploy.py",
    ]
    for path in ok_paths:
        ids = {h.rule.id for h in scan_source(path, "    print('done')")}
        assert "QUAL001" not in ids, f"{path} 的 print 不该报"

    # 库代码里的 print 仍然要报
    ids = {h.rule.id for h in scan_source("backend/harness/core.py", "    print('debug x')")}
    assert "QUAL001" in ids


def test_scanner_does_not_flag_its_own_keyword_tables() -> None:
    """**检测器不许扫自己的配置数据**。

    实测事故：PR 上真实报了三条「阻断级」漏洞，全是自我指涉：

    - `guardrails.py` 的关键词元组里有 `"select *"` → 被 SEC002 报成 SQL 注入
    - `rules.py` 的正则源码里有 `exec\\s*\\(` → 被 SEC003 报成动态执行
    - `rules.py` 的规则描述里有 `x == True` → 被 PY003 报成布尔比较

    这类假阳性在 PR 上看起来和真漏洞一模一样，杀伤力比普通误报更大 ——
    它直接消耗评审对系统的信任。

    判据：把字符串字面量剥掉，剩下的如果只有 `():,` 之类结构符号，就是数据。
    """
    from harness.rules import _is_data_line, scan_source

    data_lines = [
        '    "sql": ("sql", "execute(", "f\\"select", "select *", "insert into"),',
        r'    r"|child_process\\.exec\\s*\\(|\\bexec\\s*\\(",',
    ]
    for line in data_lines:
        assert _is_data_line(line), f"未识别为数据行：{line}"
        ids = {h.rule.id for h in scan_source("harness/guardrails.py", line)}
        assert not (ids & {"SEC002", "SEC003"}), f"自我指涉误报：{line} → {ids}"


def test_data_line_detection_does_not_swallow_real_code() -> None:
    """反向保障：真实代码行不能被误判成数据。

    只测「不报」的话，把 _is_data_line 改成恒 True 也能通过。
    """
    from harness.rules import _is_data_line

    code_lines = [
        '    q = "SELECT * FROM users WHERE id = " + uid',
        '    return eval(user_input)',
        '    detail="x == True 是值比较",',
        '    print("done")',
        '    sql = "SELECT * FROM t"',
    ]
    for line in code_lines:
        assert not _is_data_line(line), f"代码行被误判为数据：{line}"


def test_docstrings_do_not_trigger_their_own_rule() -> None:
    """规则描述里写着 ``x == True``，不能被自己的规则命中。

    文档字符串整体跳过 —— 它不是可执行代码。
    """
    from harness.rules import scan_source

    src = (
        '"""说明：``x == True`` 是值比较而非真值判断。"""\n'
        "\n"
        "\n"
        "def f(flag):\n"
        "    return flag == True\n"
    )
    hits = [h for h in scan_source("a.py", src) if h.rule.id == "PY003"]
    # 只有第 5 行的真实代码该被报，文档字符串那行不该
    assert len(hits) == 1
    assert hits[0].line == 5


def test_scanner_does_not_flag_its_own_keyword_tables() -> None:
    """**检测器不许扫自己的配置数据**。

    实测事故：PR 上真实报了三条「阻断级」漏洞，全是自我指涉：

    - `guardrails.py` 的关键词元组里有 `"select *"` → 被 SEC002 报成 SQL 注入
    - `rules.py` 的正则源码里有 `exec\\s*\\(` → 被 SEC003 报成动态执行
    - `rules.py` 的规则描述里有 `x == True` → 被 PY003 报成布尔比较

    这类假阳性在 PR 上看起来和真漏洞一模一样，杀伤力比普通误报更大 ——
    它直接消耗评审对系统的信任。

    判据：把字符串字面量剥掉，剩下的如果只有 `():,` 之类结构符号，就是数据。
    """
    from harness.rules import _is_data_line, scan_source

    data_lines = [
        '    "sql": ("sql", "execute(", "f\\"select", "select *", "insert into"),',
        r'    r"|child_process\.exec\s*\(|\bexec\s*\(",',
    ]
    for line in data_lines:
        assert _is_data_line(line), f"未识别为数据行：{line}"
        ids = {h.rule.id for h in scan_source("harness/guardrails.py", line)}
        assert not (ids & {"SEC002", "SEC003"}), f"自我指涉误报：{line} → {ids}"


def test_data_line_detection_does_not_swallow_real_code() -> None:
    """反向保障：真实代码行不能被误判成数据。

    只测「不报」的话，把 _is_data_line 改成恒 True 也能通过。
    """
    from harness.rules import _is_data_line

    code_lines = [
        '    q = "SELECT * FROM users WHERE id = " + uid',
        '    return eval(user_input)',
        '    detail="x == True 是值比较",',
        '    print("done")',
        '    sql = "SELECT * FROM t"',
    ]
    for line in code_lines:
        assert not _is_data_line(line), f"代码行被误判为数据：{line}"


def test_docstrings_do_not_trigger_their_own_rule() -> None:
    """规则描述里写着 ``x == True``，不能被自己的规则命中。

    文档字符串整体跳过 —— 它不是可执行代码。
    """
    from harness.rules import scan_source

    src = (
        '"""说明：``x == True`` 是值比较而非真值判断。"""\n'
        "\n"
        "\n"
        "def f(flag):\n"
        "    return flag == True\n"
    )
    hits = [h for h in scan_source("a.py", src) if h.rule.id == "PY003"]
    # 只有第 5 行的真实代码该被报，文档字符串那行不该
    assert len(hits) == 1
    assert hits[0].line == 5
