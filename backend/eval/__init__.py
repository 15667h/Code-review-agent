"""评估模块。

- ``metrics``  指标公式：Pass@k / Pass^k / 标准误 / McNemar 配对检验
- ``dataset``  评估样本与人工标注的加载、以及发现与标注的匹配
- ``run_eval`` 执行器：跑多次、算指标、给诊断
"""

from .dataset import EvalCase, ExpectedFinding, MatchOutcome, case_passed, load_default_cases
from .metrics import RunMatrix, mcnemar, pass_at_k, pass_pow_k, standard_error

__all__ = [
    "EvalCase",
    "ExpectedFinding",
    "MatchOutcome",
    "case_passed",
    "load_default_cases",
    "RunMatrix",
    "mcnemar",
    "pass_at_k",
    "pass_pow_k",
    "standard_error",
]
