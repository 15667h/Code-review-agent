"""CI 冒烟样本：故意包含两类问题，用来验证 Margin 的 PR 集成链路。

**这个文件不参与产品逻辑**，只用于真实 PR 上验证（放在 scripts/ 而不是 tests/ —— 测试文件会被规则豁免）：
1. 规则引擎能抓到的问题（硬编码密钥）
2. 规则抓不到、需要模型推理的问题（越权读取）

验证方法：开 PR 后看 Security → Code scanning 是否出现这两个条目，
以及 PR 评论里是否贴出摘要。
"""

from pathlib import Path

# 预期 SEC001 命中（变量名含 api_key + 字面量）
API_KEY = "sk_live_smoke_test_should_be_reported_12345"


def load_report(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def get_order(store: dict, order_id: str, requester: str) -> dict:
    """预期：规则抓不到，但存在越权 —— 不校验 requester 是否为订单所有者。

    这类问题只有模型推理能发现，正好用来验证 AI 审查那一段链路。
    """
    return store[order_id]
