# codegraph 与跨 Agent 记忆 · 开源选型报告

> 检索时间 2026-10-24。所有项目的 star / license / 归档状态**均用 GitHub API 实查**，
> 不采信二手文章里的数字（实测发现有文章把 0 star 的镜像当成正主）。

---

## 一、结论先说

**两个模块都不要直接引入现成开源项目，但都值得「读其设计、抄其表达、自己写核心」。**

原因不是「造轮子爽」，而是三条硬约束：

| 约束 | 本项目现状 | 现成项目的冲突 |
|---|---|---|
| **零依赖定位** | 平台核心（`harness/`、`eval/`）只用标准库，唯一外部依赖是 `httpx` | 候选全部需要大量依赖，有的还要外部数据库 |
| **可审计** | 「这个服务本身也是要被审计的代码，依赖越少越好审」 | tree-sitter + 十几个语法包 = 审不过来 |
| **离线可用** | 有 `--offline` 模式、8 个靶场要能脱离模型跑 | 多数项目依赖 LLM 做语义 pass |

但**记忆模块**有一个例外判断：它根本不需要外部库（见第四节）。

---

## 二、codegraph 候选（实查数据）

| 项目 | Stars | 语言 | 协议 | 依赖代价 | 结论 |
|---|---|---|---|---|---|
| `Graphify-Labs/graphify` | **134,080** | Python | Apache-2.0 | networkx + graspologic + tree-sitter + **14 个语法包** | ❌ 太重 |
| `colbymchenry/codegraph` | **75,157** | C | MIT | 自带运行时，单二进制，无需编译 | ⚠️ 分发问题 |
| `CodeGraphContext` | 4,352 | Python | MIT | **neo4j 强制** + watchdog + typer + tree-sitter 全家桶 | ❌ 强制外部图库 |
| `parcadei/llm-tldr` | 1,169 | Python | AGPL-3.0 | — | ❌ **已归档**，不能用 |
| `LakshyaB-1/Codebase-RAG-MCP` | 小 | Python | — | sentence-transformers + ChromaDB + 向量库 | ❌ 引向量库 |

### 逐个说清为什么不选

**1. `graphify`（134K star，最热）**

设计很对——**正是我上一份报告推荐的「AST + 图，不要向量库」**：
tree-sitter 确定性抽边 → NetworkX 建图 → Leiden 社区检测（靠图拓扑，不需要 embedding）→
每条边标 `EXTRACTED` / `INFERRED` / `AMBIGUOUS`（**永远知道哪些是读出来的、哪些是推出来的**）。

但它的依赖清单是：

```
networkx, graspologic, tree-sitter,
tree-sitter-python, javascript, typescript, go, rust, java,
c, cpp, ruby, c-sharp, kotlin, scala, php   ← 14 个语法包
```

把这些塞进一个「自称只用标准库」的项目，等于自毁评审时的说服力。
而且它本质是个 **Claude Code skill**，靠 `/graphify` 触发 + 写 `CLAUDE.md`/`AGENTS.md`，
不是给你 `import` 的库。

**值得抄的三样**（代码不用抄，概念直接拿）：
① 边分三类 `EXTRACTED / INFERRED / AMBIGUOUS` + 置信度；
② Leiden 社区检测替代 embedding 做聚类（**这条最关键**，证明不引向量库也能聚类）；
③ 把注释里的 `# NOTE:` / `# WHY:` / ADR 引用提升成节点，连带设计理由进图。

**2. `codegraph`（75K star，C 写的）**

实测数据最硬的一个：7 个真实仓库、7 种语言，同一问题「有图 vs 无图」对比，
**成本降 44%、token 降 62%**。原理很朴素：强模型在没有图时会烧预算重新推导结构。

