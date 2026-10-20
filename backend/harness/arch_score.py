"""架构蓝图的量化评分（对标学术界的架构恢复指标）。

## 为什么需要这个

`arch-cases/RESULTS.md` 一直是「照妖镜定性通过」——能说明方案没跑偏，
但说明不了「比上一版好了多少」。学术界做架构恢复（Semref、ROS2 恢复）
都用 precision / recall / F1，理由很实在：**只有能算出比例，改进才可衡量。**

## 为什么必须做同义词归一

架构命名天然有自由度：同一个东西可以叫 `HookCaller` / `hook 调用器` / `调用器`。
按字面比会把它算成一漏一多 —— 那是**度量错误**，不是模型能力差。
（项目记忆里同源教训：`match_findings` 重复消费导致召回虚低 10pt。）
所以参考蓝图里每个条目都带 `aliases`，归一后匹配。

## 局限（必须说清）

1. **参考蓝图是人写的**，评分上限受它约束。写得粗 → 分数虚高；写得细 → 分数偏低。
2. 只测「结构是否正确」，**不测论证质量**（那条由 `arch_check` 的权衡检查兜）。
3. 组件的粒度判断（一个组件该不该拆）本身有争议，这里按「匹配到就算命中」处理。
"""

from __future__ import annotations

import re
from typing import Any


def _norm(text: object) -> str:
    """归一：去标点、去空白、小写。

    与 `normalize` 在 tools_design / blueprint.js 里的规则保持一致 ——
    三处若不同规则，就会出现「后端说匹配、前端说没有」的分裂。
    """
    raw = str(text or "").lower()
    return re.sub(r"[\s\-_/·。，,.:：（）()\[\]【】]+", "", raw)


def _name_set(entry: dict) -> set[str]:
    """一个参考条目的全部可接受写法。"""
    names = {_norm(entry.get("name"))}
    for alias in entry.get("aliases") or []:
        names.add(_norm(alias))
    names.discard("")
    return names


# 匹配用的语义单元。把复合名拆成这些片段再比 —— 
# 实测产出的名字是 `hook-caller` / `plugin-manager` / `执行内核` 这类形式，
# 直接字符串包含会把「hook 描述层」和「HookSpec」判成两个不相干的东西。
_UNITS = (
    "hookcaller", "caller", "调用器", "调用",
    "pluginmanager", "manager", "管理器", "插件管理",
    "hookspec", "spec", "规格", "契约",
    "hookimpl", "impl", "实现",
    "relay", "中继",
    "result", "结果",
    "wrapper", "包装",
    "subset", "子集",
    "execution", "执行内核", "multicall", "内核",
    "marker", "标记",
    "opt", "选项",
    "tracer", "追踪",
    "compat", "兼容", "shim", "旧名",
    "public", "门面", "api",
    "register", "注册",
)


def _units(text: str) -> set[str]:
    """把名字拆成可比较的语义单元。"""
    key = _norm(text)
    return {u for u in _UNITS if u in key}


def _match_score(
    produced: list[str],
    expected: list[dict],
) -> dict[str, Any]:
    """一组名字对一组参考条目的 P/R/F1。

    匹配规则（按强弱三级，越靠前越可信）：
    1. 归一后完全相等；
    2. 一方包含另一方（`Hook 调用器（HookCaller）` 这种复合名）；
    3. **共享语义单元**（`hook-caller` vs `HookCaller`、`执行内核` vs `execution core`）。

    第 3 级是必须的：架构命名天然自由，只靠字面比会把正确的切分算成漏报 ——
    那是度量错误，不是模型能力差。
    """
    used: set[int] = set()
    hits: list[tuple[str, str]] = []
    for raw in produced:
        key = _norm(raw)
        if not key:
            continue
        units = _units(raw)

        best: tuple[int, int] | None = None  # (强度, 索引)
        for index, entry in enumerate(expected):
            if index in used:
                continue
            names = _name_set(entry)
            if key in names:
                best = (3, index)
                break
            if any(name and (name in key or key in name) for name in names):
                best = (2, index)
                break
            entry_units: set[str] = set()
            for alias in [entry.get("name")] + list(entry.get("aliases") or []):
                entry_units |= _units(str(alias))
            if units and entry_units and (units & entry_units):
                if best is None or best[0] < 1:
                    best = (1, index)

        if best:
            used.add(best[1])
            hits.append((str(raw), str(expected[best[1]].get("name"))))

    matched = len(hits)
    precision = matched / len(produced) * 100 if produced else 0.0
    recall = matched / len(expected) * 100 if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    missed = [str(e.get("name") or e.get("title") or "?") for i, e in enumerate(expected) if i not in used]
    extra = [str(p) for p in produced if not any(_norm(p) == _norm(h) for h, _ in hits)]

    return {
        "produced": len(produced),
        "expected": len(expected),
        "matched": matched,
        "precision": round(precision, 1),
        "recall": round(recall, 1),
        "f1": round(f1, 1),
        "missed": missed,
        "extra": extra,
        "hits": hits,
    }


