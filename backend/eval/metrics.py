"""评估指标。

书中的核心区分（第七章）：

- **Pass@k = 1-(1-p)^k** —— k 次里至少一次成功。看的是**能力上限**，
  适合探索性任务，也常被用来讲「技术奇观」。
- **Pass^k = p^k** —— k 次**每一次**都成功。看的是**业务可靠性**，
  有副作用或涉及合规的操作必须看这个。
- **标准误 SE(p) ≈ sqrt(p(1-p)/n)** —— 用来判断两个方案的差异是真实的还是噪声。

书中的算例（p=0.6, k=5）：Pass@5 ≈ 99.0%，Pass^5 ≈ 7.8%。
同一个系统，两个口径差了一个数量级 —— 所以报告里必须写清用的是哪个口径。

全部用标准库实现，不依赖 scipy。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations


# =============================================================================
# 基础指标
# =============================================================================


def pass_at_k(p: float, k: int) -> float:
    """k 次至少一次成功。"""
    if k <= 0:
        raise ValueError("k 必须为正整数")
    return 1.0 - (1.0 - p) ** k


def pass_pow_k(p: float, k: int) -> float:
    """k 次全部成功（连续 k 次都过）。"""
    if k <= 0:
        raise ValueError("k 必须为正整数")
    return p**k


def standard_error(p: float, n: int) -> float:
    """比例的标准误。样本量按 1/sqrt(n) 缩小误差 —— 想分辨 2~3 个百分点，
    评估集只有几十题是远远不够的。"""
    if n <= 0:
        raise ValueError("n 必须为正整数")
    return math.sqrt(max(0.0, p * (1.0 - p)) / n)


def confidence_interval(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """正态近似的置信区间（默认 95%）。"""
    se = standard_error(p, n)
    return max(0.0, p - z * se), min(1.0, p + z * se)


def min_sample_size(target_margin: float, p: float = 0.5, z: float = 1.96) -> int:
    """要在 95% 置信度下分辨 ``target_margin`` 的差异，至少需要多少样本。

    书中的口径：n=100、p=70% 时 95% 区间约为 ±9 个百分点；
    若预期收益只有 2~3 个百分点，必须先扩大样本。
    """
    if target_margin <= 0:
        raise ValueError("target_margin 必须为正")
    return math.ceil((z**2) * p * (1 - p) / (target_margin**2))


# =============================================================================
# 配对比较
# =============================================================================


@dataclass
class PairedResult:
    a_only: int = 0
    b_only: int = 0
    both_pass: int = 0
    both_fail: int = 0
    p_value: float = 1.0

    @property
    def discordant(self) -> int:
        return self.a_only + self.b_only

    def describe(self, name_a: str = "A", name_b: str = "B", alpha: float = 0.05) -> str:
        if self.discordant == 0:
            return f"{name_a} 与 {name_b} 在全部配对样本上结果一致，无法区分优劣。"
        verdict = "差异显著" if self.p_value < alpha else "差异不显著（可能只是噪声）"
        return (
            f"{name_a} 独有通过 {self.a_only} 题，{name_b} 独有通过 {self.b_only} 题；"
            f"McNemar 精确检验 p={self.p_value:.4f} → {verdict}"
        )


def _binom_two_sided(k: int, n: int) -> float:
    """精确二项检验的双侧 p 值（p=0.5）。"""
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def mcnemar(a: list[bool], b: list[bool]) -> PairedResult:
    """配对二值结果的 McNemar 检验。

    配对的含义是**两组共享同一批任务与随机条件**，而不是各抽一批样本比平均值 ——
    后者会把任务难度差异混进结论里。
    """
    if len(a) != len(b):
        raise ValueError("配对比较要求两次运行的任务集合完全一致")

    result = PairedResult()
    for x, y in zip(a, b):
        if x and y:
            result.both_pass += 1
        elif x and not y:
            result.a_only += 1
        elif not x and y:
            result.b_only += 1
        else:
            result.both_fail += 1

    n = result.discordant
    if n > 0:
        result.p_value = _binom_two_sided(min(result.a_only, result.b_only), n)
    return result


# =============================================================================
# 多轮运行汇总
# =============================================================================


@dataclass
class RunMatrix:
    """一次评估的原始结果：``cases[i][r]`` 表示第 i 题第 r 次是否通过。"""

    case_ids: list[str] = field(default_factory=list)
    runs: list[list[bool]] = field(default_factory=list)
    costs: list[list[float]] = field(default_factory=list)

    def add(self, case_id: str, outcomes: list[bool], costs: list[float] | None = None) -> None:
        self.case_ids.append(case_id)
        self.runs.append(list(outcomes))
        self.costs.append(list(costs) if costs else [])

    @property
    def k(self) -> int:
        return max((len(row) for row in self.runs), default=0)

    def per_case_rates(self) -> list[float]:
        return [sum(row) / len(row) for row in self.runs if row]

    def pass_at_1(self) -> float:
        rates = self.per_case_rates()
        return sum(rates) / len(rates) if rates else 0.0

    def pass_hat_k(self, k: int) -> float:
        """题目级别的 Pass^k：一题在 k 次里全部通过的题数占比。"""
        eligible = [row for row in self.runs if len(row) >= k]
        if not eligible:
            return 0.0
        return sum(1 for row in eligible if all(row[:k])) / len(eligible)

    def pass_at_k_any(self, k: int) -> float:
        """题目级别的 Pass@k：一题在 k 次里至少通过一次的题数占比。"""
        eligible = [row for row in self.runs if len(row) >= k]
        if not eligible:
            return 0.0
        return sum(1 for row in eligible if any(row[:k])) / len(eligible)

    def total_cost(self) -> float:
        return sum(sum(row) for row in self.costs if row)

    def summary(self) -> dict[str, float]:
        p = self.pass_at_1()
        n = len(self.runs)
        low, high = confidence_interval(p, n)
        data = {
            "cases": float(n),
            "runs_per_case": float(self.k),
            "pass@1": p,
            "ci95_low": low,
            "ci95_high": high,
            "standard_error": standard_error(p, n),
        }
        for k in (3, 5):
            if self.k >= k:
                data[f"pass^{k}"] = self.pass_hat_k(k)
                data[f"pass@{k}"] = self.pass_at_k_any(k)
        return data


def format_summary(summary: dict[str, float]) -> str:
    lines = [
        f"题数            {int(summary['cases'])}",
        f"每题运行次数    {int(summary['runs_per_case'])}",
        f"Pass@1          {summary['pass@1']:.1%}  "
        f"(95% CI {summary['ci95_low']:.1%} ~ {summary['ci95_high']:.1%})",
        f"标准误          ±{summary['standard_error']:.1%}",
    ]
    for key in ("pass^3", "pass^5", "pass@3", "pass@5"):
        if key in summary:
            label = key.replace("^", "^").ljust(15)
            lines.append(f"{label} {summary[key]:.1%}")
    return "\n".join(lines)


# =============================================================================
# 抽样组合（用于重复采样校正）
# =============================================================================


def at_least_one_estimate(successes: int, total: int, k: int) -> float:
    """从 n 次观测里估计「k 次至少一次成功」的无偏概率（超几何组合法）。

    仅在 ``total <= successes`` 或样本很小时使用更精确的组合估计，
    避免直接用 ``1-(1-p)^k`` 在小样本上偏乐观。
    """
    if total <= 0 or k > total:
        return 0.0
    if successes <= 0:
        return 0.0
    if total - successes < k:
        return 1.0
    all_fail = 1.0
    for i in range(k):
        all_fail *= (total - successes - i) / (total - i)
    return 1.0 - all_fail


def combinations_count(n: int, k: int) -> int:
    return len(list(combinations(range(n), k))) if n >= k else 0
