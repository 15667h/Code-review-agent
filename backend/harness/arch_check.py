"""架构产出的证据核验。

## 为什么单独一个模块

审查那边的 `cross_check_line_content` 判的是「引用的行是否在支撑一条**缺陷描述**」——
它靠缺陷关键词族（shell / 加密 / 资源泄漏…）与代码行比对。
架构产出的性质不同：它说的是「这一个层 / 组件存在于这里」，
关键词族对不上（层名叫「领域层」，代码里当然不会有「领域层」两个字）。

所以这里换判据，但复用同一个思路：**能确定性判定的绝不交给模型**。

三条确定性检查，按可信度从高到低：

1. **文件存在**（硬失败）：路径写错、或仓库里根本没有这个文件。
2. **行号在范围内**（硬失败）：行号越界 = 编造或过期。
3. **那一行属于代码而不是注释/空行**（软提示）：架构证据指向的是「结构」，
   指向空行或纯注释通常说明定位不准。

第 3 条刻意只作提示不判失败：有些架构事实确实由注释声明（例如文件头的
模块职责说明），硬判会产生假阳性 —— 这与审查那边判官层的教训一致。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# `path/to/file.py:12` 或 `path/to/file.py:12-20`
_LOCATION = re.compile(r"^(?P<file>[^\s:]+\.\w+):(?P<line>\d+)(?:-(?P<end>\d+))?$")

def parse_location(raw: str) -> tuple[str, int, int] | None:
    """解析 ``相对路径:行号`` 形式的证据。失败返回 None。"""
    text = (raw or "").strip().strip("`\"'")
    match = _LOCATION.match(text)
    if not match:
        return None
    start = int(match.group("line"))
    end = int(match.group("end") or start)
    return match.group("file"), start, end
def check_evidence(
    raw: str,
    workspace_root: Path,
    *,
    unique_basenames: set[str] | None = None,
) -> tuple[str, str]:
    """核验一条证据。

    返回 ``(状态, 说明)``，状态取值：
    - ``ok``：路径与行号都对得上
    - ``weak``：能定位但不够精确（只有文件名 / 指向空行或纯注释）—— 提示但不作废
    - ``bad``：文件不存在或行号越界 —— 这条产出没有依据
    """
    parsed = parse_location(raw)
    if not parsed:
        return "bad", f"格式不是 `路径:行号`：{raw}"

    rel, start, end = parsed
    root = Path(workspace_root)
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return "bad", f"路径逃出工作区：{rel}"

    if not target.is_file():
        # 同名唯一时降级为 weak 而不是 bad：路径写短了但目标能确定。
        name = Path(rel).name
        if unique_basenames and name in unique_basenames:
            return "weak", f"只写了文件名 `{rel}`，无法直接定位（需补全路径）"
        return "bad", f"文件不存在：{rel}"

    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return "weak", f"读取失败，跳过（{exc}）"

    if start > len(lines) or end > len(lines) or start < 1:
        return "bad", f"行号越界：{rel}:{start}（该文件只有 {len(lines)} 行）"

    window = [ln.strip() for ln in lines[start - 1 : end] if ln.strip()]
    if not window:
        return "weak", f"证据指向空行：{rel}:{start}"
    if all(ln.startswith("#") for ln in window):
        return "weak", f"证据只指向注释：{rel}:{start}"

    return "ok", ""

def verify_blueprint(
    artifacts: dict[str, list[Any]],
    workspace_root: Path,
) -> dict[str, Any]:
    """核验一份蓝图的全部证据，返回报告。

    报告结构刻意做成「可复算的分数」而不是一段话：
    只有能算出比例，改进才可衡量（业界架构恢复研究都用 P/R/F1，理由相同）。
    """
    root = Path(workspace_root)
    # 同名唯一性表：用来区分「路径写短了」与「文件根本不存在」
    basenames: dict[str, int] = {}
    for item in root.rglob("*"):
        if item.is_file():
            basenames[item.name] = basenames.get(item.name, 0) + 1
    unique = {name for name, count in basenames.items() if count == 1}

    per_kind: dict[str, dict[str, int]] = {}
    problems: list[dict[str, str]] = []
    total = ok = weak = bad = missing = 0

    for kind in ("layer", "component", "decision", "open_question"):
        stats = {"total": 0, "ok": 0, "weak": 0, "bad": 0, "no_evidence": 0}
        for item in artifacts.get(kind) or []:
            data = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            refs = data.get("evidence") or []
            if isinstance(refs, str):
                refs = [refs]
            if not refs:
                # 「没给证据」与「证据是错的」是两回事：前者是遗漏，后者是编造。
                stats["no_evidence"] += 1
                missing += 1
                continue
            for ref in refs:
                stats["total"] += 1
                total += 1
                status, why = check_evidence(str(ref), root, unique_basenames=unique)
                stats[status] += 1
                if status == "ok":
                    ok += 1
                elif status == "weak":
                    weak += 1
                else:
                    bad += 1
                if status != "ok":
                    problems.append(
                        {
                            "kind": kind,
                            "name": str(data.get("name") or data.get("title") or data.get("question") or "")[:40],
                            "evidence": str(ref),
                            "status": status,
                            "why": why,
                        }
                    )
        per_kind[kind] = stats

    return {
        "total_refs": total,
        "ok": ok,
        "weak": weak,
        "bad": bad,
        "items_without_evidence": missing,
        "grounded_rate": round(ok / total * 100, 1) if total else 0.0,
        "per_kind": per_kind,
        "problems": problems,
    }

def summary_lines(report: dict[str, Any]) -> list[str]:
    """把报告转成给模型看的警告 —— 语气要具体，指出是哪一条哪一处。"""
    lines: list[str] = []
    if report["items_without_evidence"]:
        lines.append(
            f"{report['items_without_evidence']} 条产出没有登记 evidence（层/组件/决策/开放问题"
            "都应有代码依据，否则评审无法核实它是否真实存在）。"
        )
    if report["bad"]:
        sample = "；".join(
            f"{p['kind']}/{p['name']} `{p['evidence']}` —— {p['why']}"
            for p in report["problems"]
            if p["status"] == "bad"
        )[:400]
        lines.append(f"{report['bad']} 处证据无法核实（文件不存在或行号越界）：{sample}")
    if report["weak"]:
        lines.append(
            f"{report['weak']} 处证据不够精确（只有文件名，或指向空行/注释），建议补全完整路径。"
        )
    if report["total_refs"] and not (report["bad"] or report["items_without_evidence"]):
        lines.append(
            f"证据核验：{report['ok']}/{report['total_refs']} 处可精确核实（{report['grounded_rate']}%）。"
        )
    return lines

# --------------------------------------------------------------------------- #
# 权衡真实性核验（P1）
# --------------------------------------------------------------------------- #
#
# 业界最明确的一条警告：模型能为几乎任何选择写出流畅的理由，把「单纯的偏好」
# 包装成「严谨的权衡」——表面通过了「靠力量而非偏好」的标准，底下其实没通过。
# 这个问题几乎没人给解法，但它是可以用确定性规则逼近的：
#
# 1. 稻草人检测：`alternatives` 里每条都必须写出**被放弃方案自己的一个优点**。
#    只有缺点没有优点的备选，不是真的考虑过，是为了衬托而摆的。
# 2. 代价检测：`consequences` 必须出现负面词（成本/风险/复杂度/延迟/迁移…）。
#    只有好处没有代价的选择，通常不是选择，而是还没想清楚。

# 「代价」应当出现的负面信号
# 覆盖「代价」的常见表达方式。
#
# 这一版是被真实产出打脸之后重写的：初版只给了「成本/风险/复杂度」等名词，
# 结果在 E2 上把 4 条**写得很好的**决策全判成了「只写好处」。原文长这样：
#   「依赖图里是假边：只在类型检查期存在，运行时不存在」
#   「每次跳跃都是一个完整往返，意味着没有跨跳跃的连接复用优化」
#   「必须穷举 urllib3 各版本的异常类型」「多线程共享同一 Session 时无锁保护」
# 这些代价一个都没写「成本」二字，但都是实实在在的代价。
#
# 结论：判「有没有写代价」不能只查名词，要看**有没有实质内容 + 有没有转折/让步**。
_COST_CUES = (
    "成本", "代价", "风险", "复杂度", "复杂", "延迟", "变慢", "性能下降",
    "迁移", "不兼容", "限制", "牺牲", "放弃", "运维负担", "人力",
    "必须", "需要", "不一致", "监控", "额外", "增", "多一", "难",
    # 真实产出里高频出现、但初版漏掉的表达
    "假边", "只在", "运行时不存在", "没有", "无法", "不能", "丢失", "丢掉",
    "穷举", "无锁", "驻留", "拆成", "散落", "回退", "失效", "违反", "边界",
    "意味着", "但", "然而", "代价是", "只能", "仅",
    "trade-off", "tradeoff", "downside", "cost", "risk", "debt", "slow",
)

# 备选方案的「优点」信号。
#
# 同样被真实产出修正过：模型写的备选是这样的——
#   「把 Response 构建下沉到独立模块：更干净，但…」
#   「启用 urllib3 自带重定向：少一层状态机，但…」
#   「用 importlib.metadata 按需探测：无汇点依赖，但…」
# 前半句就是它自己的优点。所以除了「简单/成熟」这类形容，还要认
# 「更干净 / 少一层 / 无…依赖 / 设计更」这类**比较级与减负表达**。
_MERIT_CUES = (
    "简单", "直接", "成熟", "稳定", "快", "轻", "少", "省", "易", "明确",
    "已有", "现成", "内置", "标准", "生态", "性能好", "更合适", "优势",
    # 比较级 / 减负表达（真实产出里最常见的形式）
    "更干净", "更清", "更简单", "更明确", "更合理", "更强", "更小",
    "少一层", "无汇点", "无需", "不用", "免去", "省掉", "降低", "减少",
    "下沉", "集中", "统一", "隔离", "解耦", "边界更清",
    "simple", "mature", "fast", "cheap", "built-in", "standard",
)

# 转折词：备选里出现它，说明「优点 + 但是缺点」的结构在，这是认真考虑过的标志
_CONTRAST_CUES = ("但", "不过", "然而", "只是", "代价", "会丢", "无法", "因为", "；", ";")

# 只有好处没有代价的判断：既要**没有负面词**，也要**没有转折结构**，
# 且整段短到不像认真写过（真正的代价往往需要一两句说明）。
_MIN_CONSEQUENCE_LEN = 12

def _has_any(text: str, cues: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(cue in low for cue in cues)

def check_decision_quality(decision: dict) -> list[str]:
    """检查一条决策是否「真的在权衡」，返回问题列表（空表示通过）。

    只覆盖两个最硬的信号，不做语义判断 —— 判别「这是好决策吗」需要人，
    但判别「它有没有写出代价」可以机械做。
    """
    problems: list[str] = []
    title = str(decision.get("title") or "")[:40]

    consequences = str(decision.get("consequences") or "").strip()
    if not consequences:
        problems.append(f"决策「{title}」没有写 consequences（代价与后续影响）")
    elif (
        not _has_any(consequences, _COST_CUES)
        and not _has_any(consequences, _CONTRAST_CUES)
        and len(consequences) < _MIN_CONSEQUENCE_LEN
    ):
        # 三个条件同时成立才算「只写好处」：没有负面词、没有转折结构、还很短。
        # 只凭「没有负面词」就判，会把大量真实代价误杀（E2 上误杀 4/7 条）。
        problems.append(
            f"决策「{title}」的 consequences 只有一句好话、没有代价"
            f"—— 只有好处没有代价，通常不是选择，而是还没想清楚"
        )

    alternatives = decision.get("alternatives") or []
    if isinstance(alternatives, str):
        alternatives = [alternatives]
    if not alternatives:
        problems.append(f"决策「{title}」没有登记任何被放弃的备选 —— 评审要讨论的正是那条路")
    else:
        # 判据是**整条决策里至少有一个备选写出了它自己的优点**，不是每条都要。
        #
        # 这条也是被真实产出修正的：初版逐条判，于是「回滚方案：需要设计幂等撤销」
        # 被判成稻草人 —— 但同一条决策的另一条备选明明写了「实现简单、一遍扫描」。
        # 逐条判会把「认真考虑过多个方案、但其中一个确实没好话可写」误判成敷衍。
        #
        # 真正的稻草人特征是**所有备选都只有坏处**，那才说明没真考虑过。
        with_merit = []
        without = []
        for alt in alternatives:
            text = str(alt)
            if _has_any(text, _MERIT_CUES) or _has_any(text, _CONTRAST_CUES):
                with_merit.append(text)
            else:
                without.append(text)

        if not with_merit:
            problems.append(
                f"决策「{title}」的 {len(alternatives)} 条备选全都没写自己的优点"
                f"（例如「{without[0][:36]}」）—— 只有缺点的备选不是真的考虑过，"
                "是为了衬托而摆的"
            )
    return problems

def check_blueprint_quality(artifacts: dict[str, list[Any]]) -> list[str]:
    """批量检查决策质量，返回问题清单。"""
    problems: list[str] = []
    for item in artifacts.get("decision") or []:
        data = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        problems.extend(check_decision_quality(data))
    return problems
