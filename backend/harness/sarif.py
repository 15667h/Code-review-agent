"""SARIF 2.1.0 输出：让结果能落在 GitHub 的代码扫描界面上。

## 为什么必须做 SARIF

GitHub 原生识别 SARIF（Static Analysis Results Interchange Format），
上传后能把发现**直接标在 PR 的文件行上**，而不是只在评论里贴一段文字。

这是「独立验证层要跑在 PR 上」的最后一公里：
没有 SARIF，审查结果只能进评论；有了 SARIF，它进了 GitHub 的 Security 标签页，
可筛选、可追踪、可标记误报，并且能在合并前作为检查项。

## 三条实现纪律

**1. 只输出合法的 SARIF。**
GitHub 的上传接口对 schema 很严格：一个字段类型不对就整份 reject，
而且错误信息通常在服务端，本地看不到。所以这里宁可少给字段，也不猜。

**2. ``partialFingerprints`` 必须稳定。**
GitHub 用它判断「这是不是上次那个问题」，从而支持「已忽略」「已修复」状态。
如果指纹随运行变化，每个 PR 都会出现一堆「新问题」，开发者会立刻静音它。

**3. 严重级别映射必须如实。**
SARIF 只有 error/warning/note 三档。我们的 blocker → error 是有依据的映射，
但**不要**为了「看起来严重」把 warn 也映射成 error —— 那样会稀释真正该拦的项。

## 已知边界

- SARIF 只能定位到**行**，不能定位到列。我们的 Finding 也只有行号，正好一致。
- 不支持跨文件流路径（code flow）—— 那需要把数据流建模进 Finding，目前没有。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .types import Finding

#: SARIF 版本与 schema。GitHub 目前认 2.1.0。
SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

#: 我们只实现一个 tool driver。多了没意义 —— 所有发现都来自同一个系统。
_TOOL_NAME = "Margin"

#: 我们的严重级别 → SARIF level。
#:
#: 刻意**不**把 warn 提成 error：GitHub 会把 error 显示为红色并可以让 check 失败，
#: 一旦 warn 也标红，真正的阻断项就被稀释了，开发者会对整片红色脱敏。
_SEVERITY_TO_LEVEL = {
    "blocker": "error",
    "warn": "warning",
    "note": "note",
}

#: SARIF level 的默认 security-severity（GitHub 用它排序）。
#: 这个数值区间的约定来自 GitHub 文档：9.0+ 严重、7.0-8.9 高、4.0-6.9 中、0.1-3.9 低。
_SECURITY_SEVERITY = {
    "blocker": "9.0",
    "warn": "5.5",
    "note": "1.5",
}


def _normalize_path(path: str) -> str:
    """SARIF 要求相对 URI，且分隔符必须是 ``/``。

    Windows 下工作区路径是反斜杠，直接输出会让 GitHub 找不到文件，
    表现为「结果上传成功但一个都没显示」—— 很难排查的一类失败。
    """
    p = str(path).replace("\\", "/")
    # 去掉前导 ./ 与绝对路径前缀（如果有）
    while p.startswith("./"):
        p = p[2:]
    return p


def _fingerprint(finding: Finding) -> str:
    """给 GitHub 用的稳定指纹。

    与 ``Finding.fingerprint``（文件:行号:标题）**不同**：那份带行号，
    而 GitHub 的「已忽略」状态需要在行号漂移后仍然对应得上。

    这里用「文件 + 标题归一化」，去掉行号 —— 同一个问题往上挪了几行，
    仍然算同一个问题，之前标记的「误报」不会失效。
    """
    title = " ".join(str(finding.title or "").lower().split())
    raw = f"{_normalize_path(finding.file)}|{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _rule_descriptor(rule_id: str, severity: str, title: str = "") -> dict[str, Any]:
    """构造一条 rule 描述。

    SARIF 要求 ruleId 出现在 rules 数组里，result 里才能引用它；
    否则 GitHub 会显示成「未知规则」，且默认级别会丢。
    """
    return {
        "id": rule_id,
        "name": rule_id,
        "shortDescription": {"text": title or rule_id},
        "defaultConfiguration": {"level": _SEVERITY_TO_LEVEL.get(severity, "warning")},
        "properties": {
            "security-severity": _SECURITY_SEVERITY.get(severity, "5.5"),
            "tags": ["security" if severity == "blocker" else "maintainability"],
        },
    }


def _result(finding: Finding, rule_ids: set[str]) -> dict[str, Any]:
    """把一条发现转成 SARIF result。

    ``ruleId`` 必须落在已声明的 rules 里；否则这里退化成兜底规则 ——
    **不能出现「引用了未声明的规则」**（GitHub 会把它显示成未知规则并丢掉级别）。

    取值优先级：
    ① ``finding.rule_id``（规则引擎给的，如 ``SEC001``）—— 最准确；
    ② ``finding.id``（模型产出时是 ``F_xxx``，仅当它恰好等于某个规则 id 时用）；
    ③ 兜底 ``MARGIN001``（模型自行发现、不对应任何内置规则）。
    """
    rule_id = ""
    if finding.rule_id and finding.rule_id in rule_ids:
        rule_id = finding.rule_id
    elif finding.id and finding.id in rule_ids:
        rule_id = finding.id
    if not rule_id:
        rule_id = "MARGIN001"

    line = max(1, int(finding.line or 1))
    message = finding.title or "（无标题）"
    if finding.detail:
        message = f"{message}\n\n{finding.detail}"
    if finding.evidence:
        message = f"{message}\n\n证据：{finding.evidence}"
    if finding.suggestion:
        message = f"{message}\n\n建议：{finding.suggestion}"

    return {
        "ruleId": rule_id,
        "level": _SEVERITY_TO_LEVEL.get(str(finding.severity), "warning"),
        "message": {"text": message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {
                        "uri": _normalize_path(finding.file),
                        "uriBaseId": "%SRCROOT%",
                    },
                    # endLine 与 startLine 相同：我们只有单行定位信息，
                    # 给一个假的区间会让 GitHub 高亮错范围。
                    "region": {"startLine": line, "endLine": line},
                }
            }
        ],
        "partialFingerprints": {"marginV1": _fingerprint(finding)},
    }


def build_sarif(
    findings: list[Finding],
    *,
    repo: str = "",
    rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """把发现列表转成一份完整的 SARIF 文档。

    ``rules`` 可选：传入规则元数据（id / severity / title）后，SARIF 里会有
    完整的规则描述，GitHub 界面能显示规则名与说明。不传时只保证 result 合法。
    """
    rule_index: dict[str, dict[str, Any]] = {}
    for rule in rules or []:
        rid = str(rule.get("id") or "")
        if not rid:
            continue
        rule_index[rid] = _rule_descriptor(
            rid, str(rule.get("severity") or "warn"), str(rule.get("title") or "")
        )

    # 兜底规则：发现没有对应规则描述时用它，保证 ruleId 一定能解析
    if "MARGIN001" not in rule_index:
        rule_index["MARGIN001"] = _rule_descriptor(
            "MARGIN001", "warn", "Margin 审查发现（未分类）"
        )

    declared = set(rule_index)
    results = [_result(f, declared) for f in findings]

    # 只保留**实际被引用**的规则，避免 rules 里塞一堆没用到的项
    used = {r["ruleId"] for r in results}
    rules_out = [rule_index[rid] for rid in sorted(rule_index) if rid in used or rid == "MARGIN001"]

    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": _TOOL_NAME,
                "informationUri": "https://github.com/15667h/Code-review-agent",
                "rules": rules_out,
            }
        },
        "results": results,
        "columnKind": "utf16CodeUnits",
    }
    if repo:
        run["originalUriBaseIds"] = {"%SRCROOT%": {"uri": f"file://{_normalize_path(repo)}/"}}

    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [run],
    }


def write_sarif(
    findings: list[Finding],
    path: Path | str,
    *,
    repo: str = "",
    rules: list[dict[str, Any]] | None = None,
) -> Path:
    """写 SARIF 文件。返回实际写入路径。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = build_sarif(findings, repo=repo, rules=rules)
    target.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def rule_metadata() -> list[dict[str, Any]]:
    """从内置规则表导出元数据，供 SARIF 使用。

    单独一个函数而不是在 ``build_sarif`` 里 import ``rules``：
    这个模块不该依赖规则集，否则规则集一变就要重新审视 SARIF 输出。
    """
    from .rules import RULES

    return [
        {"id": r.id, "severity": r.severity, "title": r.title}
        for r in RULES
    ]


def summarize(findings: list[Finding]) -> str:
    """给 CI 日志用的一行摘要。

    刻意不给「通过/失败」的结论 —— 那是退出码的事。
    这里只报数量，让人一眼看出「这次扫出了什么规模的东西」。
    """
    if not findings:
        return "没有发现。"
    counts = {"blocker": 0, "warn": 0, "note": 0}
    for f in findings:
        counts[str(f.severity)] = counts.get(str(f.severity), 0) + 1
    return (
        f"共 {len(findings)} 条发现"
        f"（阻断 {counts['blocker']} / 警告 {counts['warn']} / 提示 {counts['note']}）"
    )


__all__ = [
    "SARIF_SCHEMA", "SARIF_VERSION", "build_sarif", "rule_metadata",
    "summarize", "write_sarif",
]
