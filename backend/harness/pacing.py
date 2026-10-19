"""轮次预算：让模型知道「还剩几轮」，而不是撞到上限才发现没写完。

## 问题

现有结果里 E1 有一次 15 轮撞上限、E3 一次 24 轮撞上限，两次都是
**产出已经够用，但没来得及调用 finish**。这不是能力问题，是**没给模型看预算**：
它不知道自己在第 18 轮，也不知道再读两个文件就会用完。

## 做法

每轮把「进度」写进状态栏（status_note），分三档语气：

- 正常（剩余 > 40%）：只报数字，不打扰
- 收敛（剩余 20%–40%）：提醒「如果核心结论已成立，可以开始收尾」
- 危急（剩余 ≤ 20%）：明确要求「先 finish，未覆盖的写进开放问题」

刻意不用「强制中断」：中断会留下半份产出，而**半份产出比明确声明未完成更糟**
（评审会以为那是全部）。让模型自己决定在哪里截断，并如实写进 summary。

## 为什么给百分比而不是绝对轮数

不同任务的轮数预算不同（E1 给 14、E3 给 24）。绝对数字会让模型误判
「还有 6 轮很多」——在 14 轮的预算里，6 轮已经是剩下的一半了。
"""

from __future__ import annotations


def pacing_note(iteration: int, max_iterations: int) -> str:
    """按剩余预算生成一行提示。iteration 从 0 开始计。"""
    if max_iterations <= 0:
        return ""

    used = iteration + 1
    remaining = max_iterations - used
    if remaining <= 0:
        return ""

    ratio = remaining / max_iterations

    if ratio <= 0.2:
        return (
            f"【轮次预算】第 {used}/{max_iterations} 轮，仅剩 {remaining} 轮。"
            "请**立刻收尾**：核心结论已经成立的部分先 finish，"
            "没来得及验证的写进 open_question 并标明「未覆盖」。"
            "不要在这时再展开新的阅读。"
        )
    if ratio <= 0.4:
        return (
            f"【轮次预算】第 {used}/{max_iterations} 轮，剩余 {remaining} 轮。"
            "如果主要分层与组件已经清楚，可以开始收尾；"
            "剩余轮次优先留给「补证据」和「写代价」，而不是继续扩大阅读范围。"
        )
    return f"【轮次预算】第 {used}/{max_iterations} 轮，剩余 {remaining} 轮。"


def should_warn_finish(iteration: int, max_iterations: int) -> bool:
    """是否已经进入必须收尾的区间（供测试与 UI 使用）。"""
    if max_iterations <= 0:
        return False
    return (max_iterations - (iteration + 1)) / max_iterations <= 0.2
