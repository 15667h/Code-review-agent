"""靶场基线：对每个难度样本跑真实模型，把结果与人工标注对齐并完整留档。

与 run_eval 的分工：
- run_eval 关心**统计显著性**（Pass@k / Pass^k / 配对检验），口径固定；
- run_bench 关心**单个样本的可读证据**：模型到底说了什么、漏在哪、多报在哪，
  以及指标对「行号容差」有多敏感。

为什么要算容差敏感性：代码审查里的缺陷常常横跨多条语句（读-判-写三步的竞态、
跨行的表达式），标注落在哪一行、模型指向哪一行，都能差出 3~4 行。只报一个
容差值下的数字，等于把「位置口径的差异」伪装成「能力的差异」。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.dataset import DEFAULT_GOAL, load_case, match_findings  # noqa: E402
from harness.config import load_config  # noqa: E402
from harness.loop import resolve_workspace, run_audit  # noqa: E402
from harness.model import OpenAIModel  # noqa: E402
from harness.rules import scan_tree, to_findings  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

# 报告用的容差档位。0 是「必须落在同一行」，2 是默认口径，8 已接近「同一函数内」。
TOLERANCES = (0, 1, 2, 3, 4, 6, 8)

# (样本, 难度, 提问方式, 审计目标)
SAMPLES = [
    ("s1-obvious-danger", "L1 基础", "标准口径", DEFAULT_GOAL),
    (
        "s2-resources-errors",
        "L2 基础",
        "标准口径",
        DEFAULT_GOAL,
    ),
    (
        "s3-input-paths",
        "L3 进阶",
        "完整背景（给出关注方向）",
        "审查这个仓库。用户可控的数据在进入文件系统、反序列化和外部请求之前，"
        "有没有做必要的校验与限制？请逐条给出结论。",
    ),
    (
        "s4-concurrency",
        "L4 进阶",
        "诊断型（先推根因）",
        "先说明这些函数在什么样的线程交错顺序下会出错，再逐个确认实际实现是否存在该问题。",
    ),
    (
        "s5-boundaries",
        "L5 高级",
        "诊断型（先立前提）",
        "先推导每个函数对调用方隐含的前提（区间开闭、页码基准、单位与取值范围），"
        "再判断实现有没有落实这些前提。",
    ),
    (
        "s6-authz-logic",
        "L6 高级",
        "诊断型（先建模型）",
        "先建立这个服务的权限模型与订单状态机，再逐一判断实现有没有落实它。"
        "重点是：谁能看到什么、哪些状态转换是允许的。",
    ),
    (
        "s7-crossfile-flow",
        "L7 专家",
        "定位型（只给位置，不给缺陷描述）",
        "审查 app/repository.py 里的 query 与 export 两个方法。",
    ),
    (
        "s8-crypto-protocol",
        "L8 专家",
        "诊断型（先立安全目标）",
        "先说明这套令牌方案应当达成的安全目标（不可预测、不可伪造、不可重放、机密性），"
        "再逐项判断实现是否达成。",
    ),
]

SAMPLE_MAP = {row[0]: row for row in SAMPLES}


def dump_finding(item) -> dict:
    """把一条预测完整落盘 —— 交付文档要引用模型的原话，所以不能只存行号。"""
    return {
        "file": getattr(item, "file", ""),
        "line": getattr(item, "line", 0),
        "severity": getattr(item, "severity", ""),
        "title": getattr(item, "title", ""),
        "detail": (getattr(item, "detail", "") or "")[:400],
        "evidence": (getattr(item, "evidence", "") or "")[:300],
    }


def tolerance_curve(predicted: list, expected: list) -> dict:
    """同一批预测在多个容差档位下的指标 —— 用来看结论对位置口径有多敏感。"""
    curve = {}
    for tol in TOLERANCES:
        outcome = match_findings(predicted, expected, line_tolerance=tol)
        curve[str(tol)] = {
            "precision": round(outcome.precision, 3),
            "recall": round(outcome.recall, 3),
            "f1": round(outcome.f1, 3),
            "spurious": len(outcome.spurious),
            "missed": len(outcome.missed),
            "duplicates": len(outcome.duplicates),
        }
    return curve


def classify_spurious(spurious: list, expected: list) -> list[dict]:
    """给每条「误报」量出它离最近的标注有多远。

    这是判断「真误报」还是「位置口径分歧」的唯一客观依据：
    距离 1~4 行且指向同一处代码，基本可以断定是同一个缺陷；
    距离十几行或指向别的文件，才是真的多报。
    """
    rows = []
    for item in spurious:
        item_file = getattr(item, "file", "")
        item_line = getattr(item, "line", 0)
        best = None
        for label in expected:
            if label.file != item_file:
                continue
            distance = abs(label.line - item_line)
            if best is None or distance < best[0]:
                best = (distance, label)
        rows.append(
            {
                "probe": dump_finding(item),
                "nearest_label": (
                    None
                    if best is None
                    else {"code": best[1].code, "file": best[1].file, "line": best[1].line}
                ),
                "distance": None if best is None else best[0],
                "in_same_file": best is not None,
            }
        )
    return rows


def classify_missed(missed: list, predicted: list) -> list[dict]:
    """漏报也量一次：如果是「模型报了同一处但行号差几行」，那口径问题而非能力问题。"""
    rows = []
    for label in missed:
        best = None
        for item in predicted:
            if getattr(item, "file", "") != label.file:
                continue
            distance = abs(getattr(item, "line", 0) - label.line)
            if best is None or distance < best[0]:
                best = (distance, item)
        rows.append(
            {
                "code": label.code,
                "file": label.file,
                "line": label.line,
                "description": label.description,
                "nearest_prediction": None if best is None else dump_finding(best[1]),
                "distance": None if best is None else best[0],
            }
        )
    return rows


def run_one(name: str, goal: str, config, repeats: int) -> dict:
    case_dir = EXAMPLES / name
    case = load_case(name, case_dir, goal)
    # 审计根是 case.repo（样本目录下的 repo/），标注在其外面 ——
    # 直接审 case_dir 会把 labels.json 一起送进被审范围，等于泄题。
    audit_root = case.repo

    rule_predicted = to_findings(scan_tree(audit_root))
    rule_match = match_findings(rule_predicted, case.expected)

    runs = []
    for index in range(repeats):
        model = OpenAIModel(config)
        started = time.time()
        try:
            result = run_audit(
                goal=goal,
                workspace=resolve_workspace(audit_root),
                model=model,
                config=config,
            )
        finally:
            close = getattr(model, "close", None)
            if callable(close):
                close()

        match = match_findings(result.findings, case.expected)
        runs.append(
            {
                "run": index + 1,
                "elapsed": round(time.time() - started, 1),
                "iterations": result.iterations,
                "stop_reason": result.stop_reason,
                "summary": (result.summary or "")[:600],
                "warnings": [w[:200] for w in (result.warnings or [])],
                "usage": {
                    "prompt": result.usage.prompt_tokens,
                    "completion": result.usage.completion_tokens,
                    "cached": result.usage.cached_tokens,
                },
                "cache_hit": (
                    round(result.usage.cached_tokens / result.usage.prompt_tokens, 3)
                    if result.usage.prompt_tokens
                    else 0.0
                ),
                "findings": [dump_finding(item) for item in result.findings],
                "matched": len(match.matched),
                "missed": [f"{i.code} {i.file}:{i.line}" for i in match.missed],
                "missed_detail": classify_missed(match.missed, result.findings),
                "spurious": [dump_finding(i) for i in match.spurious],
                "spurious_detail": classify_spurious(match.spurious, case.expected),
                "duplicates": [dump_finding(i) for i in match.duplicates],
                "precision": round(match.precision, 3),
                "recall": round(match.recall, 3),
                "f1": round(match.f1, 3),
                "tolerance_curve": tolerance_curve(result.findings, case.expected),
            }
        )
        print(
            f"    run {index + 1}/{repeats}  R={runs[-1]['recall']:.0%} "
            f"P={runs[-1]['precision']:.0%} F1={runs[-1]['f1']:.0%}  "
            f"{runs[-1]['iterations']}轮 {runs[-1]['elapsed']}s "
            f"缓存{runs[-1]['cache_hit']:.0%}  发现{len(runs[-1]['findings'])}条",
            flush=True,
        )

    return {
        "sample": name,
        "tier": SAMPLE_MAP[name][1],
        "style": SAMPLE_MAP[name][2],
        "goal": goal,
        "audit_root": str(audit_root.relative_to(EXAMPLES.parent)),
        "labels": [
            {"code": lb.code, "file": lb.file, "line": lb.line, "description": lb.description}
            for lb in sorted(case.expected, key=lambda x: (x.file, x.line))
        ],
        "rule": {
            "findings": len(rule_predicted),
            "recall": round(rule_match.recall, 3),
            "precision": round(rule_match.precision, 3),
            "f1": round(rule_match.f1, 3),
            "found": [f"{lb.code}@ {p.file}:{p.line}" for lb, p in rule_match.matched],
        },
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="bench", description="靶场实测")
    parser.add_argument("--samples", default="", help="只跑指定样本，逗号分隔")
    parser.add_argument("--repeats", type=int, default=1, help="每个样本重复几次（看波动）")
    parser.add_argument("--out", default="eval/bench-results.json")
    parser.add_argument("--max-iterations", type=int, default=25)
    args = parser.parse_args()

    config = load_config()
    if not config.ready:
        print("错误：未配置 LLM_API_KEY（模型接口 / Key / 模型名三项不齐）", file=sys.stderr)
        return 2
    config.max_iterations = min(config.max_iterations, args.max_iterations)

    selected = list(SAMPLES)
    if args.samples:
        wanted = {s.strip() for s in args.samples.split(",") if s.strip()}
        selected = [row for row in SAMPLES if row[0] in wanted]
        missing = wanted - {row[0] for row in selected}
        if missing:
            print(f"未知样本：{', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    print(
        f"靶场实测：{len(selected)} 个样本 × {args.repeats} 次  ·  "
        f"模型 {config.model}  ·  容差档位 {list(TOLERANCES)}",
        flush=True,
    )
    print()

    results = []
    for name, tier, style, goal in selected:
        print(f"  [{tier}] {name}  ·  {style}", flush=True)
        row = run_one(name, goal, config, args.repeats)
        results.append(row)
        curve = row["runs"][0]["tolerance_curve"]
        print(
            f"    → 规则R={row['rule']['recall']:.0%}  "
            f"容差敏感性 F1: 0行={curve['0']['f1']:.0%} 2行={curve['2']['f1']:.0%} "
            f"4行={curve['4']['f1']:.0%} 8行={curve['8']['f1']:.0%}",
            flush=True,
        )
        print()

    out = Path(args.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parent.parent / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "model": config.model,
                "repeats": args.repeats,
                "tolerances": list(TOLERANCES),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("=" * 78)
    print(f"{'样本':<24}{'难度':<10}{'规则R':<8}{'模型R':<8}{'模型P':<8}{'模型F1':<8}")
    print("-" * 78)
    for row in results:
        first = row["runs"][0]
        print(
            f"{row['sample']:<24}{row['tier']:<10}"
            f"{row['rule']['recall']:<8.0%}{first['recall']:<8.0%}"
            f"{first['precision']:<8.0%}{first['f1']:<8.0%}"
        )
    print("-" * 78)
    mean_rule = sum(r["rule"]["recall"] for r in results) / len(results)
    mean_model = sum(r["runs"][0]["recall"] for r in results) / len(results)
    mean_prec = sum(r["runs"][0]["precision"] for r in results) / len(results)
    mean_f1 = sum(r["runs"][0]["f1"] for r in results) / len(results)
    print(
        f"{'平均':<24}{'':<10}{mean_rule:<8.0%}{mean_model:<8.0%}"
        f"{mean_prec:<8.0%}{mean_f1:<8.0%}"
    )
    print(f"\n结果已写入 {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
