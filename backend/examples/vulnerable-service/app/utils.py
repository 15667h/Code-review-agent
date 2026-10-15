"""示例模块：工具函数，故意包含可变默认参数与不安全求值。

标注写在缺陷行的行尾，格式 `# 缺陷 编号：说明`。
"""

import json


def append_event(event: dict, target: list = []) -> list:  # 缺陷 PY001-a：可变对象作为默认参数
    """把事件追加到列表。

    target 在多次调用之间共享，第二次调用会看到第一次追加进去的元素。
    """
    target.append(event)
    return target


def build_index(items: list = {}) -> dict:  # 缺陷 PY001-b：字典作为默认参数同样共享
    """构建索引。"""
    for index, item in enumerate(items):
        items[index] = (index, item)
    return items


def parse_payload(text: str):
    """解析一段负载。"""
    return eval(text)  # 缺陷 SEC003-a：eval 执行任意表达式


def parse_config(text: str) -> dict:
    """解析配置 JSON。"""
    namespace: dict = {}
    exec(text, namespace)  # 缺陷 SEC003-b：exec 在任意输入上执行
    return namespace


def dumps_compact(payload: dict) -> str:
    """紧凑序列化。"""
    return json.dumps(payload, separators=(",", ":"))
