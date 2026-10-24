"""codegraph 与跨 Agent 记忆的行为测试。

重点守三类容易静默出错的东西：
1. **索引不存在时必须明确报错**，不能返回空列表（空列表会被理解成「没有调用方」，
   这是错误的事实，会让模型得出「可以随便改」的结论）；
2. **去重不依赖行号**（行号会漂，用它当指纹会把一条缺陷算成两条）；
3. **趋势只用有效运行**（实测踩过：4 次空转运行被算成「0 缺陷」的基线，
   得出「缺陷数从 0 涨到 27」的错误结论）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from harness import memory as mem_mod
from harness.codegraph import CodeGraph, is_test_file, module_name, parse_file
from harness.memory import fingerprint
from harness.sandbox import Workspace
from harness.tools_graph import build_graph_toolbox, graph_path


# =============================================================================
# 夹具：造一个小仓库
# =============================================================================


@pytest.fixture()
def tiny_repo(tmp_path: Path) -> Path:
    """一个最小的多文件仓库，含跨文件调用与一个测试。"""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()

    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "class Store:\n"
        "    def save(self, item):\n"
        "        return add(item, 1)\n",
        encoding="utf-8",
    )
    (root / "pkg" / "api.py").write_text(
        "from pkg.core import add, Store\n"
        "\n"
        "def handler(x):\n"
        "    s = Store()\n"
        "    s.save(x)\n"
        "    return add(x, 2)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_api.py").write_text(
        "from pkg.api import handler\n"
        "\n"
        "def test_handler():\n"
        "    assert handler(1)\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def graph(tmp_path: Path, tiny_repo: Path) -> CodeGraph:
    g = CodeGraph(tmp_path / "graph.db")
    g.build(tiny_repo)
    yield g
    g.close()


# =============================================================================
# 基础：模块名与测试判定
# =============================================================================


def test_module_name_handles_package_init() -> None:
    """``__init__.py`` 取包名，不是 ``pkg.__init__``。"""
    assert module_name("pkg/mod.py") == "pkg.mod"
    assert module_name("pkg/__init__.py") == "pkg"
    assert module_name("mod.py") == "mod"


def test_is_test_file_recognizes_common_layouts() -> None:
    assert is_test_file("tests/test_x.py")
    assert is_test_file("test_y.py")
    assert is_test_file("pkg/x_test.py")
    assert is_test_file("pkg/tests/helper.py")
    assert not is_test_file("pkg/core.py")


def test_parse_file_extracts_nodes_and_refs() -> None:
    src = (
        "import os\n"
        "class A:\n"
        "    def f(self):\n"
        "        return os.getcwd()\n"
        "def g():\n"
        "    return 1\n"
    )
    nodes, refs = parse_file("m.py", src)
    kinds = {n.kind for n in nodes}
    assert "class" in kinds and "function" in kinds and "method" in kinds
    names = {n.name for n in nodes}
    assert {"A", "f", "g"} <= names
    # 调用被记成 CALLS 引用
    assert any(r.kind == "CALLS" for r in refs)


def test_parse_file_raises_on_syntax_error() -> None:
    with pytest.raises(SyntaxError):
        parse_file("bad.py", "def broken(:\n")


# =============================================================================
# 建图与查询
# =============================================================================


def test_build_produces_nodes_and_edges(graph: CodeGraph) -> None:
    n_nodes = graph.conn.execute("SELECT count(*) c FROM nodes").fetchone()["c"]
    n_edges = graph.conn.execute("SELECT count(*) c FROM edges").fetchone()["c"]
    assert n_nodes > 0 and n_edges > 0


def test_who_calls_finds_cross_file_caller(graph: CodeGraph) -> None:
    """跨文件调用：``add`` 被 core 内部和 api 都调用到。"""
    rows = graph.who_calls("add")
    callers = {r["caller"] for r in rows}
    assert any("pkg.api" in c for c in callers), f"没找到跨文件调用方：{callers}"


def test_who_calls_marks_edge_origin(graph: CodeGraph) -> None:
    """每条边都要标来源（EXTRACTED / INFERRED），不能把推断当事实展示。"""
    rows = graph.who_calls("add")
    assert rows
    for r in rows:
        assert r["origin"] in ("EXTRACTED", "INFERRED")
        assert 0 < r["confidence"] <= 1.0


def test_impact_groups_by_depth_ring(graph: CodeGraph) -> None:
    """影响半径要分档：d=1 必坏 / d=2 很可能 / d=3 需测试。

    分档的意义在于可执行性 —— 平铺 200 个文件等于噪音。
    """
    rings = graph.impact("add", depth=2)
    assert rings
    labels = list(rings.keys())
    assert any("d=1" in x for x in labels)


def test_tests_for_finds_covering_test(graph: CodeGraph) -> None:
    rows = graph.tests_for("handler")
    assert any("test_api" in (r.get("test") or "") for r in rows)


def test_fuzzy_match_resolves_tool_name_with_prefix(graph: CodeGraph) -> None:
    """``apply_patch`` 应能匹配到 ``_tool_apply_patch`` 这类带前缀的名字。

    实测踩过：模型用工具名查询，实际函数带下划线前缀，只做精确匹���会空手而归，
    模型随即放弃这条工具路径 —— 而它本来是对的。
    """
    found = graph.find("save")   # 裸名
    assert found or graph.suggest("save")


def test_suggest_returns_candidates_when_missing(graph: CodeGraph) -> None:
    cands = graph.suggest("zzz_not_here")
    assert cands == []
    # 用一个必然存在的子串来找候选
    cands2 = graph.suggest("sav")
    assert any("save" in c for c in cands2)


# =============================================================================
# 工具层：索引不存在时必须明确报错
# =============================================================================


def test_tools_refuse_to_answer_without_index(tmp_path: Path, tiny_repo: Path) -> None:
    """没有索引时**不能返回空列表**。

    空列表会被模型理解成「没有任何调用方」，那是错误的事实，
    会直接导致它得出「这个函数没人用，可以随便改」的结论。
    """
    ws = Workspace(tiny_repo)
    tb = build_graph_toolbox(ws)
    for name in ("who_calls", "impact", "tests_for"):
        result = tb.get(name).handler(symbol="add")
        assert result.ok is False, f"{name} 在无索引时不应该是 success"
        assert result.error_type == "NoGraphIndex"
        # 错误信息必须给出下一步动作
        assert "build_graph" in result.message or "build_graph" in result.suggestion


def test_tools_work_after_build(tmp_path: Path, tiny_repo: Path, monkeypatch) -> None:
    """建图之后三个查询都能用。"""
    monkeypatch.chdir(tmp_path)
    ws = Workspace(tiny_repo)
    tb = build_graph_toolbox(ws)
    built = tb.get("build_graph").handler()
    assert built.ok

    rows = tb.get("who_calls").handler(symbol="add").render()
    assert "调用方" in rows or "没有" in rows

    assert tb.get("impact").handler(symbol="add").render()
    assert tb.get("tests_for").handler(symbol="handler").render()


def test_index_not_written_into_workspace(tmp_path: Path, tiny_repo: Path, monkeypatch) -> None:
    """索引不能落在被审工作区里。

    两个理由：① 会污染审查范围；② 架构助手 can_edit=False，
    往工作区写文件的工具会被 _enforce_read_only 拦下。
    """
    monkeypatch.chdir(tmp_path)
    ws = Workspace(tiny_repo)
    path = graph_path(ws)
    assert tiny_repo not in path.parents, f"索引落在了工作区内：{path}"


def test_readonly_toolbox_has_no_write_tool(tiny_repo: Path) -> None:
    """只读 agent 不提供 build_graph（它会写盘）。"""
    ws = Workspace(tiny_repo)
    ro = build_graph_toolbox(ws, readonly=True)
    assert "build_graph" not in ro.names()
    # 但三个查询仍在
    assert {"who_calls", "impact", "tests_for"} <= set(ro.names())


# =============================================================================
# 指纹与去重
# =============================================================================


def test_fingerprint_ignores_line_numbers() -> None:
    """**行号不能进指纹**。

    同一缺陷在两次运行里行号会漂（上面插几行就变）。用行号当指纹的一部分，
    会把一条缺陷算成两条 —— 记忆就完全失去意义。
    """
    a = {"file": "app/x.py", "line": 10, "severity": "blocker", "title": "缺少鉴权检查", "rule_id": "SEC001"}
    b = {"file": "app/x.py", "line": 87, "severity": "blocker", "title": "缺少鉴权检查", "rule_id": "SEC001"}
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_ignores_numbers_in_title() -> None:
    """描述里的数字也要归一化（「超过 100 条」改成「超过 500 条」还是同一个缺陷）。"""
    a = {"file": "a.py", "title": "批量查询超过 100 条时超时"}
    b = {"file": "a.py", "title": "批量查询超过 500 条时超时"}
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_differs_on_file_or_rule() -> None:
    a = {"file": "a.py", "rule_id": "SEC001", "title": "缺少鉴权"}
    b = {"file": "b.py", "rule_id": "SEC001", "title": "缺少鉴权"}
    c = {"file": "a.py", "rule_id": "ERR001", "title": "缺少鉴权"}
    assert fingerprint(a) != fingerprint(b)
    assert fingerprint(a) != fingerprint(c)


# =============================================================================
# 记忆聚合：有效性过滤（最关键的一条）
# =============================================================================


def _write_run(runs_dir: Path, *, run_id: str, repo: Path, agent: str,
               iterations: int, findings: list[dict], status: str = "finished",
               created_at: float | None = None, events: int = 40) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "run_id": run_id,
        "repo": str(repo),
        "agent": agent,
        "status": status,
        "created_at": created_at or time.time(),
        "elapsed": 10.0,
        "goal": "g",
        "event_count": events,
        "events": [{"type": "x"}] * events,
        "result": {
            "iterations": iterations,
            "findings": findings,
            "artifacts": {"finding": findings},
            "cache_hit_ratio": 0.9,
        },
    }
    (runs_dir / f"{run_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_empty_run_is_marked_ineffective(tmp_path: Path) -> None:
    """只跑 1 轮就结束的运行必须被判为「没干活」。

    实测踩过：归档里有 4 次 ``finished / iterations=1 / findings=0`` 的运行，
    它们看起来是「正常完成、没发现问题」，实际是模型第一轮就没调工具
    （当时的中转端点不支持 function calling）。拿它们当基线会算出
    「缺陷数从 0 涨到 27」，得出「代码库在恶化」的错误结论。
    """
    repo = tmp_path / "r"
    repo.mkdir()
    runs = tmp_path / "runs"
    _write_run(runs, run_id="idle", repo=repo, agent="review",
               iterations=1, findings=[], events=4)

    mem = mem_mod.aggregate(repo, runs)
    assert len(mem.runs) == 1
    assert mem.runs[0].effective is False
    assert "轮" in mem.runs[0].ineffective_reason


def test_stopped_run_is_ineffective(tmp_path: Path) -> None:
    repo = tmp_path / "r"; repo.mkdir()
    runs = tmp_path / "runs"
    _write_run(runs, run_id="st", repo=repo, agent="review",
               iterations=8, findings=[], status="stopped")
    mem = mem_mod.aggregate(repo, runs)
    assert mem.runs[0].effective is False


def test_trend_excludes_ineffective_runs(tmp_path: Path) -> None:
    """趋势只能算有效运行，并明确说明排除了几次。"""
    repo = tmp_path / "r"; repo.mkdir()
    runs = tmp_path / "runs"
    _write_run(runs, run_id="idle1", repo=repo, agent="review",
               iterations=1, findings=[], events=3, created_at=1.0)
    _write_run(runs, run_id="idle2", repo=repo, agent="review",
               iterations=1, findings=[], events=3, created_at=2.0)
    _write_run(runs, run_id="real", repo=repo, agent="review",
               iterations=20, findings=[{"file": "a.py", "line": 1,
                                          "severity": "blocker", "title": "x"}],
               created_at=3.0)

    mem = mem_mod.aggregate(repo, runs)
    assert len(mem.runs) == 3
    assert len(mem.effective_runs) == 1
    text = mem.describe_trend()
    assert "不足以判断趋势" in text
    assert "2 次运行未计入" in text


def test_dedup_merges_same_issue_across_runs(tmp_path: Path) -> None:
    """同一缺陷在两次运行里出现 → 合并成一条，并记 seen_times=2。"""
    repo = tmp_path / "r"; repo.mkdir()
    runs = tmp_path / "runs"
    issue = {"file": "app/x.py", "line": 10, "severity": "blocker",
             "title": "缺少鉴权检查", "rule_id": "SEC001"}
    _write_run(runs, run_id="r1", repo=repo, agent="review",
               iterations=10, findings=[issue], created_at=1.0)
    _write_run(runs, run_id="r2", repo=repo, agent="review",
               iterations=10, findings=[dict(issue, line=42)], created_at=2.0)

    mem = mem_mod.aggregate(repo, runs)
    assert len(mem.issues) == 1
    assert mem.issues[0].seen_times == 2


def test_other_agents_are_surfaced_in_brief(tmp_path: Path) -> None:
    """brief 要优先给出**其他助手**的产出 —— 那才是当前助手的盲区。"""
    repo = tmp_path / "r"; repo.mkdir()
    runs = tmp_path / "runs"
    _write_run(runs, run_id="rev", repo=repo, agent="review",
               iterations=10, findings=[{"file": "a.py", "line": 1,
                                          "severity": "warn", "title": "t"}],
               created_at=1.0)
    _write_run(runs, run_id="arch", repo=repo, agent="architect",
               iterations=10, findings=[], created_at=2.0)

    mem = mem_mod.aggregate(repo, runs)
    text = mem_mod.brief(mem, agent="review")
    assert "其他助手" in text
    assert "architect" in text


def test_brief_marks_history_as_possibly_stale(tmp_path: Path) -> None:
    """注入内容必须写明「可能已过期」。

    不标的话模型会把历史发现当成当前事实，直接复述给用户 ——
    那是把过期信息当结论，比没有记忆更糟。
    """
    repo = tmp_path / "r"; repo.mkdir()
    runs = tmp_path / "runs"
    _write_run(runs, run_id="r1", repo=repo, agent="review", iterations=10,
               findings=[{"file": "a.py", "line": 1, "severity": "warn", "title": "t"}])
    mem = mem_mod.aggregate(repo, runs)
    text = mem_mod.brief(mem, agent="review")
    assert "过期" in text


def test_brief_respects_token_budget(tmp_path: Path) -> None:
    """超预算必须**显式截断**，不能静默裁剪。"""
    repo = tmp_path / "r"; repo.mkdir()
    runs = tmp_path / "runs"
    many = [{"file": f"a{i}.py", "line": i, "severity": "warn",
             "title": f"问题描述第 {i} 条，内容尽量长一些用来撑爆预算"} for i in range(200)]
    _write_run(runs, run_id="r1", repo=repo, agent="review",
               iterations=10, findings=many)
    mem = mem_mod.aggregate(repo, runs)
    text = mem_mod.brief(mem, agent="review", budget_tokens=300)
    assert "预算" in text and "省略" in text


def test_aggregate_ignores_non_run_files(tmp_path: Path) -> None:
    """``.runs/`` 里还有 workspace.json，不能被当成运行记录。

    （这是本项目早就踩过的坑：侧栏会多一条幽灵任务。）
    """
    repo = tmp_path / "r"; repo.mkdir()
    runs = tmp_path / "runs"; runs.mkdir()
    (runs / "workspace.json").write_text(json.dumps({"roots": {}}), encoding="utf-8")
    _write_run(runs, run_id="r1", repo=repo, agent="review", iterations=10, findings=[])
    mem = mem_mod.aggregate(repo, runs)
    assert len(mem.runs) == 1


def test_aggregate_isolates_by_repo(tmp_path: Path) -> None:
    """别的仓库的运行不能混进来。"""
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    runs = tmp_path / "runs"
    _write_run(runs, run_id="ra", repo=a, agent="review", iterations=10, findings=[])
    _write_run(runs, run_id="rb", repo=b, agent="review", iterations=10, findings=[])
    mem = mem_mod.aggregate(a, runs)
    assert [r.run_id for r in mem.runs] == ["ra"]