但它是 C 单二进制 + 自动化安装脚本（`curl | sh`），把 MCP server 注入到各种 agent 配置里。
对竞赛项目来说两个问题：
- 「自带运行时、安装脚本改你的 PATH 和 agent 配置」——评审会问这是什么；
- 我们自己的 server 要调它，就得依赖一个外部二进制，破坏「`pip install` 就能跑」。

**值得抄的**：它的 **impact 深度环**表达（d=1 必坏 / d=2 可能受影响 / d=3 需测试），
以及 R 语言支持那次提交里的**验证方式**——「headless agent benchmark：
有图时跨文件问题零次文件读取，无图时 6–28 次 read/grep」。这个对比方式我们可以直接照搬。

**3. `CodeGraphContext`（4.3K star）**

功能对齐度最高（`cgc` 有 watch 实时更新、预索引包、MCP + CLI 双模式），
但 `pyproject.toml` 里 **`neo4j>=5.15.0` 是必装**，没有内存/SQLite 降级。
为一个「单机竞赛项目」跑 Neo4j 服务，成本与收益完全不匹配。

**踩坑记录**：我先前搜到 `bitbottrap/CodeGraphContext` 并写了引用，
API 一查 **0 star**——是镜像。正主是 `CodeGraphContext/CodeGraphContext`。
**二手文章里的仓库地址必须核实**，这条要记住。

**4. `llm-tldr`**

架构最漂亮的一个——**五层分析**：AST → 调用图 → CFG → DFG → PDG，
宣传「95% token 节省」，正好是我们想要的分层。

**但它 `archived: true`，且 AGPL-3.0（传染性协议）。**
归档项目不能进生产，AGPL 与项目现有 MIT 不兼容。**只能读设计，不能用代码。**

---

## 三、codegraph 的建议做法

**自己写，但只写最小内核。** 工作量比上一份估的少，因为没有语义层。

### 为什么能做到纯标准库

实测验证（Python 3.13）：

```python
import ast
# 一次 walk 就能拿到全部四种边
FunctionDef / ClassDef  → 节点
Import / ImportFrom     → IMPORTS 边
Call                    → CALLS 边（被调名可从 n.func 推）
```

`ast` 是标准库，**不需要 tree-sitter**。tree-sitter 的价值只在「多语言」——
而这正好对应我们「只支持 Python」的现状：**先做 Python 版本的图，纯标准库，
不破坏定位；多语言作为可选后端（有 tree-sitter 就用，没有就降级）。**

### 最小可落版本

```
harness/codegraph.py
  · index(root) → SQLite（.runs/codegraph/<repo_hash>.db）
      表 nodes(id, kind, name, file, line, col)
      表 edges(src, dst, kind, confidence)   kind ∈ CALLS/IMPORTS/DEFINES/TESTED_BY
  · 四个查询（对应上一份报告的四个工具）
      who_calls(symbol) → [{file, line}]
      impact(symbol, depth) → 按深度环分组
      tests_for(symbol) → 覆盖它的测试函数
      detect_changes(diff) → 受影响符号集
```

**四个必须抄的设计决定**：

1. **边分 `EXTRACTED` / `INFERRED`**，并给 INFERRED 打置信度
   （静态解析拿不到动态分发，只能推断；不标出来就是在骗人）
2. **`impact` 用深度环返回**，不要返回一个平铺的文件列表
3. **节点只存 span**（file + line + col），正文按需读盘 —— 索引小、不存重复内容
4. **增量更新**：只重解析 mtime/hash 变了的文件

### 必须诚实说明的失效模式（写进文档）

- Python 的**动态调用**（`getattr`、注册表、装饰器包装）静态解析拿不到 → 标 `INFERRED` 或不标
- **跨文件符号解析**：`from x import y` 要知道 `x` 是哪个文件，需要模块搜索路径推断，会猜错
- **重载/同名符号**：同一个 `run()` 在三个类里，`who_calls` 分不清是谁

这三条不写清楚，`impact` 给出的结果会被当成保证——那就是上一份报告警告的「近失方案」。

---

