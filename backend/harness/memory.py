"""跨 Agent 记忆：让同一个仓库的多次运行互相知道对方发现了什么。

## 为什么需要这个（与「会话记忆」的区别）

先说清一件事：这个模块**不是** mem0 / agentmemory 那类「会话记忆」。
那些记的是「上次对话说了什么」。这里记的是「**这个仓库已知什么**」：

- 之前跑出过哪些缺陷（去重后还剩哪些尚未见修复）
- 之前提过什么计划 / 分层（architect 的产出）
- 这个仓库的指标趋势（迭代数、耗时、缓存命中、按级别的缺陷数）

行业里明确点出这块是空白：「现在的 review 工具没有记忆，每次审查都独立，
你无法看出代码库在变好还是变坏」。趋势比记忆本身更值钱。

## 为什么不用 LLM 压缩摘要

``agentmemory`` / ``hindsight`` 都用 LLM 把观察压成摘要。这里**刻意不做**，三个理由：

1. 我们的产出**本来就是结构化的**（finding 有 severity / file / line / rule_id）。
   把结构化数据压成自然语言再喂回去，是纯粹的信息损失。
2. 摘要一旦错了，后续所有基于它的判断都错，而且**看不出来** —— 这是最坏的失败模式。
3. 每次写记忆都要调模型，成本和不确定性都上升。

所以：**结构化字段直接拼进上下文**，让模型自己读原始数据。
只在「原始事件轨迹」这种非结构化长文本上用**确定性规则**截断（保留工具名与参数，砍掉正文）。

## 去重靠指纹，不靠行号

同一缺陷在两次运行里行号会漂（上面插了几行就变）。用行号去重会把同一条缺陷算成两条。
指纹 = 「文件 + 规则/类别 + 归一化描述 hash」，与行号无关。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# =============================================================================
# 常量
# =============================================================================

#: 注入上下文时的默认 token 预算。取 2000 是行业默认值（agentmemory 也用这个量级）。
#: 再大就会挤压「本次任务」的上下文 —— 记忆是为了辅助判断，不是喧宾夺主。
DEFAULT_BUDGET_TOKENS = 2000

#: 粗估 token：中文按 1 字 ≈ 1 token，英文按 4 字符 ≈ 1 token。
#: 不引入 tokenizer 是刻意的：估错 20% 无所谓，但要能离线算。
def rough_tokens(text: str) -> int:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cjk
    return cjk + max(1, other // 4)


#: 缺陷级别的排序权重（阻断 > 警告 > 提示）
_SEVERITY_ORDER = {"blocker": 0, "warn": 1, "warning": 1, "note": 2, "info": 2}


# =============================================================================
# 数据结构
# =============================================================================


@dataclass(slots=True)
class KnownIssue:
    """一条「已知缺陷」。指纹相同的多次发现会合并成一条。"""

    fingerprint: str
    severity: str
    title: str
    file: str
    line: int
    rule_id: str = ""
    seen_times: int = 1
    first_seen: float = 0.0
    last_seen: float = 0.0
    runs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "severity": self.severity,
            "title": self.title,
            "file": self.file,
            "line": self.line,
            "rule_id": self.rule_id,
            "seen_times": self.seen_times,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


@dataclass(slots=True)
class RunSummary:
    """一次运行的摘要。用于趋势分析。"""

    run_id: str
    agent: str
    created_at: float
    status: str = ""
    iterations: int = 0
    elapsed: float = 0.0
    cache_hit_ratio: float = 0.0
    blocker: int = 0
    warn: int = 0
    note: int = 0
    goal: str = ""
    #: 这次运行是否**可用来算趋势**。False 表示它没真正干活（空转/中止）。
    effective: bool = True
    #: 不可用的原因，纯给人看
    ineffective_reason: str = ""

    @property
    def total_findings(self) -> int:
        return self.blocker + self.warn + self.note


def _judge_effective(status: str, iterations: int, events: int) -> tuple[bool, str]:
    """判断一次运行能不能用来算趋势。

    ## 为什么必须判这个（实测踩到的坑）

    归档里有 4 次运行是 ``status=finished / iterations=1 / findings=0``。
    它们看起来「正常完成、没发现问题」，实际是**根本没干活** ——
    模型第一轮就没调工具（当时用的中转端点不支持 function calling）。

    如果不筛掉这些，趋势会算出「缺陷数从 0 涨到 27」，
    读者会得出「代码库在恶化」的结论 —— 而真相是**前几次根本没在审**。
    这是最坏的一类错误：**用一个假基线算出真结论**。
    """
    if status in ("failed", "error", "cancelled", "stopped"):
        return False, f"状态为 {status}，未正常结束"
    # 只跑 1 轮就结束 = 模型没有进入工具循环（正常审查至少几轮）
    if iterations <= 1:
        return False, f"只跑了 {iterations} 轮就结束，未进入工具调用循环"
    if events <= 4:
        return False, f"只有 {events} 个事件，像是空转"
    return True, ""


@dataclass
class RepoMemory:
    """一个仓库的聚合记忆。"""

    repo: str
    issues: list[KnownIssue] = field(default_factory=list)
    runs: list[RunSummary] = field(default_factory=list)
    artifacts: dict[str, list[dict]] = field(default_factory=dict)  # kind -> 产出
    built_at: float = 0.0

    # ---- 趋势 ----

    @property
    def effective_runs(self) -> list[RunSummary]:
        """可用于算趋势的运行（排除空转/中止的）。"""
        return [r for r in self.runs if r.effective]

    def trend(self) -> dict[str, Any]:
        """指标趋势。**按时间正序**，便于直接画曲线。

        只统计**有效**运行：空转那几次的「0 条发现」不是「仓库干净」，
        拿它当基线会算出完全错误的趋势。
        """
        runs = sorted(self.effective_runs, key=lambda r: r.created_at)
        return {
            "run_count": len(runs),
            "findings": [r.total_findings for r in runs],
            "blocker": [r.blocker for r in runs],
            "iterations": [r.iterations for r in runs],
            "elapsed": [round(r.elapsed, 1) for r in runs],
            "cache_hit": [round(r.cache_hit_ratio, 3) for r in runs],
            "timestamps": [int(r.created_at) for r in runs],
        }

    def describe_trend(self) -> str:
        """把趋势说成人话。少于 2 次**有效**运行就没法谈趋势 —— 要如实说。"""
        runs = sorted(self.effective_runs, key=lambda r: r.created_at)
        skipped = len(self.runs) - len(runs)
        suffix = f"（另有 {skipped} 次运行未计入：它们没进入工具循环或未正常结束）" if skipped else ""
        if len(runs) < 2:
            return (f"这个仓库只有 {len(runs)} 次**有效**运行记录，不足以判断趋势"
                    f"（至少需要 2 次）。{suffix}")
        first, last = runs[0], runs[-1]
        delta = last.total_findings - first.total_findings
        if delta < 0:
            head = f"缺陷数在下降：{first.total_findings} → {last.total_findings}（少 {-delta} 条）"
        elif delta > 0:
            head = f"缺陷数在上升：{first.total_findings} → {last.total_findings}（多 {delta} 条）"
        else:
            head = f"缺陷数持平：{first.total_findings} → {last.total_findings}"
        # 阻断级单独看 —— 它才是真正决定能不能上线的
        b_delta = last.blocker - first.blocker
        tail = ""
        if b_delta:
            tail = f"；其中阻断级 {first.blocker} → {last.blocker}"
        return f"{head}{tail}（共 {len(runs)} 次有效运行）。{suffix}"


# =============================================================================
# 指纹与归一化
# =============================================================================


_WS = re.compile(r"\s+")
_NUM = re.compile(r"\b\d+\b")


def normalize_title(title: str) -> str:
    """归一化描述：去空白、数字替换、大小写统一。

    为什么要换掉数字：同一缺陷的描述里常带具体数值（「超过 100 条」「偏移 3 行」），
    这些数值变了但缺陷没变。不归一化的话指纹会漂，去重就失效。
    """
    t = _WS.sub(" ", (title or "").strip().lower())
    t = _NUM.sub("#", t)
    return t[:400]


def fingerprint(issue: dict[str, Any]) -> str:
    """缺陷指纹：文件 + 类别 + 归一化描述。**不含行号**。

    行号会漂：上面插几行就变。用行号当指纹的一部分，会把同一条缺陷在两次运行里
    算成两条不同的缺陷 —— 那记忆就完全失去了意义。
    """
    file = (issue.get("file") or "").replace("\\", "/").lower()
    cat = (issue.get("rule_id") or issue.get("category") or issue.get("kind") or "").lower()
    norm = normalize_title(str(issue.get("title") or issue.get("summary") or ""))
    raw = f"{file}|{cat}|{norm}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# =============================================================================
# 聚合
# =============================================================================


def _load_archive_files(runs_dir: Path) -> list[dict[str, Any]]:
    """读 ``.runs/*.json``。按「形状」筛选：运行记录必须有 run_id。"""
    out: list[dict[str, Any]] = []
    if not runs_dir.is_dir():
        return out
    for path in sorted(runs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # .runs/ 里不只有运行：workspace.json 也在这里。没有 run_id 的跳过。
        if not isinstance(data, dict) or not data.get("run_id"):
            continue
        out.append(data)
    return out


def _same_repo(record: dict[str, Any], repo: Path) -> bool:
    """判断归档是否属于这个仓库。按 resolved 路径比较，大小写不敏感。"""
    raw = str(record.get("repo") or "").strip()
    if not raw:
        return False
    try:
        return Path(raw).resolve() == repo.resolve()
    except (OSError, ValueError):
        return False


def _count_severities(findings: Iterable[dict[str, Any]]) -> tuple[int, int, int]:
    b = w = n = 0
    for f in findings:
        sev = str(f.get("severity") or "").lower()
        if sev in ("blocker", "high", "critical"):
            b += 1
        elif sev in ("warn", "warning", "medium"):
            w += 1
        else:
            n += 1
    return b, w, n


def aggregate(repo: Path, runs_dir: Path) -> RepoMemory:
    """把一个仓库的所有运行聚合成记忆。"""
    repo = Path(repo).resolve()
    records = [r for r in _load_archive_files(runs_dir) if _same_repo(r, repo)]

    mem = RepoMemory(repo=str(repo), built_at=time.time())
    issues: dict[str, KnownIssue] = {}
    artifacts: dict[str, list[dict]] = {}

    for rec in records:
        result = rec.get("result") or {}
        findings = result.get("findings") or []
        if not isinstance(findings, list):
            findings = []
        b, w, n = _count_severities(findings)
        iters = int(result.get("iterations") or 0)
        status = str(rec.get("status") or "")
        ev_count = int(rec.get("event_count") or len(rec.get("events") or []))
        effective, why = _judge_effective(status, iters, ev_count)
        mem.runs.append(RunSummary(
            run_id=str(rec.get("run_id")),
            agent=str(rec.get("agent") or ""),
            created_at=float(rec.get("created_at") or 0),
            status=status,
            iterations=iters,
            elapsed=float(rec.get("elapsed") or result.get("elapsed") or 0),
            cache_hit_ratio=float(result.get("cache_hit_ratio") or 0),
            blocker=b, warn=w, note=n,
            goal=str(rec.get("goal") or "")[:200],
            effective=effective,
            ineffective_reason=why,
        ))
        for f in findings:
            if not isinstance(f, dict):
                continue
            fp = fingerprint(f)
            if fp in issues:
                known = issues[fp]
                known.seen_times += 1
                known.last_seen = max(known.last_seen, float(rec.get("created_at") or 0))
                known.runs.append(str(rec.get("run_id")))
                # 行号取最新的（更接近当前代码）
                if float(rec.get("created_at") or 0) >= known.last_seen:
                    known.line = int(f.get("line") or known.line or 0)
            else:
                issues[fp] = KnownIssue(
                    fingerprint=fp,
                    severity=str(f.get("severity") or "note").lower(),
                    title=str(f.get("title") or f.get("summary") or "")[:300],
                    file=str(f.get("file") or ""),
                    line=int(f.get("line") or 0),
                    rule_id=str(f.get("rule_id") or f.get("category") or ""),
                    first_seen=float(rec.get("created_at") or 0),
                    last_seen=float(rec.get("created_at") or 0),
                    runs=[str(rec.get("run_id"))],
                )

        # 非 finding 类产出（blueprint 的分层/组件/决策）也按仓库聚起来
        for kind, items in (result.get("artifacts") or {}).items():
            if kind == "finding" or not isinstance(items, list):
                continue
            bucket = artifacts.setdefault(kind, [])
            if len(bucket) < 200:
                bucket.extend(i for i in items[:20] if isinstance(i, dict))

    mem.issues = sorted(
        issues.values(),
        key=lambda x: (_SEVERITY_ORDER.get(x.severity, 9), -x.seen_times, x.file, x.line),
    )
    mem.artifacts = artifacts
    return mem


# =============================================================================
# 上下文生成（给 Agent 注入）
# =============================================================================


def brief(mem: RepoMemory, *, agent: str = "", budget_tokens: int = DEFAULT_BUDGET_TOKENS) -> str:
    """生成注入用的上下文片段。

    ## 三条纪律

    **1. 必须写明「这是历史，可能过期」。**
       不标的话模型会把历史发现当成当前事实，直接复述给用户 ——
       那是把过期信息当结论，比没有记忆更糟。

    **2. 必须给预算并在超限时明确截断。**
       记忆不能挤压本次任务的上下文。

    **3. 跨 agent 才有价值。**
       同一 agent 自己的历史对它的增量最小（它本来就做了类似的事）。
       这里**优先展示其他 agent 的产出**，因为那是它的盲区。
    """
    if not mem.issues and not mem.runs:
        return ""

    lines: list[str] = [
        "## 这个仓库的历史（自动汇总，**可能已过期**）",
        f"以下内容来自 {len(mem.runs)} 次历史运行。它是**过去某次运行时的判断**，"
        "代码可能已经改了；把它当作「值得复核的线索」，不要当作当前事实。",
        "",
    ]

    # ---- 趋势 ----
    lines.append("### 趋势")
    lines.append(mem.describe_trend())

    # ---- 其他 agent 的产出（盲区信息）----
    other_runs = [r for r in mem.runs if r.agent and r.agent != agent]
    if other_runs:
        agents = sorted({r.agent for r in other_runs})
        lines.append("")
        lines.append(f"### 其他助手在这个仓库上的记录（{('、'.join(agents))}）")
        for ag in agents:
            subset = [r for r in other_runs if r.agent == ag]
            latest = max(subset, key=lambda r: r.created_at)
            desc = f"- `{ag}`：{len(subset)} 次运行"
            if latest.goal:
                desc += f"，最近一次目标是「{latest.goal}」"
            if latest.total_findings:
                desc += f"，产出 {latest.total_findings} 条发现"
            lines.append(desc)
        for kind, items in mem.artifacts.items():
            if items:
                sample = "、".join(str(i.get("name") or i.get("title") or "?")[:24]
                                  for i in items[:6])
                lines.append(f"- 已有 {kind} 产出 {len(items)} 项：{sample}")

    # ---- 已知缺陷（按级别）----
    if mem.issues:
        repeated = [i for i in mem.issues if i.seen_times > 1]
        lines.append("")
        head = f"### 历史发现（去重后 {len(mem.issues)} 条"
        if repeated:
            head += f"，其中 {len(repeated)} 条在多次运行里重复出现"
        lines.append(head + "）")

        if repeated:
            lines.append("**反复出现的**（说明之前没被修掉，优先核对）：")
            for i in repeated[:8]:
                lines.append(f"- [{i.severity}] {i.file}:{i.line} {i.title}（出现 {i.seen_times} 次）")

        fresh = [i for i in mem.issues if i.seen_times == 1]
        if fresh:
            lines.append("")
            lines.append("近期单次出现的：")
            for i in fresh[:10]:
                lines.append(f"- [{i.severity}] {i.file}:{i.line} {i.title}")

    text = "\n".join(lines)

    # ---- 预算控制：明确截断，不静默裁剪 ----
    if rough_tokens(text) > budget_tokens:
        # 按行累积到预算，再补一句说明
        acc: list[str] = []
        used = 0
        for line in text.split("\n"):
            cost = rough_tokens(line) + 1
            if used + cost > budget_tokens - 40:
                break
            acc.append(line)
            used += cost
        dropped = len(text.split("\n")) - len(acc)
        acc.append(f"\n[... 历史摘要超出 {budget_tokens} token 预算，省略 {dropped} 行。"
                   "需要更完整的历史时，让用户显式要求查看。]")
        text = "\n".join(acc)

    return text
