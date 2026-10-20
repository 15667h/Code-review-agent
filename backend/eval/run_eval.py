"""评估执行器：跑多次、算指标、给诊断。

用法：

    # 离线基线（不需要 API Key）
    python -m eval.run_eval --offline --runs 3

    # 用真实模型对比
    python -m eval.run_eval --runs 3 --out eval/result-deepseek.json

为什么同时报两种匹配口径：

一次运行会产出十几条发现，而人工标注是**逐实例**写的。两者粒度经常对不上 ——
模型把「同一个文件里同一个模式的 3 个 assert」合并成一条报告，
逐实例口径会判它漏了 2 条，但这在代码审查里是合理甚至更好的写法。
所以这里对同一批运行结果算两遍：

- ``instance``  逐条标注计分（保守，漏一条算一条）
- ``file_rule`` 同文件 + 同规则号视为一个缺陷单元（贴近「审查者有没有发现这个问题」）

两个数字都报出来，由读者判断差异来自能力还是口径。**不要只报对自己有利的那个。**

为什么要跑多次：单次运行只能用来筛选方向。书中的口径是
**每个配置至少 3~5 个随机种子**，报告均值和波动范围，而不是拿一次结果说事。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.config import load_config
from harness.loop import resolve_workspace, run_audit
from harness.model import OpenAIModel
from harness.offline import HeuristicModel

from .dataset import (
    GROUPINGS,
    EvalCase,
    MatchOutcome,
    case_passed,
    load_default_cases,
    match_findings,
)
from .metrics import RunMatrix, format_summary

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXAMPLES = BACKEND_ROOT / "examples"

GROUPING_NOTE = {
    "instance": "逐条标注计分",
    "file_rule": "同文件 + 同规则号合并为一个缺陷单元",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval", description="Margin Harness 评估")
    parser.add_argument("--examples", default=str(DEFAULT_EXAMPLES), help="评估样本根目录")
    parser.add_argument("--runs", type=int, default=3, help="每个样本运行几次（建议 3~5）")
    parser.add_argument("--offline", action="store_true", help="用离线规则模型作为基线")
    parser.add_argument("--model", default="", help="覆盖模型名")
    parser.add_argument("--out", default="", help="把结果写成 JSON")
    parser.add_argument("--min-recall", type=float, default=1.0, help="判定通过所需的最低召回率")
    parser.add_argument("--min-precision", type=float, default=0.5, help="判定通过所需的最低精确率")
    parser.add_argument(
        "--grouping",
        choices=GROUPINGS,
        default="instance",
        help="决定 Pass 判定的主口径；两种口径的指标都会打印",
    )
    parser.add_argument("--quiet", action="store_true", help="不打印每次运行的细节")
    parser.add_argument("--detail", action="store_true", help="打印漏报/误报的具体条目")
    return parser


def _nearest(label, predicted: list) -> str:
    """给一条漏报标注找到最接近的预测，用来区分「真漏」与「位置口径差异」。"""
    same_file = [item for item in predicted if getattr(item, "file", "") == label.file]
    if not same_file:
        return "（该文件无任何预测）"
    best = min(same_file, key=lambda item: abs(getattr(item, "line", -999) - label.line))
    delta = getattr(best, "line", -999) - label.line
    return (
        f"{label.file}:{getattr(best, 'line', '?')}（Δ{delta:+d}）"
        f"{getattr(best, 'title', '')[:40]}"
    )


def run_case(
    case: EvalCase,
    *,
    offline: bool,
    config,
    runs: int,
    verbose: bool,
) -> tuple[list, dict[str, list[MatchOutcome]]]:
    """跑完一个样本，返回每次的运行结果与**两种口径**下的匹配结论。

    两种口径共用同一批运行结果 —— 否则要跑两倍的次数，纯属浪费调用。
    """
    results: list = []
    per_grouping: dict[str, list[MatchOutcome]] = {name: [] for name in GROUPINGS}

    for run_index in range(runs):
        model = HeuristicModel() if offline else OpenAIModel(config)
        try:
            result = run_audit(
                goal=case.goal,
                workspace=resolve_workspace(case.repo),
                model=model,
                config=config,
            )
        finally:
            close = getattr(model, "close", None)
            if callable(close):
                close()

        results.append(result)
        for name in GROUPINGS:
            per_grouping[name].append(
                match_findings(result.findings, case.expected, grouping=name)
            )

        if verbose:
            strict = per_grouping["instance"][-1]
            grouped = per_grouping["file_rule"][-1]
            print(
                f"    run {run_index + 1}/{runs}  "
                f"[逐条] {strict.describe()}  "
                f"[合并] R={grouped.recall:.1%}  "
                f"轮数={result.iterations} 原因={result.stop_reason}"
            )

    return results, per_grouping


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    args = build_parser().parse_args(argv)
    examples_root = Path(args.examples).resolve()
    cases = load_default_cases(examples_root)
    if not cases:
        print(f"错误：{examples_root} 下没有找到评估样本目录。", file=sys.stderr)
        return 2

    config = load_config()
    if args.model:
        config.model = args.model
    config.max_iterations = max(config.max_iterations, 40)

    if not args.offline and not config.ready:
        print(
            "错误：未配置 LLM_API_KEY（模型接口 / Key / 模型名三项不齐）。加 --offline 可先跑离线基线。",
            file=sys.stderr,
        )
        return 2

    label = "heuristic-offline" if args.offline else config.model
    print(f"评估配置：{label}  ·  样本 {len(cases)} 个  ·  每个跑 {args.runs} 次")
    print(f"主判定口径：{args.grouping}（{GROUPING_NOTE[args.grouping]}）")
    print(f"通过门槛：召回 ≥ {args.min_recall:.0%} 且 精确率 ≥ {args.min_precision:.0%}")
    print("")

    matrices: dict[str, RunMatrix] = {name: RunMatrix() for name in GROUPINGS}
    details: list[dict] = []

    for case in cases:
        print(f"  {case.describe()}")
        results, per_grouping = run_case(
            case,
            offline=args.offline,
            config=config,
            runs=args.runs,
            verbose=not args.quiet,
        )

        for name in GROUPINGS:
            outcomes = [
                case_passed(m, min_recall=args.min_recall, min_precision=args.min_precision)
                for m in per_grouping[name]
            ]
            matrices[name].add(
                case.id, outcomes, [float(r.usage.total_tokens) for r in results]
            )

        strict = per_grouping["instance"][0] if per_grouping["instance"] else None
        grouped = per_grouping["file_rule"][0] if per_grouping["file_rule"] else None
        predicted = list(results[0].findings) if results else []

        if args.detail and strict is not None:
            if strict.missed:
                print("      ↳ 逐条口径下的漏报（附最接近的预测，用于区分真漏与位置口径差异）：")
                for item in strict.missed:
                    mark = "已被合并覆盖" if item in grouped.collateral else "无对应预测"
                    print(f"        · {item.code} {item.file}:{item.line} [{mark}] → {_nearest(item, predicted)}")
            if strict.spurious:
                print("      ↳ 误报（规则集里没有、模型报了的条目）：")
                for item in strict.spurious:
                    print(
                        f"        · {getattr(item, 'file', '?')}:{getattr(item, 'line', '?')} "
                        f"[{getattr(item, 'severity', '?')}] {getattr(item, 'title', '')[:52]}"
                    )

        details.append(
            {
                "case": case.id,
                "expected": len(case.expected),
                "outcomes": {
                    name: [
                        case_passed(
                            m, min_recall=args.min_recall, min_precision=args.min_precision
                        )
                        for m in per_grouping[name]
                    ]
                    for name in GROUPINGS
                },
                "missed_on_first_run": [
                    f"{item.code} {item.file}:{item.line}" for item in (strict.missed if strict else [])
                ],
                "missed_detail_on_first_run": [
                    {
                        "code": item.code,
                        "file": item.file,
                        "line": item.line,
                        "merged_into_another_report": bool(grouped and item in grouped.collateral),
                        "nearest": _nearest(item, predicted),
                    }
                    for item in (strict.missed if strict else [])
                ],
                "spurious_on_first_run": [
                    {
                        "file": getattr(item, "file", ""),
                        "line": getattr(item, "line", 0),
                        "severity": getattr(item, "severity", ""),
                        "title": getattr(item, "title", ""),
                    }
                    for item in (strict.spurious if strict else [])
                ],
                "metrics_first_run": {
                    "instance": {
                        "precision": strict.precision if strict else 0.0,
                        "recall": strict.recall if strict else 0.0,
                        "f1": strict.f1 if strict else 0.0,
                    },
                    "file_rule": {
                        "precision": grouped.precision if grouped else 0.0,
                        "recall": grouped.recall if grouped else 0.0,
                        "f1": grouped.f1 if grouped else 0.0,
                    },
                },
                "iterations": [r.iterations for r in results],
                "stop_reasons": [r.stop_reason for r in results],
                "cache_hit_ratio": [
                    (r.usage.cached_tokens / r.usage.prompt_tokens) if r.usage.prompt_tokens else 0.0
                    for r in results
                ],
                "total_tokens": [r.usage.total_tokens for r in results],
            }
        )

    summaries = {name: matrices[name].summary() for name in GROUPINGS}

    print("")
    print("─" * 60)
    print(f"评估汇总（{label}）")
    print("─" * 60)
    for name in GROUPINGS:
        print("")
        print(f"■ 口径 {name} —— {GROUPING_NOTE[name]}")
        print(format_summary(summaries[name]))

    # ---- 单轮平均指标（比 Pass 更细，便于看出差异来自精确率还是召回率）----
    print("")
    print("首轮平均指标（均值，用于对比口径与配置）：")
    for name in GROUPINGS:
        rows = [item["metrics_first_run"][name] for item in details]
        if not rows:
            continue
        avg_p = sum(row["precision"] for row in rows) / len(rows)
        avg_r = sum(row["recall"] for row in rows) / len(rows)
        avg_f = sum(row["f1"] for row in rows) / len(rows)
        print(f"  {name:<10} P={avg_p:.1%}  R={avg_r:.1%}  F1={avg_f:.1%}")

    print("")
    missed_any = [item for item in details if item["missed_on_first_run"]]
    if missed_any:
        print("逐条口径下首轮漏报的标注缺陷：")
        for item in missed_any:
            merged = sum(
                1 for row in item["missed_detail_on_first_run"] if row["merged_into_another_report"]
            )
            total = len(item["missed_on_first_run"])
            print(f"  {item['case']}: {', '.join(item['missed_on_first_run'])}")
            print(f"    其中 {merged}/{total} 条属于「已被同一条报告合并覆盖」，"
                  f"{total - merged}/{total} 条无对应预测")
        print("  （加 --detail 可看到每条漏报最接近的预测）")
        print("")

    spurious_any = [item for item in details if item["spurious_on_first_run"]]
    if spurious_any:
        total_spurious = sum(len(item["spurious_on_first_run"]) for item in spurious_any)
        print(f"误报合计 {total_spurious} 条（加 --detail 看明细）")
        print("")

    if args.out:
        payload = {
            "label": label,
            "model": "heuristic-offline" if args.offline else config.model,
            "runs_per_case": args.runs,
            "primary_grouping": args.grouping,
            "thresholds": {"min_recall": args.min_recall, "min_precision": args.min_precision},
            "summary": summaries,
            "details": details,
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