## 四、跨 Agent 记忆：**不要引入现成记忆库**

### 为什么现成的都不合适

| 项目 | Stars | 为什么不适合 |
|---|---|---|
| `mem0ai/mem0` | 67,725 | 定位是「**会话/用户个性化**」的对话记忆，输入是 chat 消息。我们没有 chat 场景 |
| `vectorize-io/hindsight` | 53,271 | 同上，LongMemEval 高分但那是「跨会话问答」基准 |
| `getzep/graphiti` | 32,176 | 时序知识图，强在「三月时什么是真的」。我们要的是「这个仓库已知什么」 |
| `letta-ai/letta` | 25,241 | 整套 agent runtime（带自己的 server + Postgres），等于换掉我们的 harness |
| `rohitg00/agentmemory` | 29,845 | **TypeScript**，Node 生态；靠 12 个 hook 拦 Claude Code 的生命周期 |

**关键认知**：这些全部是「**会话记忆**」——记的是「上次对话说了什么」。
我们要的是「**仓库记忆**」——记的是「这个仓库有哪些已知缺陷、哪些计划、哪些测试」。
**是两种东西。**

`agentmemory` 的 README 自己划了这条线，写得很清楚：

> Graphify 记代码结构；**agentmemory 记工作过程**（session memory）。
> 它不记「代码是什么」，只记「上次干了什么」。

我们要的第一层（代码结构）对应 codegraph，第二层（关于这个仓库的结论）——
两者都不是它。硬接会得到一个「记错东西」的记忆层。

### 你的项目已经具备了做这件事的全部原料

实测 `.runs/*.json` 的归档结构：

```
run_id / repo / repo_name / goal / agent / status / elapsed
result.artifacts / counts / artifact_counts / stop_reason / iterations
result.usage / cache_hit_ratio / findings / summary / evidence_report
```

**`repo` + `agent` + `artifacts` 这三个字段已经够了。**
按 repo 聚合所有 run 的产出，就是「这个仓库的已知情况」。不需要 embedding，不需要向量库。

### 建议做法：`harness/memory.py`（纯标准库）

```
memory.py
  · aggregate(repo) → 读该 repo 的所有 run 归档
      → 已知缺陷（finding 去重：文件+行号±2 + 规则 id）
      → 已提过的计划 / 已建的分层（blueprint）
      → 历史指标序列（iterations / elapsed / cache_hit / counts）
  · brief(repo, agent, budget) → 生成注入用的上下文片段（有 token 预算）
  · 提供两个查询工具：history(repo) / known_issues(symbol_or_file)
```

**四条设计纪律**（都是从上一次调研的坑里来的）：

1. **去重必须有指纹**。同一缺陷在不同 run 里行号会漂，用「文件 + 规则 id + 归一化描述 hash」当指纹，不要用行号
2. **注入要有预算且显式**。参照行业默认值（agentmemory 默认 2000 token），
   超预算就截断，并**在上下文里写明「以下是历史摘要，可能过期」**
3. **必须能关**。有人在 HackerNews 上尖锐批评「coding agent 根本不需要记忆」，
   理由是「不透明的自动注入会让 agent 基于过期前提行动」。所以：开关 + 可查看 + 可删除
4. **趋势比记忆本身更有价值**。同一仓库多次运行的缺陷数/耗时曲线，
   能直接变成报告里一个新指标（「代码库在变好还是变坏」）——
   **这是行业承认的空白**：「现在的 review 工具没有记忆，你无法看出代码库在变好还是变坏」

### 一个反直觉但重要的取舍

**不要做「LLM 压缩摘要」**。

`agentmemory` / `hindsight` 都用 LLM 把观察压成摘要。我们的场景**不需要**：
- 我们的 run 产出已经是**结构化**的（finding 有 severity / file / line / rule_id），
  结构化数据不需要摘要成自然语言再喂回去——那是**信息损失**；
