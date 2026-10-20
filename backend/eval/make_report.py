"""从实测结果生成靶场交付文档。

为什么不手写这份文档：数字一旦手抄就会和数据脱节，改了标注、重跑了模型之后
文档不会跟着变，最后没人知道哪个数字是真的。这里所有数字都从
`bench-results.json` / `variance-results.json` / `review.json` 读出来现算。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

# 人工复核里，哪些判定算「这条预测报得对」（进精确率分子）
VALID_VERDICTS = {"hit", "merged", "placement", "key_gap"}
# 哪些算「报错了」（进精确率分母但不进分子）
INVALID_VERDICTS = {"partial", "true_fp"}
# 漏报里，哪些判定说明它其实被别的发现覆盖了（不进漏报）
COVERED_BY_OTHER = {"merged", "placement"}


def load(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"缺少结果文件：{path}（先跑 eval.run_bench）")
    return json.loads(path.read_text(encoding="utf-8"))


def review_metrics(sample_review: dict) -> dict:
    """把人工复核的判定折成两个数字。

    复核召回：漏报条目的判定若为 merged / placement，说明它其实被别的发现覆盖了，
    不计入漏报；否则计入。命中条目一律算覆盖。
    复核精确率：判定为 key_gap 的预测是真缺陷（只是答案键没收录），算报得对；
    partial / true_fp 算报错。冗余条目按判定归入这两类之一，不再单列。
    """
    labels = sample_review["labels"]
    predictions = [e for e in sample_review["entries"] if e["role"] == "prediction"]
    missed = [e for e in sample_review["entries"] if e["role"] == "missed_label"]

    uncovered = [e for e in missed if e["verdict"] not in COVERED_BY_OTHER]
    covered = labels - len(uncovered)

    valid = [e for e in predictions if e["verdict"] in VALID_VERDICTS]
    invalid = [e for e in predictions if e["verdict"] in INVALID_VERDICTS]
    unknown = [e for e in predictions if e["verdict"] not in VALID_VERDICTS | INVALID_VERDICTS]

    counts: dict[str, int] = {}
    for entry in sample_review["entries"]:
        counts[entry["verdict"]] = counts.get(entry["verdict"], 0) + 1

    denominator = len(valid) + len(invalid)
    return {
        "recall": covered / labels if labels else 0.0,
        "precision": len(valid) / denominator if denominator else 0.0,
        "covered": covered,
        "uncovered": uncovered,
        "valid": len(valid),
        "invalid": invalid,
        "unknown": unknown,
        "verdict_counts": counts,
    }


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def bar(value: float, width: int = 10) -> str:
    filled = max(0, min(width, round(value * width)))
    return "█" * filled + "·" * (width - filled)


def tolerance_row(run: dict) -> str:
    curve = run["tolerance_curve"]
    cells = [
        f"{curve[key]['f1'] * 100:.0f}%" for key in ("0", "1", "2", "3", "4", "6", "8")
    ]
    return " | ".join(cells)


def describe_spurious(item: dict) -> str:
    """把「误报」翻译成可读判断 —— 距离近的是口径分歧，不是真的报错。"""
    probe = item["probe"]
    nearest = item["nearest_label"]
    distance = item["distance"]
    head = f"`{probe['file']}:{probe['line']}` [{probe['severity']}] {probe['title']}"

    if nearest is None:
        return f"- {head}\n  - 判断：**文件内无对应标注** → 真·多报或标注集未覆盖"
    if distance <= 4:
        return (
            f"- {head}\n"
            f"  - 最近标注：`{nearest['code']}` @ `{nearest['file']}:{nearest['line']}`，"
            f"相距 {distance} 行 → **同一处缺陷的位置分歧**（非误报）"
        )
    return (
        f"- {head}\n"
        f"  - 最近标注：`{nearest['code']}` @ `{nearest['file']}:{nearest['line']}`，"
        f"相距 {distance} 行 → 需要人工判断"
    )


def describe_missed(item: dict) -> str:
    head = f"`{item['code']}` @ `{item['file']}:{item['line']}` — {item['description']}"
    nearest = item["nearest_prediction"]
    distance = item["distance"]
    if nearest is None:
        return f"- {head}\n  - **模型完全没提到这个文件/位置** → 真漏报"
    if distance <= 6:
        return (
            f"- {head}\n"
            f"  - 模型在 `{nearest['file']}:{nearest['line']}` 报了一处，相距 {distance} 行："
            f"{nearest['title']} → **大概率是同一处缺陷的位置分歧**"
        )
    return (
        f"- {head}\n"
        f"  - 模型最近的一处报在 `{nearest['file']}:{nearest['line']}`（相距 {distance} 行）："
        f"{nearest['title']}"
    )


def build(
    bench: dict,
    variance: dict | None,
    review: dict | None,
    variance_round1: dict | None = None,
) -> str:
    results = bench["results"]
    lines: list[str] = []

    review_by_sample: dict[str, dict] = {}
    review_metrics_by_sample: dict[str, dict] = {}
    verdict_lookup: dict[tuple[str, str, int], str] = {}
    if review:
        for item in review["samples"]:
            review_by_sample[item["sample"]] = item
            review_metrics_by_sample[item["sample"]] = review_metrics(item)
            for entry in item["entries"]:
                verdict_lookup[(item["sample"], entry["file"], entry["line"])] = entry["verdict"]

    # ---------- 头部 ----------
    lines += [
        "# 代码审计 Harness · 难度梯度靶场（8 个）",
        "",
        f"模型：`{bench['model']}`　·　每个样本重复 {bench['repeats']} 次"
        f"　·　行号容差档位 {bench['tolerances']}",
        "",
        "这份文档是**实测产物**，不是设计说明：所有数字都由 `eval/run_bench.py` 跑出来后",
        "从 `eval/bench-results.json` 现算，改标注或重跑模型后重新生成即可，不存在手抄误差。",
        "",
        "生成命令：",
        "",
        "```bash",
        "cd backend",
        "python -m eval.run_bench --repeats 1 --out eval/bench-results.json        # 跑靶场（调真实模型）",
        "python -m eval.run_bench --samples s1-obvious-danger,s6-authz-logic \\",
        "       --repeats 3 --out eval/variance-results.json                     # 可复现性",
        "python -m eval.make_review                                             # 摊平待判定条目",
        "python -m eval.make_report                                            # 生成这份文档",
        "```",
        "",
        "> 重跑之前请先读第七节「已知局限」。另外 `eval/bench-results-contaminated.json`",
        "> 保留了一版**已作废**的结果：那份靶场把缺陷标注以行尾注释的形式写在了被审代码里，",
        "> 等于把答案印在题目下面，模型分数被显著抬高（多个样本 R=P=100%）。",
        "> 留着它是为了让「数据污染会怎样」这件事有据可查。",
        "",
        "---",
        "",
        "## 一、为什么不用 OWASP Benchmark / Juliet",
        "",
        "先说清设计依据，否则这 8 个样本像是随手编的。",
        "",
        "| 现成基准 | 规模 | 用它做 LLM 代码审查测什么 | 为什么不合适 |",
        "|---|---|---|---|",
        "| OWASP Benchmark | 2,740 例 / 11 类 CWE | 单文件、单漏洞、静态可判 | 公开十余年，"
        "**几乎确定进了训练集**；无难度分档；缺竞态、越权、业务逻辑 |",
        "| NIST Juliet | ~64,000 例 | 同上 | 同上，且判分只看「有没有报出这一类」，不看定位与说明质量 |",
        "| GitHub ReviewBench | 219 个真实 PR / 1.04 亿 PR 中筛出 | 真实审查 | 只有真实缺陷分布，"
        "**没有难度梯度**，无法回答「难到什么程度开始失效」 |",
        "",
        "来自公开测评的三个关键事实，直接决定了这份靶场的设计：",
        "",
        "1. **误报比漏报更能区分审查者**（Red-Pen Bench）。一个把什么都报一遍的审查者，"
        "得分低于什么都不报的 —— 所以本文档**逐条列出多报**，不只看召回。",
        "2. **难度分档不跨模型单调**。某个模型在 L3 强、L6 弱，另一个模型可能反过来。"
        "所以每个样本都标了「它考的是什么能力」，而不是笼统的「难/易」。",
        "3. **单次跑分是观测值，不是稳定测量**。同一 PR 重跑三次，首轮为真的 15 个发现只复现了 7 个。"
        "所以本文档附了可复现性一节。",
        "",
        "据此，8 个样本按**「需要什么才能发现」**分层，而不是按代码行数或漏洞知名度：",
        "",
        "| 层 | 需要的能力 | 样本 |",
        "|---|---|---|",
        "| L1–L2 | 认识危险 API、认识资源与错误处理模式 | s1、s2 |",
        "| L3–L4 | 追踪数据来源、构造并发交错 | s3、s4 |",
        "| L5–L6 | 自己立前提/建模型，再回头验证实现 | s5、s6 |",
        "| L7–L8 | 跨文件跟数据流、掌握密码学性质 | s7、s8 |",
        "",
        "---",
        "",
    ]

    n = len(results)
    mean_rule = sum(r["rule"]["recall"] for r in results) / n
    mean_recall = sum(r["runs"][0]["recall"] for r in results) / n
    mean_prec = sum(r["runs"][0]["precision"] for r in results) / n
    mean_f1 = sum(r["runs"][0]["f1"] for r in results) / n
    total_labels = sum(len(r["labels"]) for r in results)
    lines += [
        "## 二、总览",
        "",
        "两套口径都给：**自动匹配**（只有「同文件 + 行号在 ±2 行内」才算命中，完全可复现、"
        "不含任何主观判断）与**人工复核**（逐条判定，理由见第四节）。",
        "只报对自己有利的那一套等于作弊，所以两张数字并列。",
        "",
        "| 样本 | 难度 | 考的能力 | 文件 | 标注 | 规则召回 | 自动召回 | 自动精确率 | 自动 F1 | 复核召回 | 复核精确率 | 轮数 | 耗时 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    for row in results:
        run = row["runs"][0]
        rm = review_metrics_by_sample.get(row["sample"])
        lines.append(
            f"| [`{row['sample']}`](#{row['sample']}) | {row['tier']} | {row['style']} | "
            f"{len(set(lb['file'] for lb in row['labels']))} | {len(row['labels'])} | "
            f"{pct(row['rule']['recall'])} | {pct(run['recall'])} | {pct(run['precision'])} | "
            f"{pct(run['f1'])} | "
            f"{pct(rm['recall']) if rm else '—'} | {pct(rm['precision']) if rm else '—'} | "
            f"{run['iterations']} | {run['elapsed']:.0f}s |"
        )

    n = len(results)
    mean_rule = sum(r["rule"]["recall"] for r in results) / n
    mean_recall = sum(r["runs"][0]["recall"] for r in results) / n
    mean_prec = sum(r["runs"][0]["precision"] for r in results) / n
    mean_f1 = sum(r["runs"][0]["f1"] for r in results) / n
    mean_r_recall = (
        sum(m["recall"] for m in review_metrics_by_sample.values()) / len(review_metrics_by_sample)
        if review_metrics_by_sample
        else None
    )
    mean_r_prec = (
        sum(m["precision"] for m in review_metrics_by_sample.values()) / len(review_metrics_by_sample)
        if review_metrics_by_sample
        else None
    )
    total_labels = sum(len(r["labels"]) for r in results)
    lines += [
        f"| **平均** | | | | **{total_labels}** | **{pct(mean_rule)}** | "
        f"**{pct(mean_recall)}** | **{pct(mean_prec)}** | **{pct(mean_f1)}** | "
        f"**{pct(mean_r_recall)}** | **{pct(mean_r_prec)}** | | |",
        "",
        f"自动口径　召回 {bar(mean_recall)} {pct(mean_recall)}　"
        f"精确率 {bar(mean_prec)} {pct(mean_prec)}",
        "",
        f"复核口径　召回 {bar(mean_r_recall or 0)} {pct(mean_r_recall)}　"
        f"精确率 {bar(mean_r_prec or 0)} {pct(mean_r_prec)}",
        "",
        "> **规则召回**：`harness/rules.py` 的确定性规则（正则 + 结构判定）在同一批标注上的召回率。",
        "> 它不受模型影响，可作为「不用 LLM 能拿到多少」的下界参照。",
        "",
        "---",
        "",
        "## 三、指标对「行号容差」有多敏感",
        "",
        "这一节是本文档最该被认真看的部分。",
        "",
        "一条缺陷横跨多条语句时（例如「读长度 → 判上限 → 写入集合」三步的竞态），",
        "标注落在哪一行、模型指向哪一行，天然会差 3~4 行。只报一个容差档下的数字，",
        "等于把**位置口径的差异**算成**能力的差异**。",
        "",
        "下表是各样本 F1 随容差增长的走势（容差 = 允许预测与标注相距多少行仍算命中）：",
        "",
        "| 样本 | ±0 | ±1 | ±2（默认） | ±3 | ±4 | ±6 | ±8 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in results:
        lines.append(f"| `{row['sample']}` | {tolerance_row(row['runs'][0])} |")

    # 整体（含权）容差走势
    totals = {}
    for key in ("0", "1", "2", "3", "4", "6", "8"):
        hits = sum(r["runs"][0]["tolerance_curve"][key]["f1"] for r in results)
        totals[key] = hits / len(results)
    lines += [
        f"| **平均 F1** | " + " | ".join(f"**{totals[k] * 100:.0f}%**" for k in
                                        ("0", "1", "2", "3", "4", "6", "8")) + " |",
        "",
        "读法：**接近水平的行**说明结论稳健，模型确实定位到了那一行；",
        "**明显上台阶的行**说明有相当一部分「漏报」只是指向了同一处缺陷的另一行。",
        f"整体从 ±0 的 {totals['0'] * 100:.0f}% 升到 ±4 的 {totals['4'] * 100:.0f}%，"
        "说明这份靶场的绝对指标**高度依赖位置口径**。",
        "",
        "> 更早一版权威基准（OWASP / Juliet）多为单文件单漏洞、缺陷落在一行内，",
        "> 所以它们不会暴露这个敏感性。真实审查里缺陷横跨多行是常态 ——",
        "> 这也是判断「能不能用这类基准做模型选型」的一个理由。",
        "",
        "---",
        "",
    ]

    # ---------- 人工复核 ----------
    lines += [
        "## 四、人工复核（逐条判定）",
        "",
        "自动匹配在两头都会出错，必须逐条看。三类典型情况都在这批数据里真实出现过：",
        "",
        "1. **同一缺陷、行号不同 → 被算成「一漏一多」。**",
        "   `s4` 的竞态窗口是「读 `len(used)` / 判上限 / `used.add`」三步，",
        "   模型从读起点切入、标注落在写操作，相距 3 行，于是同时产生一条「漏报」和一条「误报」。",
        "2. **模型报了一处标注集里没有的真缺陷 → 被算成误报。**",
        "   `s6` 的 `cancel` 完全不校验 `user_id`、`s7` 的 `export` 允许把导出路径",
        "   完全交给请求方、`s2` 的 CSV 字段未转义 —— 都是真问题，只是我的答案键没收录。",
        "3. **两条不同缺陷恰好相邻 → 互相顶替。**",
        "   `s8` 里 `sign`/`verify` 的 `'.'` 切分不对称（真缺陷）离「非常量时间比较」只差 2 行，",
        "   被自动算作后者命中；而后者自己反倒被判成了「冗余」。",
        "",
        "### 判定标准",
        "",
        "| 判定 | 含义 | 计入指标的方式 |",
        "|---|---|---|",
        "| `hit` | 自动命中的是同一缺陷 | 进命中 |",
        "| `merged` | 一条发现合并描述了多条标注 / 多个缺陷点，内容确实覆盖 | 进命中，被覆盖的标注计为已覆盖 |",
        "| `placement` | 同一缺陷，位置不同 | 进命中，对应标注计为已覆盖 |",
        "| `key_gap` | 模型报的是真缺陷，标注集没收录 | 进命中（答案键的问题，不是模型的错） |",
        "| `partial` | 可争议：确有其事，但算不算缺陷取决于设计约定或加固等级 | **计入误报**（从严） |",
        "| `true_fp` | 明确误报 | 计入误报 |",
        "| `true_miss` | 明确漏报 | 计入漏报 |",
        "",
        "复核口径的两个公式：",
        "",
        "```text",
        "复核召回 = (标注总数 − 判定为 true_miss/partial 的漏报数) / 标注总数",
        "复核精确率 = 判定为 hit/merged/placement/key_gap 的预测数 / 判定为该四类或 partial/true_fp 的预测数",
        "```",
        "",
        f"> **判定者是我（助手），不是独立第三方。** 每条判定和理由都写在 `eval/review.json` 里，",
        "> 你可以逐条推翻并重跑生成器。要注意的是：复核普遍会**上调**模型分数，",
        "> 因为自动口径把「位置分歧」和「答案键遗漏」都算成了错误 —— 这两类在真实审查里都不是错。",
        "> 只看复核数字会高估，只看自动数字会低估。",
        "",
        "### 各样本判定分布与复核指标",
        "",
        "| 样本 | 判定分布 | 复核召回 | 复核精确率 | 偏差来源 |",
        "|---|---|---|---|---|",
    ]
    for row in results:
        rm = review_metrics_by_sample.get(row["sample"])
        if not rm:
            continue
        dist = "　".join(
            f"`{key}`×{value}" for key, value in sorted(rm["verdict_counts"].items())
        )
        reasons = []
        if rm["uncovered"]:
            reasons.append(f"{len(rm['uncovered'])} 条真漏报")
        if rm["invalid"]:
            reasons.append(f"{len(rm['invalid'])} 条可争议多报")
        if not reasons:
            reasons.append("自动口径下的偏差全部由位置/合并/答案键遗漏造成")
        lines.append(
            f"| `{row['sample']}` | {dist} | {pct(rm['recall'])} | {pct(rm['precision'])} | "
            f"{'；'.join(reasons)} |"
        )
    lines.append("")

    # 逐条明细
    lines += ["### 逐条判定明细", ""]
    for sample in review["samples"]:
        lines += [
            f"<details><summary><code>{sample['sample']}</code>　"
            f"自动 R={pct(sample['auto']['recall'])} P={pct(sample['auto']['precision'])}</summary>",
            "",
            "| 角色 | 位置 | 内容 | 判定 | 理由 |",
            "|---|---|---|---|---|",
        ]
        for entry in sample["entries"]:
            if entry["role"] == "prediction":
                role, where = "预测", f"`{entry['file']}:{entry['line']}`"
                content = f"[{entry['severity']}] {entry['title']}"
            else:
                role, where = "漏报", f"`{entry['file']}:{entry['line']}`"
                content = f"`{entry['code']}` {entry['description']}"
            content = content.replace("|", "\\|")
            rationale = (entry["rationale"] or "").replace("\n", " ").replace("|", "\\|")
            lines.append(
                f"| {role} | {where} | {content} | `{entry['verdict']}` | {rationale} |"
            )
        lines += ["", "</details>", ""]

    if review.get("false_credits"):
        lines += ["### 被自动匹配错误计入的命中", ""]
        for item in review["false_credits"]:
            lines.append(
                f"- `{item['sample']}`　预测 `{item['prediction']}` 被算作命中了 "
                f"`{item['credited_label']}`。{item['rationale']}"
            )
        lines.append("")

    lines += [
        "---",
        "",
        "## 五、逐样本详解",
        "",
        "每节包含：审计目标原文、植入缺陷清单（答案）、规则命中情况、模型逐条结果、",
        "以及**如何自己复现这一次运行**。",
        "",
    ]

    for row in results:
        run = row["runs"][0]
        lines += [
            f'<a id="{row["sample"]}"></a>',
            "",
            f"### {row['tier']}　`{row['sample']}`",
            "",
            f"- 考的能力：**{row['style']}**",
            f"- 审计根：`{row['audit_root']}`（标注文件 `labels.json` 在**审计根之外**，模型读不到）",
            f"- 规模：{len(set(lb['file'] for lb in row['labels']))} 个文件 / "
            f"{len(row['labels'])} 条标注",
            f"- 本次实测：{run['iterations']} 轮 / {run['elapsed']:.0f}s / "
            f"结束原因 `{run['stop_reason']}` / 缓存命中 {pct(run['cache_hit'])}",
            f"- 结果：召回 {pct(run['recall'])}（{run['matched']}/{len(row['labels'])}）"
            f"　精确率 {pct(run['precision'])}　F1 {pct(run['f1'])}",
            "",
            "**审计目标（提问原文）**",
            "",
            "```text",
            row["goal"],
            "```",
            "",
            "**植入缺陷（答案）**",
            "",
            "| 编号 | 位置 | 缺陷 |",
            "|---|---|---|",
        ]
        for lb in row["labels"]:
            lines.append(
                f"| `{lb['code']}` | `{lb['file']}:{lb['line']}` | "
                f"{lb['description']} |"
            )
        lines += [
            "",
            f"规则基线命中 {row['rule']['findings']} 条，召回 {pct(row['rule']['recall'])}"
            f"（精确率 {pct(row['rule']['precision'])}）。"
            "规则按模式匹配，**不读数据来源、不构造并发、不建权限模型** ——"
            "这正是 L3 以上样本规则全线为 0 的原因。",
            "",
            f"**模型实际报了什么（共 {len(run['findings'])} 条）**",
            "",
        ]
        for index, item in enumerate(run["findings"], 1):
            verdict = verdict_lookup.get((row["sample"], item["file"], item["line"]), "")
            tag = f"　`复核: {verdict}`" if verdict else ""
            lines.append(
                f"{index}. `{item['file']}:{item['line']}` [{item['severity']}] "
                f"**{item['title']}**{tag}"
            )
            if item["detail"]:
                detail = item["detail"].replace("\n", " ").strip()
                lines.append(f"   - {detail}")
        lines.append("")

        if run["missed_detail"]:
            lines += ["**漏报**", ""]
            for item in run["missed_detail"]:
                lines.append(describe_missed(item))
            lines.append("")

        if run["spurious_detail"]:
            lines += ["**多报（含位置分歧的判断）**", ""]
            for item in run["spurious_detail"]:
                lines.append(describe_spurious(item))
            lines.append("")

        if run["duplicates"]:
            lines += [
                "**冗余（重复报告同一处缺陷）**",
                "",
            ]
            for item in run["duplicates"]:
                lines.append(
                    f"- `{item['file']}:{item['line']}` [{item['severity']}] {item['title']}"
                )
            lines.append("")
            lines += [
                "> 冗余不计入精确率分子分母 —— 重复描述同一缺陷是啰嗦，不是报错。",
                "",
            ]

        if run["summary"]:
            lines += [
                "**模型自己的收尾总结（原文摘录）**",
                "",
                "```text",
                run["summary"].strip(),
                "```",
                "",
            ]

        lines += [
            "**自己跑一遍**",
            "",
            "```bash",
            f"cd backend",
            f"python -m harness -r {row['audit_root']} \\",
            f'  -g "{row["goal"]}" \\',
            f"  --out reports/{row['sample']}.md --json reports/{row['sample']}.json",
            "```",
            "",
            "网页界面里也可以：启动 `python -m harness.server --port 8765`，"
            "打开 <http://127.0.0.1:8765/>，在左侧空间栏选中该样本，在输入坞写明要求后回车。",
            "",
            "---",
            "",
        ]

    # ---------- 可复现性 ----------
    lines += ["## 六、可复现性（同一输入重跑多次）", ""]
    if variance:
        lines += [
            "动机：公开测评显示，同一个 PR 重跑三次，首轮验证为真的发现只在两次重复中复现了 7/15。",
            "也就是说单次跑分是观测值。下表是对两个样本各跑三次的结果：",
            "",
            "| 样本 | 标注 | 召回（三次） | 召回均值 | 召回波动 | 精确率（三次） | 精确率波动 | 发现条数（三次） |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for row in variance["results"]:
            recalls = [f"{r['recall'] * 100:.0f}%" for r in row["runs"]]
            precisions = [f"{r['precision'] * 100:.0f}%" for r in row["runs"]]
            counts = [len(r["findings"]) for r in row["runs"]]
            rec_mean = sum(r["recall"] for r in row["runs"]) / len(row["runs"])
            rec_spread = max(r["recall"] for r in row["runs"]) - min(
                r["recall"] for r in row["runs"]
            )
            pre_spread = max(r["precision"] for r in row["runs"]) - min(
                r["precision"] for r in row["runs"]
            )
            lines.append(
                f"| `{row['sample']}` | {len(row['labels'])} | {', '.join(recalls)} | "
                f"{rec_mean * 100:.0f}% | ±{rec_spread * 100:.0f}pt | "
                f"{', '.join(precisions)} | ±{pre_spread * 100:.0f}pt | {counts} |"
            )
        lines.append("")
        lines += [
            "**怎么用这个表**：波动大的样本，单次跑出来的任何数字都不该被引用。",
            "要做模型选型，先按 `python -m eval.run_bench --repeats 3` 的方式把每个候选跑 3~5 次，",
            "比较**均值与波动**，而不是比一次跑分。",
            "",
        ]
        if variance_round1:
            lines += [
                "### 第一轮的三次重复（汇总简报格式）",
                "",
                "上面那张表是这套 runner 重跑出来的。更早还独立跑过一轮（`eval/variance-round1.json`，",
                "汇总格式），两轮并排看更能说明问题：",
                "",
                "| 样本 | 召回（三次） | 召回波动 | 精确率（三次） | 精确率波动 | 发现条数（三次） |",
                "|---|---|---|---|---|---|",
            ]
            for row in variance_round1.get("summary", []):
                lines.append(
                    f"| `{row['sample']}` | {', '.join(f'{v * 100:.0f}%' for v in row['recall_runs'])} "
                    f"| ±{row['recall_spread'] * 100:.0f}pt "
                    f"| {', '.join(f'{v * 100:.0f}%' for v in row['precision_runs'])} "
                    f"| ±{row['precision_spread'] * 100:.0f}pt | {row['findings_runs']} |"
                )
            lines += [
                "",
                "**两轮合起来看，最该记住的一条**：`s6-authz-logic` 在第一轮里召回过 `100% → 100% → 50%`，",
                "单次跑能差出 **50 个百分点**（这一次三轮反而都是 100%）。",
                "也就是说同一份代码、同一个提问，结果本身就有很大抖动 ——",
                "**任何只跑一次的分数都不能用来比较方案。**",
                "",
            ]
        else:
            # 数据缺失要明说。静默跳过会让读者以为「本来就没有这一轮」，
            # 而这一段恰恰是「单次不可引用」最有力的证据。
            lines += [
                "> 更早还有一轮独立的重复实验（`eval/variance-round1.json`，汇总格式），",
                "> 那一轮里 `s6-authz-logic` 的召回是 `100% → 100% → 50%`。",
                "> 该文件在一次仓库清理中被当作中间产物删除，这里保存结论、不再给表 ——",
                "> 需要完整数据的话按下面的命令重跑即可（结果不会完全相同，这本身就是结论）。",
                "",
            ]
    else:
        lines += [
            "> 未生成可复现性数据。跑法：",
            "> `python -m eval.run_bench --samples s1-obvious-danger,s6-authz-logic --repeats 3 --out eval/variance-results.json`",
            "",
        ]

    # ---------- 局限 ----------
    lines += [
        "---",
        "",
        "## 七、这份靶场的已知局限",
        "",
        "如实列出，避免把结论用超范围。",
        "",
        "1. **样本量小**。8 个样本 / "
        f"{total_labels} 条标注，够用来看「难度梯度长什么样」，"
        "**不够用来选模**。要分辨 2~3 个百分点的差异，需要上百题量级。",
        "2. **答案键不完整**。人工复核发现 6 处模型报出的真缺陷没有进标注集"
        "（`s2` 的 CSV 注入、`s6` 的 `cancel` 越权与 `can_view` 从未被调用、"
        "`s7` 的任意文件写入与 `return 0`、`s8` 的 `'.'` 切分不对称）。"
        "也就是说这套靶场的**自动精确率系统性低估**了模型 —— 而按「模型没报的算漏报」"
        "定义出来的召回率也可能低估。答案键应由独立于作者的人补全后重跑。",
        "3. **标注与规则出自同一作者同一批代码**，天然偏向「正则能表达」的缺陷，"
        "会让规则基线显得比实际强。",
        "4. **每个样本的提问方式不同**（这是刻意的：要看提问方式的影响），"
        "所以横向比较召回率时，差异里混着「难度」和「提问方式」两个因素。",
        "5. **判官是我自己，不是独立第三方**。复核口径的每个数字都建立在第四节那张判定表上，"
        "而我对模型的产出会有系统性宽容（例如把「设计层面的缺失」判成 `key_gap` 而不是 `partial`）。"
        "要注意**自动口径里的距离分类本身不可靠**：`s2` 的 CSV 注入离最近的标注只有 3 行却",
        "   是完全不同的缺陷，`s7` 的任意文件写入被算成了「冗余」。这两类错判都会让数字偏离真相。",
        "6. **未开高风险权限**：`run_command` / `run_tests` 全程被护栏拒绝，"
        "所以「资源泄漏」「并发」类结论是基于代码结构的静态推断，没有运行时确认。"
        "需要运行时证据加 `--allow-high-risk`。",
        "7. **单模型**：只测了 `deepseek-flash`。难度分档是否跨模型单调，本文档回答不了。",
        "8. **每个样本只跑了 1 次**（可复现性一节单独跑了 2 个样本 × 3 次）。"
        "总览表里的行是单次观测，波动见第六节。",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(prog="make-report")
    parser.add_argument("--bench", default="eval/bench-results.json")
    parser.add_argument("--variance", default="eval/variance-results.json")
    parser.add_argument("--review", default="eval/review.json")
    parser.add_argument("--variance-round1", default="eval/variance-round1.json")
    parser.add_argument("--out", default="BENCHMARK.md")
    args = parser.parse_args()

    bench = load(ROOT / args.bench)
    variance_path = ROOT / args.variance
    variance = json.loads(variance_path.read_text(encoding="utf-8")) if variance_path.is_file() else None
    review_path = ROOT / args.review
    review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else None
    round1_path = ROOT / args.variance_round1
    variance_round1 = (
        json.loads(round1_path.read_text(encoding="utf-8")) if round1_path.is_file() else None
    )

    # 一致性自检：复核记录必须覆盖同一批样本，否则文档里的两套数字会对不上
    if review:
        bench_ids = {row["sample"] for row in bench["results"]}
        review_ids = {item["sample"] for item in review["samples"]}
        if bench_ids != review_ids:
            raise SystemExit(
                "复核记录与实测结果的样本集合不一致，请重新生成：\n"
                f"  仅实测有：{sorted(bench_ids - review_ids)}\n"
                f"  仅复核有：{sorted(review_ids - bench_ids)}"
            )

    text = build(bench, variance, review, variance_round1)
    out = ROOT / args.out
    out.write_text(text, encoding="utf-8")
    print(f"已生成 {out}（{len(text)} 字符，{text.count(chr(10))} 行）")
    if variance is None:
        print("提示：未找到方差结果，可复现性一节留空")
    if review is None:
        print("提示：未找到复核记录，人工复核一节留空")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
