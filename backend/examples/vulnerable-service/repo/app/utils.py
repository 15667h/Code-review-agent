"""通用工具函数。"""

import json


def append_event(event: dict, target: list = []) -> list:
    """把事件追加到列表。

    target 在多次调用之间共享，第二次调用会看到第一次追加进去的元素。
    """
    target.append(event)
    return target


def build_index(items: list = {}) -> dict:
    """构建索引。"""
    for index, item in enumerate(items):
        items[index] = (index, item)
    return items


def parse_payload(text: str):
    """解析一段负载。"""
    return eval(text)


def parse_config(text: str) -> dict:
    """解析配置 JSON。"""
    namespace: dict = {}
    exec(text, namespace)
    return namespace


def dumps_compact(payload: dict) -> str:
    """紧凑序列化。"""
    return json.dumps(payload, separators=(",", ":"))