- 用 LLM 压缩意味着每次记忆写入都要调模型，成本与不确定性都上升；
- 而且摘要一旦错了，后续所有基于它的判断都错，且**看不出来**。

**正确做法：结构化数据直接按字段拼进上下文，让模型自己读。**
摘要只在「一次 run 有 200 条事件」这种原始轨迹上做，而且用确定性规则（截断 + 只留 tool 名与参数），不用模型。

---

## 五、最终建议

| 模块 | 做法 | 依赖变化 | 工作量 |
|---|---|---|---|
| **codegraph** | 自己写，Python 版用标准库 `ast`，SQLite 存；tree-sitter 作为**可选**多语言后端 | **+0** | 600–900 行 |
| **跨 Agent 记忆** | 自己写，按 repo 聚合现有 run 归档，结构化注入 | **+0** | 300–400 行 |

**为什么不引入开源项目** —— 一句话：
> 这两个模块的**价值在「与现有结构的贴合」**，不在算法复杂度。
> `ast` 抽边、按 repo 聚合 JSON —— 都是几十行核心逻辑的事。
> 引入 14 个语法包或一个 Neo4j，换来的不是能力，是评审时要解释的一堆依赖。

**该抄的（明确列出，防止以后找不到）**：

| 来源 | 抄什么 |
|---|---|
| `graphify` | 边分 `EXTRACTED/INFERRED/AMBIGUOUS` + 置信度；Leiden 社区检测（证明无 embedding 也能聚类） |
| `codegraph` | `impact` 的深度环表达（d=1/2/3）；「有图 vs 无图」的 token/调用次数对比验证法 |
| `llm-tldr` | 五层分析的分层命名（AST→Call→CFG→DFG→PDG），**只读设计**（已归档 + AGPL） |
| `agentmemory` | 「会话记忆 ≠ 代码结构记忆」的边界划分；注入预算默认值 2000 token |
| `CodeGraphContext` | `watch` 增量更新的做法；预索引包思路 |

---

## 六、实查数据附录（2026-10-24，GitHub API）

| 仓库 | Stars | 语言 | 协议 | archived |
|---|---|---|---|---|
| Graphify-Labs/graphify | 134,080 | Python | Apache-2.0 | 否 |
| colbymchenry/codegraph | 75,157 | C | MIT | 否 |
| mem0ai/mem0 | 67,725 | Python | Apache-2.0 | 否 |
| vectorize-io/hindsight | 53,271 | Python | MIT | 否 |
| getzep/graphiti | 32,176 | Python | Apache-2.0 | 否 |
| rohitg00/agentmemory | 29,845 | TypeScript | Apache-2.0 | 否 |
| letta-ai/letta | 25,241 | — | Apache-2.0 | 否 |
| CodeGraphContext/CodeGraphContext | 4,352 | Python | MIT | 否 |
| parcadei/llm-tldr | 1,169 | Python | AGPL-3.0 | **是** |
| bitbottrap/CodeGraphContext | 0 | — | MIT | 否（**镜像，勿用**） |

---

## 七、参考来源

- [Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify) —— 「AST + 图，不要向量库」的最佳实例
- [colbymchenry/codegraph](https://github.com/colbymchenry/codegraph) —— 44% 成本下降 / 62% token 下降实测
- [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext) —— 功能最全，但强制 Neo4j
- [llm-tldr](https://github.com/parcadei/llm-tldr) —— 五层分析设计（已归档，只读）
- [rohitg00/agentmemory](https://github.com/rohitg00/agentmemory) —— 会话记忆，与仓库记忆不同层
- [Agent memory consolidation 对比](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/blog/2026-05-21-agent-memory-consolidation.md) —— Mem0/Zep/Letta 四杠杆对比
- [aiagentmemory.org：Graphify 与记忆的边界](https://aiagentmemory.org/articles/graphify-memory) —— 「graph 记代码，memory 记工作」的划分依据
