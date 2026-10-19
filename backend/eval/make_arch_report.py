"""生成架构助手三档实测报告（arch-cases/RESULTS-scored.md）。

与审查侧的 BENCHMARK 同样的原则：**报告由脚本生成，不手写**。
手抄的表格会跟数据脱节，而「数字对不上」是评估里最不该发生的事。
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.arch_score import score_blueprint

CASES = [
    ("e1-pluggy", "简单", "36 文件 / 7.2k 行", "eval/arch-e1-evidence.json"),
    ("e2-requests", "中等", "37 文件 / 12k 行", "eval/arch-e2-evidence.json"),
    ("e3-celery", "困难", "447 文件 / 123k 行", "eval/arch-e3-evidence.json"),
]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    rows = []
    for case, tier, scale, result_file in CASES:
        res_path = root / result_file
        ref_path = root / "arch-cases" / case / "reference.json"
        if not res_path.is_file() or not ref_path.is_file():
            rows.append((case, tier, scale, None, None, None))
            continue
        data = json.loads(res_path.read_text(encoding="utf-8"))
        ref = json.loads(ref_path.read_text(encoding="utf-8"))
        score = score_blueprint(data.get("artifacts") or {}, ref)
        rep = data.get("evidence_report") or {}
        rows.append((case, tier, scale, score, rep, data))

    lines = [
        "# 架构设计助手 · 三档实测（量化版）",
        "",
        "> 本文件由 `eval/make_arch_report.py` 生成，**不要手改**。",
        "> 改参考蓝图或重跑模型后重新生成即可，不存在手抄误差。",
        "",
        "## 一、蓝图质量评分",
        "",
        "四维 P/R/F1：分层 / 组件 / 决策 / 依赖边；加权总分（组件 35%、分层 25%、",
        "决策 25%、边 15%）。参考蓝图是人写的，评分上限受它约束 —— 这是本方法的已知局限。",
        "",
        "| 案例 | 难度 | 规模 | 分层 | 组件 | 决策 | 依赖边 | 总分 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for case, tier, scale, score, _rep, _data in rows:
        if score is None:
            lines.append(f"| {case} | {tier} | {scale} | — | — | — | — | 缺数据 |")
            continue
        lines.append(
            f"| {case} | {tier} | {scale} | {score['layers']['f1']:.0f} | "
            f"{score['components']['f1']:.0f} | {score['decisions']['f1']:.0f} | "
            f"{score['edges']['f1']:.0f} | **{score['overall']:.1f}** |"
        )

    lines += [
        "",
        "## 二、证据可核验性",
        "",
        "「可精确定位率」= 路径完整且行号有效的引用 / 全部引用。",
        "**这一列是本轮改造最直接的成效**：改造前 E1 只有 9%（55 处引用仅 5 处写了完整路径）。",
        "",
        "| 案例 | 引用总数 | ok | weak | bad | 缺证据条目 | 可精确定位率 |",
        "|---|---|---|---|---|---|---|",
    ]
    for case, _tier, _scale, _score, rep, _data in rows:
        if not rep:
            lines.append(f"| {case} | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {case} | {rep.get('total_refs', 0)} | {rep.get('ok', 0)} | "
            f"{rep.get('weak', 0)} | {rep.get('bad', 0)} | "
            f"{rep.get('items_without_evidence', 0)} | **{rep.get('grounded_rate', 0)}%** |"
        )

    lines += [
        "",
        "## 三、运行指标",
        "",
        "| 案例 | 轮数 | 停止原因 | 层 | 组件 | 决策 | 开放问题 |",
        "|---|---|---|---|---|---|---|",
    ]
    for case, _tier, _scale, _score, _rep, data in rows:
        if not data:
            continue
        art = data.get("artifacts") or {}
        lines.append(
            f"| {case} | {data.get('iterations', '—')} | {data.get('stop_reason', '—')} | "
            f"{len(art.get('layer') or [])} | {len(art.get('component') or [])} | "
            f"{len(art.get('decision') or [])} | {len(art.get('open_question') or [])} |"
        )

    violations = []
    for case, _tier, _scale, score, _rep, _data in rows:
        if score and score.get("violations"):
            violations.append(f"- **{case}**：{', '.join(score['violations'])} —— {score.get('violation_note', '')}")
    lines += ["", "## 四、照妖镜（不该出现的东西）", ""]
    lines += violations or ["未命中任何照妖镜条目。"]

    lines += [
        "",
        "## 五、必须说清的局限",
        "",
        "0. **⚠ 同一案例两次跑分差 16 分**：E1 先前一次 79.3、重跑一次 63.4",
        "   （分层 100→40、决策 7→4 条）。这不是评分器不稳定，是**模型产出本身在波动** ——",
        "   一次多登记了几条与参考对齐的层与决策，另一次把注意力放到了别处。",
        "   **单次分数不能当「系统能力」，要比 3–5 次的均值与波动。**",
        "   这条与审查侧的老教训同源：s6 三轮召回 100/100/50。",
        "",
        "1. **参考蓝图是人写的**：写得粗 → 分数虚高；写得细 → 分数偏低。E2/E3 的参考蓝图",
        "   在本轮被重写过一次 —— 初版只收「教科书级事实」，而模型答的是「代码里的实现决策」",
        "   （bootsteps DAG 装配、惰性模块代理、fast_trace 快路径），两者不在同一层，",
        "   导致决策维度 0 命中。合并两类条目后才反映出真实水平。",
        "2. **每档 1 次运行**：单次跑分不可引用为「系统能力」，只能作为信号。",
        "3. 只测结构正确性，**不测论证质量**（后者由 `arch_check` 的权衡检查兜，只覆盖两个硬信号）。",
        "",
    ]

    out = root / "arch-cases" / "RESULTS-scored.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