def _component_index(components: list[dict], reference: dict) -> dict[str, str]:
    """把产出的组件名映射到参考条目名，供依赖边比对使用。

    边比对必须在**同一套名字**上做：产出写「调用器」、参考写「HookCaller」，
    直接比边会全错。
    """
    mapping: dict[str, str] = {}
    for comp in components:
        raw = str(comp.get("name") or "")
        key = _norm(raw)
        for entry in reference.get("components") or []:
            names = _name_set(entry)
            if any(name and (name in key or key in name) for name in names):
                mapping[key] = str(entry.get("name"))
                break
    return mapping


def score_blueprint(
    artifacts: dict[str, list[Any]],
    reference: dict,
) -> dict[str, Any]:
    """对一份蓝图打分。返回各维度的 P/R/F1 与整体结论。"""
    def names(kind: str) -> list[str]:
        out: list[str] = []
        for item in artifacts.get(kind) or []:
            data = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            value = data.get("name") or data.get("title") or ""
            if value:
                out.append(str(value))
        return out

    layers = _match_score(names("layer"), reference.get("layers") or [])
    components = _match_score(names("component"), reference.get("components") or [])
    decisions = _match_score(names("decision"), reference.get("decisions") or [])

    # ---- 依赖边 ----
    comps = [
        (i.to_dict() if hasattr(i, "to_dict") else dict(i))
        for i in (artifacts.get("component") or [])
    ]
    mapping = _component_index(comps, reference)
    produced_edges: set[tuple[str, str]] = set()
    for comp in comps:
        src = mapping.get(_norm(comp.get("name")))
        if not src:
            continue
        for dep in comp.get("depends_on") or []:
            dst = mapping.get(_norm(dep))
            if dst and dst != src:
                produced_edges.add((src, dst))

    expected_edges = {
        (str(e.get("from")), str(e.get("to"))) for e in reference.get("edges") or []
    }
    edge_hits = produced_edges & expected_edges
    edge_p = len(edge_hits) / len(produced_edges) * 100 if produced_edges else 0.0
    edge_r = len(edge_hits) / len(expected_edges) * 100 if expected_edges else 0.0
    edge_f1 = 2 * edge_p * edge_r / (edge_p + edge_r) if (edge_p + edge_r) else 0.0

    edges = {
        "produced": len(produced_edges),
        "expected": len(expected_edges),
        "matched": len(edge_hits),
        "precision": round(edge_p, 1),
        "recall": round(edge_r, 1),
        "f1": round(edge_f1, 1),
        "missed": [f"{a} → {b}" for a, b in sorted(expected_edges - edge_hits)],
        "extra": [f"{a} → {b}" for a, b in sorted(produced_edges - edge_hits)],
    }

    # 整体：四个维度等权。刻意给结构（层+组件）更高权重 ——
    # 架构方案的核心是「切得对不对」，决策与边是支撑。
    overall = (
        components["f1"] * 0.35
        + layers["f1"] * 0.25
        + decisions["f1"] * 0.25
        + edges["f1"] * 0.15
    )

    # ---- 照妖镜：不该出现的东西 ----
    # 有些错误不是「漏了什么」，而是「多了一个不该有的东西」——比如把 requests
    # 画出一个重试层（重试其实在 urllib3）。这类错误单靠 P/R 抓不到：
    # 多报的组件只让精确率降一点，但它恰恰是「背架构图而不是读代码」的铁证。
    forbidden = reference.get("must_not_contain") or {}
    banned = [str(x) for x in (forbidden.get("components") or [])]
    violations: list[str] = []
    if banned:
        produced_names = [str(d.get("name") or "") for d in comps]
        for name in produced_names:
            key = _norm(name)
            for bad in banned:
                if _norm(bad) and _norm(bad) in key:
                    violations.append(name)
                    break

    return {
        "layers": layers,
        "components": components,
        "decisions": decisions,
        "edges": edges,
        "violations": sorted(set(violations)),
        "violation_note": forbidden.get("why", ""),
        "overall": round(overall, 1),
    }
