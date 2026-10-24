"""CI 集成（SARIF 输出 + diff 过滤）的行为测试。

守四件事：

1. **SARIF 必须合法** —— GitHub 的 schema 很严，一个字段类型不对就整份 reject，
   而错误在服务端，本地看不到。
2. **严重级别映射不能注水** —— warn 不能提成 error，否则真该拦的项被稀释。
3. **指纹必须稳定且不含行号** —— 行号会漂，GitHub 的「已忽略」状态要能跨版本对应。
4. **CI 只报改动范围** —— 历史遗留把 PR 刷红，是「AI 审查被静音」的典型路径。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness.sarif import (
    SARIF_SCHEMA,
    SARIF_VERSION,
    build_sarif,
    rule_metadata,
    summarize,
    write_sarif,
)
from harness.types import Finding


def _f(severity="blocker", file="app/a.py", line=1, title="问题", evidence="x = 1", **kw):
    return Finding(severity=severity, file=file, line=line, title=title,
                   detail=kw.pop("detail", "细节"), evidence=evidence, **kw)


# =============================================================================
# SARIF 结构合法性
# =============================================================================


def test_sarif_has_required_top_level_fields() -> None:
    doc = build_sarif([_f()])
    assert doc["$schema"] == SARIF_SCHEMA
    assert doc["version"] == SARIF_VERSION
    assert isinstance(doc["runs"], list) and len(doc["runs"]) == 1


def test_sarif_run_has_tool_driver_and_results() -> None:
    run = build_sarif([_f()])["runs"][0]
    assert run["tool"]["driver"]["name"]
    assert isinstance(run["tool"]["driver"]["rules"], list)
    assert isinstance(run["results"], list)


def test_sarif_empty_findings_is_still_valid() -> None:
    """零发现也要产出合法文档 —— 否则 CI 里「没问题」反而上传失败。"""
    doc = build_sarif([])
    run = doc["runs"][0]
    assert run["results"] == []
    # 规则表不能为空：GitHub 要求至少有兜底规则，否则后续无法归类
    assert run["tool"]["driver"]["rules"]


def test_sarif_locations_use_relative_uri() -> None:
    doc = build_sarif([_f(file="app/service.py", line=42)])
    loc = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "app/service.py"
    assert loc["region"]["startLine"] == 42


def test_sarif_normalizes_windows_paths() -> None:
    """Windows 反斜杠必须转成正斜杠。

    不转的话 GitHub 找不到文件，表现为「上传成功但一个结果都不显示」——
    这类失败本地完全看不出来。
    """
    doc = build_sarif([_f(file="web\\src\\api.ts", line=6)])
    uri = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert "\\" not in uri
    assert uri == "web/src/api.ts"


def test_sarif_region_is_single_line() -> None:
    """我们只有行号，没有区间。给假的 endLine 会让 GitHub 高亮错范围。"""
    doc = build_sarif([_f(line=10)])
    region = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    assert region["startLine"] == region["endLine"] == 10


def test_sarif_handles_invalid_line_numbers() -> None:
    """行号 0 或负数必须被夹到 1 —— SARIF 要求 startLine >= 1。"""
    for bad in (0, -5):
        doc = build_sarif([_f(line=bad)])
        region = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
        assert region["startLine"] >= 1


# =============================================================================
# 级别映射：不能注水
# =============================================================================


def test_severity_mapping_is_faithful() -> None:
    doc = build_sarif([
        _f(severity="blocker", title="a"),
        _f(severity="warn", title="b"),
        _f(severity="note", title="c"),
    ])
    levels = [r["level"] for r in doc["runs"][0]["results"]]
    assert levels == ["error", "warning", "note"]


def test_warn_is_not_promoted_to_error() -> None:
    """**warn 不能提成 error**。

    GitHub 会把 error 显示成红色并可以让 check 失败。一旦 warn 也标红，
    真正的阻断项就被稀释，开发者会对整片红色脱敏 —— 那等于关掉了这个检查。
    """
    doc = build_sarif([_f(severity="warn")])
    assert doc["runs"][0]["results"][0]["level"] != "error"


def test_unknown_severity_falls_back_to_warning() -> None:
    doc = build_sarif([_f(severity="whatever")])
    assert doc["runs"][0]["results"][0]["level"] == "warning"


# =============================================================================
# 指纹：稳定且不含行号
# =============================================================================


def test_fingerprint_is_stable_across_runs() -> None:
    doc1 = build_sarif([_f(title="同一问题")])
    doc2 = build_sarif([_f(title="同一问题")])
    fp1 = doc1["runs"][0]["results"][0]["partialFingerprints"]
    fp2 = doc2["runs"][0]["results"][0]["partialFingerprints"]
    assert fp1 == fp2


def test_fingerprint_ignores_line_movement() -> None:
    """**行号不进指纹**。

    GitHub 用指纹判断「这是不是上次那个问题」，从而支持「已忽略」「已修复」。
    带行号的话，代码往上挪几行就会被当成新问题，之前标记的误报全部失效。
    """
    a = build_sarif([_f(title="同一问题", line=10)])["runs"][0]["results"][0]
    b = build_sarif([_f(title="同一问题", line=99)])["runs"][0]["results"][0]
    assert a["partialFingerprints"] == b["partialFingerprints"]


def test_fingerprint_differs_per_file_and_title() -> None:
    a = build_sarif([_f(file="a.py", title="t")])["runs"][0]["results"][0]
    b = build_sarif([_f(file="b.py", title="t")])["runs"][0]["results"][0]
    c = build_sarif([_f(file="a.py", title="u")])["runs"][0]["results"][0]
    assert a["partialFingerprints"] != b["partialFingerprints"]
    assert a["partialFingerprints"] != c["partialFingerprints"]


# =============================================================================
# 规则归类
# =============================================================================


def test_rule_id_is_used_when_declared() -> None:
    """``Finding.rule_id`` 能对上已声明的规则时，必须用它归类。

    之前规则 id 被塞在 title 里（``标题（SEC001）``），信息在但不可机读 ——
    GitHub 上会全部显示成「未分类」，无法按规则筛选或批量忽略。
    """
    f = Finding(severity="blocker", file="a.py", line=1, title="硬编码密钥",
                detail="d", evidence="e", rule_id="SEC001")
    doc = build_sarif([f], rules=rule_metadata())
    result = doc["runs"][0]["results"][0]
    assert result["ruleId"] == "SEC001"
    declared = {r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]}
    assert "SEC001" in declared, "引用了未声明的规则 —— GitHub 会显示成未知规则"


def test_unknown_rule_falls_back_to_catch_all() -> None:
    """模型自发现的（没有 rule_id）走兜底规则，不能引用不存在的 id。"""
    f = Finding(severity="warn", file="a.py", line=1, title="模型发现的",
                detail="d", evidence="e")
    doc = build_sarif([f], rules=rule_metadata())
    result = doc["runs"][0]["results"][0]
    declared = {r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]}
    assert result["ruleId"] in declared


def test_every_result_rule_id_is_declared() -> None:
    """所有 result 的 ruleId 都必须出现在 rules 里 —— 这是 SARIF 的硬要求。"""
    fs = [
        Finding(severity="blocker", file="a.py", line=1, title="x", detail="d",
                evidence="e", rule_id="SEC001"),
        Finding(severity="warn", file="b.py", line=2, title="y", detail="d",
                evidence="e", rule_id="NOT_A_RULE"),
        Finding(severity="note", file="c.py", line=3, title="z", detail="d", evidence="e"),
    ]
    doc = build_sarif(fs, rules=rule_metadata())
    declared = {r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]}
    for result in doc["runs"][0]["results"]:
        assert result["ruleId"] in declared


def test_rule_metadata_covers_builtin_rules() -> None:
    meta = rule_metadata()
    ids = {m["id"] for m in meta}
    assert "SEC001" in ids and "SEC003" in ids
    assert all(m.get("severity") and m.get("title") for m in meta)


# =============================================================================
# 写文件
# =============================================================================


def test_write_sarif_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "reports" / "deep" / "out.sarif"
    write_sarif([_f()], target)
    assert target.is_file()
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["version"] == SARIF_VERSION


def test_summarize_counts_by_severity() -> None:
    text = summarize([_f(severity="blocker"), _f(severity="warn"), _f(severity="note")])
    assert "共 3 条" in text
    assert "阻断 1" in text
    assert summarize([]) == "没有发现。"


# =============================================================================
# diff 过滤：CI 里最关键的取舍
# =============================================================================


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    (repo / "app").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    # 注意变量名必须含敏感词（SEC001 判据是「疑似凭据名 + 字面量」）。
    # 写成 KEY 不会命中 —— 一开始我把测试数据写错了，误以为过滤逻辑有问题。
    (repo / "app" / "legacy.py").write_text('API_KEY = "sk_live_legacy_123456"\n', encoding="utf-8")
    (repo / "app" / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_filter_drops_history_outside_the_pr(git_repo: Path) -> None:
    """**CI 只报本次改动**。

    实测场景：全仓有 3 条历史遗留（2 个硬编码密钥 + eval），而 PR 只改了一个
    无害文件。不过滤的话开发者会看到 3 个红点 —— 第一反应是把这个检查关掉。
    这正是「AI 审查变成噪音」的典型路径。

    过滤后输出 0 条，信号是「这个 PR 没引入新问题」，这才是对的。
    """
    from harness.__main__ import _filter_to_diff

    # 本次 PR 只改 main.py（无害）
    (git_repo / "app" / "main.py").write_text(
        "def main():\n    return 2\n", encoding="utf-8"
    )
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "pr")

    from harness.rules import scan_tree, to_findings

    findings = to_findings(scan_tree(git_repo))
    assert findings, "前置条件：全仓应有历史遗留问题"

    kept, note = _filter_to_diff(findings, git_repo, "HEAD~1")
    assert kept == [], f"改动范围外的历史问题不该上报，实际保留了 {kept}"
    assert "省略" in note


def test_filter_keeps_findings_inside_the_pr(git_repo: Path) -> None:
    from harness.__main__ import _filter_to_diff
    from harness.rules import scan_tree, to_findings

    # 本次 PR 在 main.py 里引入密钥
    (git_repo / "app" / "main.py").write_text(
        'def main():\n    API_KEY = "sk_live_new_123456"\n    return 1\n', encoding="utf-8"
    )
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "pr")

    findings = to_findings(scan_tree(git_repo))
    kept, note = _filter_to_diff(findings, git_repo, "HEAD~1")
    files = {f.file for f in kept}
    # 调试：把 note 带进断言，失败时能看到原因
    assert any("main.py" in f for f in files), f"改动范围内的发现被漏掉：{files} | {note}"
    assert not any("legacy.py" in f for f in files), "改动范围外的被保留"


def test_filter_does_not_crash_on_bad_base(git_repo: Path) -> None:
    """base 不存在时不能抛异常 —— CI 里抛异常就是整个 job 失败。

    退回全仓结果并在说明里写清楚，比让流水线炸掉好。
    """
    from harness.__main__ import _filter_to_diff

    findings = [_f(file="app/main.py")]
    kept, note = _filter_to_diff(findings, git_repo, "no-such-ref-xyz")
    assert len(kept) == len(findings)
    assert "全仓" in note or "无法" in note or "为空" in note


# =============================================================================
# CLI：--scan-only 也必须尊重 --diff
# =============================================================================


def test_scan_only_respects_diff_flag(git_repo: Path, capsys) -> None:
    """``--scan-only --diff`` 必须过滤。

    实测踩过的 bug：``--scan-only`` 分支根本没读 ``args.diff``，
    传了会被**静默忽略**，仍然输出全仓结果。那比报错更糟 ——
    CI 里用户以为过滤生效，实际没有，PR 上照样一片红。
    """
    from harness.__main__ import main

    # 本次 PR 只改一个无害文件
    (git_repo / "app" / "main.py").write_text("def main():\n    return 2\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "clean pr")

    sarif_path = git_repo / "out.sarif"
    code = main([
        "-r", str(git_repo),
        "--scan-only",
        "--diff", "HEAD~1",
        "--sarif", str(sarif_path),
    ])
    out = capsys.readouterr().out
    assert "过滤" in out, "没有走过滤分支 —— --diff 被忽略了"

    doc = json.loads(sarif_path.read_text(encoding="utf-8"))
    assert doc["runs"][0]["results"] == [], "改动范围外的历史问题不该进 SARIF"
    # 没有引入阻断级问题 → 退出码 0（历史遗留不该拦住这个 PR）
    assert code == 0


def test_scan_only_exit_code_reflects_the_pr_not_the_repo(git_repo: Path) -> None:
    """退出码要看**过滤后**的结果。

    CI 要拦的是「这个 PR 引入了阻断级问题」，不是「仓库里历史上有阻断级问题」。
    后者会让任何 PR 都过不去 —— 那等于把这个检查废掉。
    """
    from harness.__main__ import main

    (git_repo / "app" / "main.py").write_text(
        'def main():\n    API_KEY = "sk_live_new_blocker_123"\n    return 1\n',
        encoding="utf-8",
    )
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-qm", "bad pr")

    code = main([
        "-r", str(git_repo),
        "--scan-only",
        "--diff", "HEAD~1",
        "--sarif", str(git_repo / "out2.sarif"),
    ])
    assert code == 1, "PR 引入了阻断级问题，退出码应为 1"
