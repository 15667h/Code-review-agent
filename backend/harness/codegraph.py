"""仓库代码图：把「谁定义了什么、谁调用了谁」变成可查询的结构。

## 为什么需要这个

审查助手现在只有 grep（正则文本）和 read_symbols（**单文件** AST）。
跨文件的「谁调用了我」完全靠模型自己 grep 猜。实测后果很具体：
改了一个函数签名，三个调用点会坏，但审查器从来没读过那三个文件。

测试助手、导读助手、事故定位也都卡在同一件事上：不知道调用关系和测试覆盖。

## 设计取舍（三条，都有明确理由）

**0. 能力按语言分级，不假装全语言同等支持。**
Python 有标准库 ``ast``，能做完整结构图；其他语言没有解析器（项目零依赖），
**只被登记为「文件存在」**，不进符号图。这条必须如实暴露：
如果 ``who_calls`` 在 JS 仓库返回空，模型会理解成「没人调用」——
那是错误的事实。所以查询结果里会带上「该语言只支持文本级」的提示。

**0. 能力按语言分级，不假装全语言同等支持。**
Python 有标准库 ``ast``，能做完整结构图；其他语言没有解析器（项目零依赖），
**只被登记为「文件存在」**，不进符号图。这条必须如实暴露：
如果 ``who_calls`` 在 JS 仓库返回空，模型会理解成「没人调用」——
那是错误的事实。所以查询结果里会带上「该语言只支持文本级」的提示。

**1. 只做结构图，不做语义检索。**
不引 embedding、不引向量库。理由是项目定位：平台核心只用标准库。
实测过：``ast`` 一次 walk 就能拿到节点与 IMPORTS/CALLS 边，
tree-sitter 的唯一增量价值是「多语言」，所以把它留成**可选后端**。

**2. 边必须分 EXTRACTED / INFERRED，并且给出置信度。**
静态解析拿不到动态分发。把推断出来的边当成事实展示，等于骗人 ——
这是「近失方案」的典型形态：看起来给出了影响范围，实际漏了关键调用点。
所以：**能从语法直接读到的标 EXTRACTED，需要推断的标 INFERRED 并降置信度。**

**3. 节点只存 span，不存正文。**
索引要小、要能增量更新、要避免和仓库内容不同步。
需要正文时按 file + line 回读磁盘。

## 已知失效模式（这些必须如实报出来，不能假装没有）

- **动态调用**：``getattr`` / 注册表字典 / 装饰器包装 / ``**kwargs`` 转发，
  静态解析拿不到目标符号。这类边不会出现在 CALLS 里，或只以低置信度出现。
- **跨文件符号解析**：``from pkg.mod import name`` 要知道 ``pkg.mod`` 对应哪个文件。
  这里用「模块路径 → 文件路径」的猜测规则，会猜错（尤其是同名的多个包）。
- **同名符号**：三个类里都有 ``run()``，``who_calls("run")`` 分不清是哪个。
  接口接受限定名（``Class.method``）来缓解，但纯名字仍然是模糊的。

这些不是「待修的 bug」，是静态分析的固有边界。报告里要写清楚。
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

# =============================================================================
# 常量
# =============================================================================

#: 索引文件版本。改了抽取逻辑就加一 —— 旧索引会被判为过期并重建，
#: 而不是用旧 schema 读新代码（那会给出静默错误的结果）。
SCHEMA_VERSION = 1

#: 索引目录名。
#:
#: **刻意不放在被审工作区里**，两个理由：
#: ① 放进去会污染审查范围 —— 审查器会把自己的索引当被审代码扫一遍；
#: ② 架构助手是 ``can_edit=False``（只读），任何往工作区写文件的工具都会被
#:    ``_enforce_read_only`` 拦下。索引是「分析与缓存」，不是「改代码」，
#:    放到归档目录（``.runs/codegraph/``，本就 gitignore）才符合语义。
GRAPH_DIR = "codegraph"

#: 参与**符号解析**的后缀。只有这些语言能进节点/边。
#: 其它语言仍会被「清点」（记录文件存在与能力级别），但不进符号图。
PY_SUFFIX = ".py"

#: 测试文件的识别规则（用于 TESTED_BY 边）
_TEST_DIR_NAMES = {"test", "tests", "spec", "specs", "__tests__"}
_TEST_FILE_PATTERNS = (
    re.compile(r"^test_.*\.py$"),
    re.compile(r"^.*_test\.py$"),
    re.compile(r"^conftest\.py$"),
)

#: 调用置信度。EXTRACTED = 语法里直接写着；INFERRED = 靠规则推出来的。
CONF_EXTRACTED = 1.0
CONF_INFERRED_IMPORT = 0.85   # 通过 import 表解析到的目标
CONF_INFERRED_SELF = 0.7      # 同类内方法调用
CONF_INFERRED_NAME = 0.45     # 只有裸名字，靠全局同名匹配


# =============================================================================
# 数据结构
# =============================================================================


@dataclass(slots=True)
class Node:
    """一个代码实体。``qname`` 是限定名，用于消歧。"""

    qname: str          # 限定名：pkg.mod.Class.method
    kind: str           # module / class / function / method
    name: str           # 短名
    file: str           # 相对工作区的 posix 路径
    line: int
    end_line: int = 0
    parent: str = ""    # 所属类/函数的 qname

    def to_row(self) -> tuple:
        return (self.qname, self.kind, self.name, self.file, self.line,
                self.end_line, self.parent)


@dataclass(slots=True)
class Edge:
    """一条有向关系。"""

    src: str            # 源 qname（调用方 / 导入方）
    dst: str            # 目标 qname
    kind: str           # CALLS / IMPORTS / DEFINES / TESTED_BY
    confidence: float
    origin: str         # EXTRACTED / INFERRED
    file: str = ""
    line: int = 0

    def to_row(self) -> tuple:
        return (self.src, self.dst, self.kind, self.confidence,
                self.origin, self.file, self.line)


@dataclass
class GraphStats:
    """建图结果统计。给调用方看的，也是自我核验的依据。"""

    files: int = 0
    nodes: int = 0
    edges: int = 0
    extracted: int = 0
    inferred: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    #: 语言清点：``{语言 id: {"files": n, "level": "deep"/"shallow", "display": 名字}}``
    #: 只统计**有文件**的语言。用来回答「这个仓库我能分析到什么程度」。
    languages: dict[str, dict[str, Any]] = field(default_factory=dict)

    def language_summary(self) -> str:
        """语言构成与能力级别。给模型看的「我能做到什么」。"""
        if not self.languages:
            return "没有识别到任何已注册语言的源文件。"
        deep = [i for i, v in self.languages.items() if v.get("level") == "deep"]
        shallow = [i for i, v in self.languages.items() if v.get("level") != "deep"]
        total = sum(v.get("files", 0) for v in self.languages.values())
        lines = [f"共 {total} 个源文件，涉及 {len(self.languages)} 种语言："]
        for lid, info in sorted(self.languages.items(),
                                key=lambda kv: -kv[1].get("files", 0)):
            lines.append(
                f"  - {info.get('display', lid)}：{info.get('files', 0)} 个文件"
                f"（{info.get('level', 'text')}）"
            )
        if deep:
            lines.append(f"可做完整结构分析：{', '.join(deep)}")
        if shallow:
            lines.append(
                f"仅规则扫描（无符号图 / 无跨文件调用）：{', '.join(shallow)}"
            )
        return "\n".join(lines)

    def describe(self) -> str:
        parts = [
            f"扫描 {self.files} 个 Python 文件",
            f"{self.nodes} 个符号",
            f"{self.edges} 条边",
            f"（直接读到 {self.extracted} 条 / 推断 {self.inferred} 条）",
        ]
        if self.skipped:
            parts.append(f"跳过 {self.skipped} 个")
        if self.errors:
            parts.append(f"{len(self.errors)} 个文件解析失败")
        return "，".join(parts)


# =============================================================================
# 符号抽取：单文件 AST → 节点与原始引用
# =============================================================================


@dataclass(slots=True)
class RawRef:
    """文件内的一条待解析引用（还没有确定目标）。"""

    src: str            # 出边的源 qname
    kind: str           # CALLS / IMPORTS
    target: str         # 目标短名或模块路径（未解析）
    line: int
    via: str = ""       # 'name' / 'attr' / 'import' / 'self'


def module_name(rel_path: str) -> str:
    """把相对路径转成模块名：``pkg/mod.py`` → ``pkg.mod``；``__init__.py`` 取包名。"""
    p = rel_path.replace("\\", "/")
    if p.endswith(PY_SUFFIX):
        p = p[: -len(PY_SUFFIX)]
    parts = [x for x in p.split("/") if x]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else "(root)"


def is_test_file(rel_path: str) -> bool:
    """判断是否测试文件。用于 TESTED_BY 边。

    委托给 ``languages.is_test_file`` —— 那份实现覆盖所有已注册语言的
    测试文件命名约定。自己维护一份只会漏掉别的语言。
    """
    from .languages import is_test_file as _is_test

    return _is_test(rel_path)


def parse_file(rel_path: str, source: str) -> tuple[list[Node], list[RawRef]]:
    """解析一个 Python 文件，产出节点与未解析的引用。

    只抽四种边需要的原料，不做数据流分析 —— 那是 PDG 层的事，这里刻意不做。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        raise

    mod = module_name(rel_path)
    nodes: list[Node] = [Node(qname=mod, kind="module", name=mod, file=rel_path,
                              line=1, end_line=source.count("\n") + 1)]
    refs: list[RawRef] = []

    # 类/函数可能嵌套，用栈维护当前作用域；scope_kind 记录每个栈帧是类还是函数。
    # 之前判断「是不是方法」靠扫一遍 nodes 找同类节点，是 O(n²)；
    # 这里直接用栈顶的 kind，O(1)。
    stack: list[str] = [mod]
    scope_kind: list[str] = ["module"]
    owner_of: dict[str, str] = {}

    def current() -> str:
        return stack[-1]

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> Any:
            parent = current()
            qn = f"{parent}.{node.name}"
            nodes.append(Node(qname=qn, kind="class", name=node.name, file=rel_path,
                              line=node.lineno, end_line=getattr(node, "end_lineno", 0) or 0,
                              parent=parent))
            owner_of.setdefault(node.name, qn)
            stack.append(qn)
            scope_kind.append("class")
            self.generic_visit(node)
            scope_kind.pop()
            stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            self._func(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
            self._func(node)

        def _func(self, node: ast.AST) -> None:
            name = getattr(node, "name", "")
            parent = current()
            # 直接父作用域是类 → 这是一个方法；否则是函数
            kind = "method" if scope_kind[-1] == "class" else "function"
            qn = f"{parent}.{name}"
            nodes.append(Node(qname=qn, kind=kind, name=name, file=rel_path,
                              line=node.lineno,
                              end_line=getattr(node, "end_lineno", 0) or 0,
                              parent=parent))
            owner_of.setdefault(name, qn)
            stack.append(qn)
            scope_kind.append("function")
            # 函数体内的调用要算在函数头上，所以继续往下走
            for child in ast.iter_child_nodes(node):
                self.visit(child)
            scope_kind.pop()
            stack.pop()

        def visit_Import(self, node: ast.Import) -> Any:
            for alias in node.names:
                refs.append(RawRef(src=current(), kind="IMPORTS",
                                   target=alias.name, line=node.lineno, via="import"))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
            base = node.module or ""
            # level > 0 表示相对导入，这里记为相对路径，稍后按本文件所在包解析
            if node.level:
                base = "." * node.level + base
            for alias in node.names:
                if alias.name == "*":
                    refs.append(RawRef(src=current(), kind="IMPORTS",
                                       target=base, line=node.lineno, via="import"))
                else:
                    refs.append(RawRef(src=current(), kind="IMPORTS",
                                       target=f"{base}.{alias.name}" if base else alias.name,
                                       line=node.lineno, via="import"))

        def visit_Call(self, node: ast.Call) -> Any:
            target, via = _call_target(node)
            if target:
                refs.append(RawRef(src=current(), kind="CALLS",
                                   target=target, line=node.lineno, via=via))
            # 继续下钻，抓嵌套调用 f(g())
            self.generic_visit(node)

    def _call_target(node: ast.Call) -> tuple[str, str]:
        """从 Call 节点推出目标名。

        能直接读到的三种形态：
        - ``foo()``            → ('foo', 'name')
        - ``obj.method()``     → ('method', 'attr')，obj 是什么静态上不知道
        - ``self.method()``    → ('method', 'self')

        更复杂的（``getattr(x, 'm')()``、``d[k]()``）静态拿不到 —— 返回空。
        """
        f = node.func
        if isinstance(f, ast.Name):
            return f.id, "name"
        if isinstance(f, ast.Attribute):
            if isinstance(f.value, ast.Name) and f.value.id in ("self", "cls"):
                return f.attr, "self"
            return f.attr, "attr"
        return "", ""

    Visitor().visit(tree)
    return nodes, refs


# =============================================================================
# 索引构建
# =============================================================================


def _iter_py_files(root: Path, skip_dirs: set[str], limit: int = 5000) -> Iterator[Path]:
    """按路径顺序遍历 .py 文件，跳过隐藏目录与 skip_dirs。

    ``limit`` 是**硬上限**：到顶就停。之前写成先 yield 再判断，会多给一个文件 ——
    这种「差一个」在索引里不容易被发现，但会让统计数字和实际内容对不上。
    """
    count = 0
    for path in sorted(root.rglob(f"*{PY_SUFFIX}")):
        if count >= limit:
            return
        if any(
            part.startswith(".") or part in skip_dirs
            for part in path.relative_to(root).parts[:-1]
        ):
            continue
        if path.name.startswith("."):
            continue
        count += 1
        yield path


class CodeGraph:
    """仓库结构图。构建一次落盘到 SQLite，之后只读。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    # ---------------------------------------------------------------- 连接

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            c = sqlite3.connect(str(self.db_path))
            c.row_factory = sqlite3.Row
            self._conn = c
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS nodes (
                qname TEXT PRIMARY KEY, kind TEXT, name TEXT,
                file TEXT, line INTEGER, end_line INTEGER, parent TEXT
            );
            CREATE TABLE IF NOT EXISTS edges (
                src TEXT, dst TEXT, kind TEXT, confidence REAL,
                origin TEXT, file TEXT, line INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
            CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file);
            CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
            CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
            CREATE INDEX IF NOT EXISTS idx_edges_kind ON edges(kind);
            """
        )

    def schema_version(self) -> int:
        try:
            row = self.conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
            return int(row["v"]) if row else 0
        except sqlite3.Error:
            return 0

    # ---------------------------------------------------------------- 建图

    def build(self, root: Path, *, skip_dirs: set[str] | None = None) -> GraphStats:
        """全量重建索引。"""
        started = time.time()
        stats = GraphStats()
        root = Path(root).resolve()
        skip = set(skip_dirs or DEFAULT_SKIP)

        self.close()
        if self.db_path.exists():
            self.db_path.unlink()

        self._create_schema()
        conn = self.conn
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('root', ?)", (str(root),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', ?)", (str(int(started)),))

        all_nodes: list[Node] = []
        all_refs: list[tuple[str, list[RawRef], bool]] = []   # (rel, refs, is_test)
        file_hashes: dict[str, str] = {}

        # ---- 语言清点：先扫一遍全部已注册语言的文件 ----
        # 这一步与「符号解析」分开：非 Python 语言也值得被清点出来，
        # 否则 Agent 会以为「仓库里只有 Python」。
        from .languages import LANGUAGES as _LANGS

        _suffix_to_lang = {}
        for _lid, _l in _LANGS.items():
            for _suf in _l.suffixes:
                _suffix_to_lang[_suf.lower()] = (_lid, _l.display, _l.level)

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel_parts = path.relative_to(root).parts
            if any(part.startswith(".") or part in skip for part in rel_parts[:-1]):
                continue
            info = _suffix_to_lang.get(path.suffix.lower())
            if not info:
                continue
            value = stats.languages.setdefault(
                info[0], {"files": 0, "level": info[2], "display": info[1]}
            )
            value["files"] += 1

        for path in _iter_py_files(root, skip):
            rel = path.relative_to(root).as_posix()
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                stats.errors.append(f"{rel}: {exc}")
                continue
            stats.files += 1
            file_hashes[rel] = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
            try:
                nodes, refs = parse_file(rel, source)
            except SyntaxError as exc:
                stats.skipped += 1
                stats.errors.append(f"{rel}: 语法错误（第 {exc.lineno} 行）")
                continue
            all_nodes.extend(nodes)
            all_refs.append((rel, refs, is_test_file(rel)))

        # ---- 解析引用：目标名 → 真实 qname ----
        by_name: dict[str, list[str]] = {}
        by_file_module: dict[str, str] = {}
        for n in all_nodes:
            by_name.setdefault(n.name, []).append(n.qname)
        for n in all_nodes:
            if n.kind == "module":
                by_file_module[n.file] = n.qname

        edges: list[Edge] = []
        seen: set[tuple[str, str, str]] = set()

        def add_edge(e: Edge) -> None:
            key = (e.src, e.dst, e.kind)
            if e.src == e.dst or key in seen:
                return
            seen.add(key)
            edges.append(e)

        # DEFINES：模块 → 顶层定义
        for n in all_nodes:
            if n.parent and n.kind in ("class", "function", "method"):
                mod = by_file_module.get(n.file)
                if mod:
                    add_edge(Edge(mod, n.qname, "DEFINES", CONF_EXTRACTED, "EXTRACTED"))

        # 每个文件的 import 表：短名 → 目标模块/符号
        imports_of: dict[str, dict[str, str]] = {}
        for rel, refs, _ in all_refs:
            table: dict[str, str] = {}
            for r in refs:
                if r.kind == "IMPORTS":
                    short = r.target.rsplit(".", 1)[-1]
                    table[short] = r.target
            imports_of[rel] = table

        for rel, refs, is_test in all_refs:
            mod = by_file_module.get(rel, "")
            table = imports_of.get(rel, {})

            for r in refs:
                if r.kind == "IMPORTS":
                    dst = self._resolve_import(r.target, rel, table, by_file_module)
                    if dst:
                        add_edge(Edge(r.src, dst, "IMPORTS", CONF_INFERRED_IMPORT,
                                      "EXTRACTED" if dst == r.target else "INFERRED",
                                      rel, r.line))
                    continue

                # CALLS：按可见性从高到低尝试
                dst, conf, origin = "", 0.0, "INFERRED"

                # ① 本文件内同名定义 —— 直接读到，最高置信
                local = [n for n in by_name.get(r.target, []) if n.startswith(mod + ".")
                         or n == r.target]
                if local:
                    dst, conf, origin = local[0], CONF_EXTRACTED, "EXTRACTED"

                # ② 通过 import 表解析到外部模块的符号
                elif r.via in ("name", "attr") and r.target in table:
                    full = table[r.target]
                    cands = by_name.get(r.target, [])
                    if cands:
                        # 优先匹配模块路径前缀一致的
                        pref = full.rsplit(".", 1)[0]
                        pick = next((c for c in cands if c.startswith(pref + ".")), cands[0])
                        dst, conf = pick, CONF_INFERRED_IMPORT

                # ③ self.method() —— 在同类/父类里找
                elif r.via == "self":
                    scope = r.src.rsplit(".", 1)[0]
                    cands = [q for q in by_name.get(r.target, []) if q.startswith(scope + ".")]
                    if cands:
                        dst, conf = cands[0], CONF_INFERRED_SELF

                # ④ 裸名字全局唯一 —— 低置信
                elif len(by_name.get(r.target, [])) == 1:
                    dst, conf = by_name[r.target][0], CONF_INFERRED_NAME

                if dst:
                    add_edge(Edge(r.src, dst, "CALLS", conf, origin, rel, r.line))

        # TESTED_BY：测试文件里出现的调用/导入，指向被测符号
        tests_by_target: dict[str, list[str]] = {}
        for e in edges:
            if e.kind in ("CALLS", "IMPORTS"):
                src_node = next((n for n in all_nodes if n.qname == e.src), None)
                if src_node and is_test_file(src_node.file):
                    tests_by_target.setdefault(e.dst, []).append(e.src)
        for target, tests in tests_by_target.items():
            for t in tests:
                add_edge(Edge(t, target, "TESTED_BY", CONF_INFERRED_NAME, "INFERRED"))

        # ---- 落盘 ----
        conn.executemany("INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?)",
                         [n.to_row() for n in all_nodes])
        conn.executemany("INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?,?,?)",
                         [e.to_row() for e in edges])
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('hashes', ?)",
                     (json.dumps(file_hashes),))
        conn.commit()

        stats.nodes = len(all_nodes)
        stats.edges = len(edges)
        stats.extracted = sum(1 for e in edges if e.origin == "EXTRACTED")
        stats.inferred = stats.edges - stats.extracted
        stats.elapsed = time.time() - started
        return stats

    @staticmethod
    def _resolve_import(target: str, rel: str, table: dict[str, str],
                        by_module: dict[str, str]) -> str:
        """把 import 目标串解析成 qname。解析不到时返回空。"""
        if target.startswith("."):
            # 相对导入：按本文件所在包层层上退
            depth = len(target) - len(target.lstrip("."))
            rest = target[depth:]
            base_parts = module_name(rel).split(".")
            if module_name(rel) == "(root)":
                base_parts = []
            base_parts = base_parts[:-1] if base_parts else []
            keep = max(0, len(base_parts) - (depth - 1))
            parts = base_parts[:keep] + ([rest] if rest else [])
            target = ".".join([p for p in parts if p])

        # 逐级前缀尝试：pkg.mod.Thing → pkg.mod
        pieces = target.split(".")
        for cut in range(len(pieces), 0, -1):
            cand = ".".join(pieces[:cut])
            hit = next((q for q in by_module.values() if q == cand), None)
            if hit:
                return hit
        return ""

    # ---------------------------------------------------------------- 查询

    def who_calls(self, symbol: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """谁调用了这个符号。返回 CALLS 边的反向查询结果。"""
        targets = self._match_symbols(symbol)
        if not targets:
            return []
        qnames = [t for t, _ in targets]
        placeholders = ",".join("?" * len(qnames))
        rows = self.conn.execute(
            f"""SELECT e.src AS caller, e.dst AS callee, e.confidence, e.origin, e.file, e.line,
                       n.file AS caller_file, n.line AS caller_line, n.kind AS caller_kind
                FROM edges e LEFT JOIN nodes n ON n.qname = e.src
                WHERE e.kind='CALLS' AND e.dst IN ({placeholders})
                ORDER BY e.confidence DESC, e.line
                LIMIT ?""",
            (*qnames, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def impact(self, symbol: str, *, depth: int = 2, limit: int = 200) -> dict[str, list[dict]]:
        """影响半径，按深度环分组。

        d=1：直接调用方（改了签名一定坏）
        d=2：间接依赖（很可能受影响）
        d=3：传递影响（需要跑测试确认）

        为什么要分环而不是给一个平铺列表：平铺列表无法表达「这个必坏、那个也许」，
        评审看到 200 个文件只会当噪音。
        """
        rings: dict[str, list[dict]] = {}
        frontier = [t for t, _ in self._match_symbols(symbol)]
        visited: set[str] = set(frontier)
        for level in range(1, depth + 3):
            if not frontier:
                break
            placeholders = ",".join("?" * len(frontier))
            rows = self.conn.execute(
                f"""SELECT DISTINCT e.dst AS target, e.src AS caller, e.confidence,
                           n.file, n.line, n.kind
                    FROM edges e LEFT JOIN nodes n ON n.qname = e.src
                    WHERE e.kind IN ('CALLS','IMPORTS') AND e.dst IN ({placeholders})
                    LIMIT ?""",
                (*frontier, limit),
            ).fetchall()
            batch: list[dict] = []
            nxt: list[str] = []
            for r in rows:
                if r["caller"] in visited:
                    continue
                visited.add(r["caller"])
                batch.append({k: r[k] for k in r.keys() if k != "target"})
                nxt.append(r["caller"])
            if not batch:
                break
            label = "d=1 直接调用方（改了会坏）" if level == 1 else \
                    "d=2 间接依赖（很可能受影响）" if level == 2 else \
                    f"d={level} 传递影响（需要跑测试确认）"
            rings[label] = batch
            frontier = nxt
        return rings

    def tests_for(self, symbol: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """哪些测试覆盖了这个符号。"""
        targets = self._match_symbols(symbol)
        if not targets:
            return []
        qnames = [t for t, _ in targets]
        placeholders = ",".join("?" * len(qnames))
        rows = self.conn.execute(
            f"""SELECT DISTINCT e.src AS test, n.file, n.line
                FROM edges e LEFT JOIN nodes n ON n.qname = e.src
                WHERE e.kind='TESTED_BY' AND e.dst IN ({placeholders})
                LIMIT ?""",
            (*qnames, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def find(self, symbol: str, *, limit: int = 30) -> list[dict[str, Any]]:
        """按名字或限定名找符号。"""
        return [{"qname": q, "score": s} for q, s in self._match_symbols(symbol)[:limit]]

    def _match_symbols(self, symbol: str) -> list[tuple[str, float]]:
        """把用户给的符号串匹配到 qname。

        匹配顺序（从严到宽）：
        1. 完全匹配 qname
        2. 后缀匹配 ``Class.method`` / ``mod.func``
        3. 裸名完全匹配
        4. **子串匹配**（新增）：``apply_patch`` 能匹配到 ``_tool_apply_patch``

        第 4 步是必需的：模型自然地会用「工具名」去查，而实际函数带前导下划线
        （``_tool_apply_patch``）。只做前三步的话，最常见的一次查询就会空手而归 ——
        实测踩过：查 ``apply_patch`` 返回「没有这个符号」，模型随即放弃这条路。
        子串匹配的置信度必须更低，并且调用方要把候选名展示出来，让人能核对。
        """
        s = (symbol or "").strip()
        if not s:
            return []
        exact = self.conn.execute("SELECT qname FROM nodes WHERE qname=?", (s,)).fetchall()
        if exact:
            return [(r["qname"], 1.0) for r in exact]
        suffix = self.conn.execute(
            "SELECT qname FROM nodes WHERE qname LIKE ? ORDER BY length(qname) LIMIT 50",
            (f"%.{s}",),
        ).fetchall()
        if suffix:
            return [(r["qname"], 0.9) for r in suffix]
        byname = self.conn.execute(
            "SELECT qname FROM nodes WHERE name=? LIMIT 50", (s,)
        ).fetchall()
        if byname:
            return [(r["qname"], 0.6) for r in byname]
        # 子串兜底：按长度升序，越短越可能是用户想要的那个
        fuzzy = self.conn.execute(
            "SELECT qname FROM nodes WHERE name LIKE ? ORDER BY length(qname) LIMIT 20",
            (f"%{s}%",),
        ).fetchall()
        return [(r["qname"], 0.35) for r in fuzzy]

    def suggest(self, symbol: str, *, limit: int = 5) -> list[str]:
        """查不到符号时给出的候选名。

        单独一个方法而不是塞进 ``_match_symbols``：调用方需要区分
        「精确命中」与「勉强猜的候选」—— 前者可以直接用，后者必须让模型/人核对。
        """
        s = (symbol or "").strip()
        if not s:
            return []
        rows = self.conn.execute(
            "SELECT DISTINCT name FROM nodes WHERE name LIKE ? ORDER BY length(name) LIMIT ?",
            (f"%{s}%", limit),
        ).fetchall()
        return [r["name"] for r in rows]


#: 建图时跳过的目录（与 Workspace 的默认跳过集保持一致）
DEFAULT_SKIP = {
    ".git", ".hg", ".svn", ".margin-graph",
    "node_modules", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".tox", "site-packages",
    ".idea", ".vscode", "target", "vendor",
}
