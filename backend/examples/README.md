# 审计靶场（examples/）

这些目录是给 `harness` 用的**故意含缺陷的样例仓库**，用于回放、演示和评估。

## ⚠️ 凭据全部是合成的

样本代码里出现的 `DEPLOY_KEY` / `DB_PASSWORD` / `SIGNING_SECRET` 等字面量
**是随机编造出来的假值，不对应任何真实系统**。它们必须存在 ——
「生产凭据硬编码在源码里」这条缺陷如果写成 `PASSWORD = ""` 就无法被检出，
靶场也就失去了意义。

如果密钥扫描器对这些文件报警，属预期行为，请忽略。

## 目录布局（重要）

```text
examples/
  <样本名>/
    repo/            ← 审计根。Agent 只能看到这里的文件
      app/*.py
    labels.json      ← 人工标注（答案键）。在审计根之外，Agent 读不到
```

**标注绝不能放进 `repo/` 里。** 早期版本把 `# 缺陷 XXX：说明` 写在缺陷行的行尾，
等于把答案印在题目下面 —— 模型 6/8 样本直接拿到 R=P=100%，那一批结果已作废
（存档在 `eval/bench-results-contaminated.json`）。
回归测试 `tests/test_harness.py::test_audit_roots_do_not_leak_labels` 守住这条边界。

## 难度分层

| 层 | 考的能力 | 样本 |
|---|---|---|
| L1–L2 | 认识危险 API、资源与错误处理模式 | `s1-obvious-danger`、`s2-resources-errors` |
| L3–L4 | 追踪数据来源、构造并发交错 | `s3-input-paths`、`s4-concurrency` |
| L5–L6 | 自己立前提、自己建模型 | `s5-boundaries`、`s6-authz-logic` |
| L7–L8 | 跨文件跟数据流、掌握密码学性质 | `s7-crossfile-flow`、`s8-crypto-protocol` |
| — | 早期综合样本（同样已去污染） | `vulnerable-service` |
| — | **中大规模样本**（8–17 文件 / 1300+ 行，含跨文件调用链与干扰项） | `m1-order-desk`、`m2-report-hub`、`m3-auth-gateway` |

### 为什么要有 m 系列

`s1`–`s8` 是**难度刻度**，不是**规模刻度**：它们平均只有 2 个文件、62 行，
s3–s8 全是单文件 31–44 行。这个规模下「跨文件跟数据流」实际上无从谈起 ——
`s7` 号称跨文件，也只有 3 个文件 54 行。

小样本上测出来的分数会系统性偏高。实测（2026-10-19）：
`vulnerable-service`（6 文件 / 231 行 / 23 条标注）在同一套口径下
F1 从 82%（8 个小靶场）掉到 70%。所以 `m1`–`m3` 刻意做到 **1300 行以上、8 个文件以上、
真实分层调用（api → service → repository → store）**，用来回答「放大 20 倍还站得住吗」。

三个 m 样本还刻意埋了**干扰项**（用了 `eval` 但输入是白名单常量、`subprocess` 但
`shell=False` 且参数是列表、`compare_digest` 但比较的是固定长度摘要），
它们**不在 labels 里** —— 报出来就是误报，用来测精确率。

## 怎么跑

```bash
cd backend

# 只跑确定性规则（不调模型）
python -m harness -r examples/s4-concurrency/repo --scan-only

# 用真实模型审计
python -m harness -r examples/s4-concurrency/repo \
  -g "先说明这些函数在什么样的线程交错顺序下会出错，再逐个确认实际实现是否存在该问题。"

# 全量实测 + 生成交付文档
python -m eval.run_bench --repeats 1 --out eval/bench-results.json
python -m eval.make_review
python -m eval.make_report          # → backend/BENCHMARK.md
```

每个样本的植入缺陷清单、提问原文与实测结果见 [`../BENCHMARK.md`](../BENCHMARK.md)。
