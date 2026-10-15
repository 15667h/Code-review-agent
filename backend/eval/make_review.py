"""生成人工复核记录 `eval/review.json`。

为什么需要这一步：自动匹配器只用「同文件 + 行号在容差内」判定命中，
在两头都会出错 ——
- 同一条缺陷横跨多条语句时，模型指向起点、标注落在操作行，会被算成「一漏一多」；
- 模型报了一处**标注集里根本没有的真缺陷**时，会被算成误报；
- 两条不同缺陷恰好相隔两行时，还会互相「顶替」。
所以指标必须同时给两套：自动口径（可复现、无主观）与人工复核口径（我逐条判定，
判定理由全部写进 JSON，用户可以不同意并自己重判）。

本脚本只负责把 bench-results.json 里的每个预测/每条标注摊平成待判定条目，
并套上下面 `VERDICTS` 里我逐条给出的结论。判定标准写在 `CRITERIA` 里。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "eval" / "bench-results.json"
OUT = ROOT / "eval" / "review.json"

# 判定标签的含义
CRITERIA = {
    "hit": "自动匹配命中，且人工复核认可为同一缺陷。",
    "merged": "模型在同一条发现里合并描述了多条标注 / 多个缺陷点，内容确实覆盖到了。",
    "placement": "同一条缺陷，模型指向的位置与标注不同（多为多行语句的起点与操作行之差）。",
    "key_gap": "模型报的是**真缺陷**，但标注集没收录 —— 属于答案键遗漏，不是模型报错。",
    "partial": "可争议：确有其事，但算不算缺陷取决于设计约定或加固等级，人工判为多报。",
    "true_fp": "明确的误报：所指代码不存在该问题。",
    "true_miss": "明确的漏报：模型既没定位到，完整发现里也没有等价描述。",
    "false_credit": "该预测被自动匹配算作命中，但它描述的其实是另一处缺陷，属于错误计入。",
}

# (样本, 文件, 行号) → (判定, 理由)。
# 行号取模型发现所报的行号；标注漏报则取标注行号。
VERDICTS: dict[tuple[str, str, int], tuple[str, str]] = {
    # ---- L1 s1 ----
    ("s1-obvious-danger", "app/settings.py", 5): (
        "merged",
        "这条发现明确写了「DEPLOY_KEY 与 DB_PASSWORD 两个生产环境密钥…（第 5、6 行）」，"
        "一条报告覆盖了 SEC010-a 与 SEC010-b 两条标注，属合并描述。",
    ),
    ("s1-obvious-danger", "app/settings.py", 13): ("hit", "命中 SEC011-a（shell 注入）。"),
    ("s1-obvious-danger", "app/settings.py", 19): ("hit", "命中 SEC012-a（eval）。"),
    ("s1-obvious-danger", "app/hooks.py", 6): ("hit", "命中 SEC013-a（exec）。"),
    ("s1-obvious-danger", "app/settings.py", 6): (
        "merged",
        "该标注被上面 settings.py:5 那条发现一并覆盖（原文点名了 5、6 两行）。"
        "按逐条口径算漏报，按合并口径算覆盖。",
    ),
    # ---- L2 s2 ----
    ("s2-resources-errors", "app/pool.py", 24): ("hit", "命中 RES011-a（提前 return 跳过 release）。"),
    ("s2-resources-errors", "app/exporter.py", 11): ("hit", "命中 RES010-a（未用 with）。"),
    ("s2-resources-errors", "app/exporter.py", 23): ("hit", "命中 ERR010-a（裸 except）。"),
    ("s2-resources-errors", "app/exporter.py", 34): ("hit", "命中 ERR011-a（异常吞掉后返回 0）。"),
    ("s2-resources-errors", "app/exporter.py", 14): (
        "key_gap",
        "CSV 字段未转义且存在公式注入（值以 = + - @ 开头会被表格软件当公式执行）。"
        "rows 来自外部数据，是实打实的缺陷 —— 我的标注集只标了「未用 with」，漏收了这一处。",
    ),
    # ---- L3 s3 ----
    ("s3-input-paths", "app/files.py", 13): ("hit", "命中 PATH01-a（路径穿越）。"),
    ("s3-input-paths", "app/files.py", 20): ("hit", "命中 DESER01-a（pickle）。"),
    ("s3-input-paths", "app/files.py", 25): ("hit", "命中 DESER02-a（yaml.load 无 Loader）。"),
    ("s3-input-paths", "app/files.py", 30): ("hit", "命中 SSRF01-a（未限制目标地址）。"),
    # ---- L4 s4 ----
    ("s4-concurrency", "app/cache.py", 15): ("hit", "命中 CONC01-a（无锁惰性单例）。"),
    ("s4-concurrency", "app/cache.py", 20): ("hit", "命中 CONC02-a（check-then-act）。"),
    ("s4-concurrency", "app/cache.py", 38): ("hit", "命中 CONC04-a（读-改-写丢更新）。"),
    ("s4-concurrency", "app/cache.py", 29): (
        "placement",
        "与 CONC03-a 是同一处：模型从竞态窗口的读起点（len(used)）切入，"
        "标注落在写操作（used.add）。模型把三步交错完整推演了出来，判定为同一缺陷。",
    ),
    ("s4-concurrency", "app/cache.py", 32): (
        "placement",
        "CONC03-a 被上面 cache.py:29 那条发现覆盖，只是行号差 3 行超出默认容差。",
    ),
    # ---- L5 s5 ----
    ("s5-boundaries", "app/limits.py", 11): ("hit", "命中 BOUND01-a（闭区间重复计入）。"),
    ("s5-boundaries", "app/limits.py", 16): ("hit", "命中 BOUND02-a（返回负配额）。"),
    ("s5-boundaries", "app/limits.py", 21): ("hit", "命中 BOUND03-a（页码基准错位）。"),
    ("s5-boundaries", "app/limits.py", 26): ("hit", "命中 BOUND04-a（whole=0 除零）。"),
    ("s5-boundaries", "app/limits.py", 31): ("hit", "命中 BOUND05-a（含尾区间少算一天）。"),
    ("s5-boundaries", "app/limits.py", 36): ("hit", "命中 BOUND06-a（时区换算未归一化）。"),
    # ---- L6 s6 ----
    ("s6-authz-logic", "app/orders.py", 12): ("hit", "命中 AUTHZ01-a（越权读取）。"),
    ("s6-authz-logic", "app/orders.py", 17): ("hit", "命中 AUTHZ02-a（状态守卫只挡 shipped）。"),
    ("s6-authz-logic", "app/orders.py", 30): ("hit", "命中 AUTHZ04-a（退款金额无校验）。"),
    ("s6-authz-logic", "app/orders.py", 16): (
        "key_gap",
        "cancel 完全不使用 user_id，任意用户可取消他人订单 —— 真缺陷，"
        "与「状态守卫只挡 shipped」是两回事，标注集只收了后者。",
    ),
    ("s6-authz-logic", "app/orders.py", 22): (
        "key_gap",
        "can_view 定义了权限判定却全仓无调用点，且是默认放行语义 —— 真缺陷，标注集未收录。",
    ),
    ("s6-authz-logic", "app/orders.py", 25): (
        "true_miss",
        "AUTHZ03-a 的具体问题是「global 权限直接放行、静默覆盖 CLIENT_DENY」。"
        "模型报了同一个函数的默认放行与未被调用，但没有点出 global 分支这条具体路径，"
        "按逐条口径判为真漏报。",
    ),
    # ---- L7 s7 ----
    ("s7-crossfile-flow", "app/repository.py", 12): ("hit", "命中 FLOW001-a（SQL 注入）。"),
    ("s7-crossfile-flow", "app/repository.py", 18): ("hit", "命中 FLOW002-a（表名注入）。"),
    ("s7-crossfile-flow", "app/repository.py", 17): (
        "key_gap",
        "path 直接来自请求体，构成任意文件写入 —— 真缺陷，标注集未收录。"
        "该条被自动匹配算作「冗余」（因为离 FLOW002-a 只有 2 行且已被消费），属误判。",
    ),
    ("s7-crossfile-flow", "app/repository.py", 20): (
        "key_gap",
        "export 无条件 return 0，上层把它当行数返回，接口契约不成立 —— 真缺陷，标注集未收录。",
    ),
    ("s7-crossfile-flow", "app/repository.py", 8): (
        "partial",
        "模型称「连接无释放路径构成泄漏」。sqlite 连接在 Repository 上常驻是常见写法，"
        "是否算泄漏取决于上层生命周期约定，本样本里没有证据表明它被反复创建。"
        "判定为**可争议多报**，从严计入误报。",
    ),
    # ---- L8 s8 ----
    ("s8-crypto-protocol", "app/tokens.py", 18): (
        "merged",
        "这条发现同时覆盖了三件事：签名不含密钥（CRYPTO03-a）、verify 忽略 public_key "
        "（CRYPTO04-a 的实质）、以及 L7 的 SIGNING_SECRET 在全文件从未被引用（CRYPTO01-a）。"
        "一条报告覆盖三条标注。",
    ),
    ("s8-crypto-protocol", "app/tokens.py", 12): ("hit", "命中 CRYPTO02-a（MT 可预测）。"),
    ("s8-crypto-protocol", "app/tokens.py", 31): (
        "placement",
        "与 CRYPTO05-a 是同一处：模型指向密钥流派生行，标注落在多行表达式的收尾行。"
        "模型指的位置更准确。",
    ),
    ("s8-crypto-protocol", "app/tokens.py", 15): (
        "key_gap",
        "sign 不注入 exp/jti，verify 也不做时效判定，重放防护缺失 —— 真缺陷，"
        "标注集未收录（属设计层面的缺失，从严可争议）。",
    ),
    ("s8-crypto-protocol", "app/tokens.py", 24): (
        "key_gap",
        "sign 用 body + '.' + digest 拼接、verify 却按**第一个** '.' 切分，"
        "payload 内含 '.' 时自己签发的令牌会被自己拒绝 —— 真缺陷，标注集未收录。"
        "注意：该条被自动匹配算作命中 CRYPTO04-a，是错误计入（见 false_credit）。",
    ),
    ("s8-crypto-protocol", "app/tokens.py", 26): (
        "partial",
        "非常量时间比较。模型自己在发现里写明「在当前实现下不构成可利用漏洞，"
        "是修复签名后的加固项」。属加固建议而非缺陷，从严计入误报。",
    ),
    ("s8-crypto-protocol", "app/tokens.py", 7): (
        "merged",
        "CRYPTO01-a（SIGNING_SECRET 硬编码且从未被引用）的实质被 tokens.py:18 那条发现覆盖，"
        "原文点名了「文件顶部定义的 SIGNING_SECRET（L7）在全文件中从未被引用」。",
    ),
    ("s8-crypto-protocol", "app/tokens.py", 34): (
        "placement",
        "CRYPTO05-a 被上面 tokens.py:31 那条发现覆盖，只是行号差 3 行。"
        "标注落在多行表达式收尾行，模型指的起点更准确 —— 这一处是我的标注位置不理想。",
    ),
}

# 自动匹配算作命中、但实际描述的是另一处缺陷的条目。
FALSE_CREDITS = [
    {
        "sample": "s8-crypto-protocol",
        "prediction": "app/tokens.py:24",
        "credited_label": "CRYPTO04-a @ app/tokens.py:26",
        "rationale": "两者相距 2 行被自动判为命中，但 24 行讲的是 '.' 切分不对称，"
        "26 行讲的是非常量时间比较，不是同一件事。CRYPTO04-a 的实质由 tokens.py:18 那条覆盖。",
    }
]


def main() -> int:
    bench = json.loads(BENCH.read_text(encoding="utf-8"))
    samples = []
    missing: list[str] = []

    for row in bench["results"]:
        name = row["sample"]
        run = row["runs"][0]
        entries = []

        for item in run["findings"]:
            key = (name, item["file"], item["line"])
            verdict, rationale = VERDICTS.get(key, ("", ""))
            if not verdict:
                missing.append(f"{name} 预测 {item['file']}:{item['line']}")
            entries.append(
                {
                    "role": "prediction",
                    "file": item["file"],
                    "line": item["line"],
                    "severity": item["severity"],
                    "title": item["title"],
                    "verdict": verdict,
                    "rationale": rationale,
                }
            )

        for item in run["missed_detail"]:
            key = (name, item["file"], item["line"])
            verdict, rationale = VERDICTS.get(key, ("", ""))
            if not verdict:
                missing.append(f"{name} 漏报 {item['code']} @ {item['file']}:{item['line']}")
            entries.append(
                {
                    "role": "missed_label",
                    "code": item["code"],
                    "file": item["file"],
                    "line": item["line"],
                    "description": item["description"],
                    "verdict": verdict,
                    "rationale": rationale,
                }
            )

        samples.append(
            {
                "sample": name,
                "tier": row["tier"],
                "labels": len(row["labels"]),
                "auto": {
                    "recall": run["recall"],
                    "precision": run["precision"],
                    "f1": run["f1"],
                },
                "entries": sorted(entries, key=lambda e: (e["file"], e["line"])),
            }
        )

    if missing:
        raise SystemExit("以下条目缺少判定，请补 VERDICTS：\n  " + "\n  ".join(missing))

    payload = {
        "criteria": CRITERIA,
        "note": (
            "判定由我（助手）逐条给出，理由写在 rationale 里。用户可以不同意并自行重判 —— "
            "这份文件的作用是让复核过程可审计，而不是把某个数字定成事实。"
        ),
        "false_credits": FALSE_CREDITS,
        "samples": samples,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"已写入 {OUT}：{len(samples)} 个样本 / "
          f"{sum(len(s['entries']) for s in samples)} 条判定")
    print()
    for sample in samples:
        counts: dict[str, int] = {}
        for entry in sample["entries"]:
            counts[entry["verdict"]] = counts.get(entry["verdict"], 0) + 1
        out = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"  {sample['sample']:<22} {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
