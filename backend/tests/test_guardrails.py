"""护栏与核验的单元测试。

本文件专门测 guardrails.py 的确定性判定 —— 尤其是「引用行内容核验」
（cross_check_line_content）：它要能拒绝编造/错位的位置，同时不能误杀
真实发现。这两条判据都是实测出来的（见 docs/系统评估与调研-20261019.md）。
"""

from __future__ import annotations

from harness import guardrails


def _write(tmp_path, rel: str, body: str):
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return tmp_path


def test_line_content_accepts_grounded_finding(tmp_path) -> None:
    """描述与所指行对得上 → 可信。"""
    root = _write(
        tmp_path, "app/auth.py",
        'API_KEY = "sk-live-xxx"\nDB_PASSWORD = "prod_pw"\n',
    )
    finding = {
        "file": "app/auth.py", "line": 2,
        "title": "凭据硬编码在源码里",
        "detail": "DB_PASSWORD 明文写死，随仓库泄露",
    }
    ok, _ = guardrails.cross_check_line_content(finding, root)
    assert ok is True


def test_line_content_rejects_wrong_file_kind(tmp_path) -> None:
    """描述说 SQL 注入，但那一行是硬编码密钥 → 拒绝（错位引用）。"""
    root = _write(tmp_path, "app/auth.py", 'API_KEY = "sk-live-xxx"\n')
    finding = {
        "file": "app/auth.py", "line": 1,
        "title": "SQL 注入：用户输入直接拼接进查询",
        "detail": "execute(f\"SELECT * FROM u WHERE n={name}\")",
    }
    ok, why = guardrails.cross_check_line_content(finding, root)
    assert ok is False, why


def test_line_content_rejects_out_of_range(tmp_path) -> None:
    """行号超出文件长度 = 编造或过期。"""
    root = _write(tmp_path, "a.py", "x = 1\n")
    ok, why = guardrails.cross_check_line_content(
        {"file": "a.py", "line": 999, "title": "凭据硬编码"}, root
    )
    assert ok is False
    assert "越界" in why


def test_line_content_rejects_missing_file(tmp_path) -> None:
    ok, why = guardrails.cross_check_line_content(
        {"file": "nope.py", "line": 3, "title": "命令注入"}, tmp_path
    )
    assert ok is False
    assert "不存在" in why


def test_line_content_rejects_structural_line(tmp_path) -> None:
    """指向空行不能靠邻域凑线索 —— 实测这条会把错位引用放过。"""
    root = _write(
        tmp_path, "app/utils.py",
        "def parse_payload(text):\n    items = []\n\n\n    return items\n",
    )
    # 第 3 行是空行
    ok, why = guardrails.cross_check_line_content(
        {"file": "app/utils.py", "line": 3, "title": "路径穿越", "detail": "os.path.join 可越界"},
        root,
    )
    assert ok is False, why


def test_line_content_skips_when_no_cue(tmp_path) -> None:
    """描述里没有可核对的技术线索时不做否定判定（避免把「设计问题」类发现误杀）。"""
    root = _write(tmp_path, "a.py", "def f():\n    return 1\n")
    ok, _ = guardrails.cross_check_line_content(
        {"file": "a.py", "line": 1, "title": "命名不够清晰，建议改名"}, root
    )
    assert ok is True


def test_verify_finding_locations_summarizes_ungrounded(tmp_path) -> None:
    """批量核验只报不可信的，并在提示里给出位置。"""
    root = _write(tmp_path, "a.py", 'x = 1\ny = 2\n')
    warnings = guardrails.verify_finding_locations(
        [
            {"file": "a.py", "line": 1, "title": "边界问题", "detail": "索引越界"},
            {"file": "a.py", "line": 500, "title": "硬编码密钥", "detail": "password 明文"},
        ],
        root,
    )
    assert len(warnings) == 1
    assert "1 条" in warnings[0] and "a.py:500" in warnings[0]


# =============================================================================
# 行号内容核验（不是「文件读没读」，而是「那一行是否真在支撑这条描述」）
# =============================================================================


def _write(tmp_path, rel: str, body: str):
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return tmp_path


def test_line_content_accepts_grounded_finding(tmp_path) -> None:
    """描述与所指行对得上 → 可信。"""
    root = _write(
        tmp_path, "app/auth.py",
        'API_KEY = "sk-live-xxx"\nDB_PASSWORD = "prod_pw"\n',
    )
    finding = {
        "file": "app/auth.py", "line": 2,
        "title": "凭据硬编码在源码里",
        "detail": "DB_PASSWORD 明文写死，随仓库泄露",
    }
    ok, _ = guardrails.cross_check_line_content(finding, root)
    assert ok is True


def test_line_content_rejects_wrong_file_kind(tmp_path) -> None:
    """描述说 SQL 注入，但那一行是硬编码密钥 → 拒绝（错位引用）。"""
    root = _write(tmp_path, "app/auth.py", 'API_KEY = "sk-live-xxx"\n')
    finding = {
        "file": "app/auth.py", "line": 1,
        "title": "SQL 注入：用户输入直接拼接进查询",
        "detail": "execute(f\"SELECT * FROM u WHERE n={name}\")",
    }
    ok, why = guardrails.cross_check_line_content(finding, root)
    assert ok is False, why


def test_line_content_rejects_out_of_range(tmp_path) -> None:
    """行号超出文件长度 = 编造或过期。"""
    root = _write(tmp_path, "a.py", "x = 1\n")
    ok, why = guardrails.cross_check_line_content(
        {"file": "a.py", "line": 999, "title": "凭据硬编码"}, root
    )
    assert ok is False
    assert "越界" in why


def test_line_content_rejects_missing_file(tmp_path) -> None:
    ok, why = guardrails.cross_check_line_content(
        {"file": "nope.py", "line": 3, "title": "命令注入"}, tmp_path
    )
    assert ok is False
    assert "不存在" in why


def test_line_content_rejects_structural_line(tmp_path) -> None:
    """指向空行不能靠邻域凑线索 —— 实测这条会把错位引用放过。"""
    root = _write(
        tmp_path, "app/utils.py",
        "def parse_payload(text):\n    items = []\n\n\n    return items\n",
    )
    # 第 3 行是空行
    ok, why = guardrails.cross_check_line_content(
        {"file": "app/utils.py", "line": 3, "title": "路径穿越", "detail": "os.path.join 可越界"},
        root,
    )
    assert ok is False, why


def test_line_content_skips_when_no_cue(tmp_path) -> None:
    """描述里没有可核对的技术线索时不做否定判定（避免把「设计问题」类发现误杀）。"""
    root = _write(tmp_path, "a.py", "def f():\n    return 1\n")
    ok, _ = guardrails.cross_check_line_content(
        {"file": "a.py", "line": 1, "title": "命名不够清晰，建议改名"}, root
    )
    assert ok is True


def test_verify_finding_locations_summarizes_ungrounded(tmp_path) -> None:
    """批量核验只报不可信的，并在提示里给出位置。"""
    root = _write(tmp_path, "a.py", 'x = 1\ny = 2\n')
    warnings = guardrails.verify_finding_locations(
        [
            {"file": "a.py", "line": 1, "title": "边界问题", "detail": "索引越界"},
            {"file": "a.py", "line": 500, "title": "硬编码密钥", "detail": "password 明文"},
        ],
        root,
    )
    assert len(warnings) == 1
    assert "1 条" in warnings[0] and "a.py:500" in warnings[0]
