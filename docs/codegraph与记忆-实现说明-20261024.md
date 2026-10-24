# codegraph 与跨 Agent 记忆 · 实现说明

> 2026-10-24 实现。选型依据见 `codegraph与记忆-开源选型-20261024.md`。
> **两个模块都是纯标准库实现，零新增依赖**（`requirements.txt` 未改动）。

---

## 一、新增了什么

| 文件 | 行数 | 作用 |
|---|---|---|
| `backend/harness/codegraph.py` | 697 | 仓库结构图：AST 抽取 → SQLite → 四个查询 |
| `backend/harness/tools_graph.py` | 401 | 把四个查询暴露成 Agent 工具 |
| `backend/harness/memory.py` | 450 | 跨 Agent 记忆：按仓库聚合历史运行 |
| `backend/tests/test_codegraph_memory.py` | 428 | 27 条行为测试 |
| `harness/loop.py` | +20 | `run_agent(remember=True)` 注入历史 |
| `harness/agents.py` | +25 | 两个 Agent 的工具箱挂上 codegraph |

回归测试：**310 passed**（原 283 + 新增 27）。

---

## 二、codegraph：怎么用、边界在哪

### 四个工具

| 工具 | 语义 | 典型问题 |
|---|---|---|
| `build_graph` | 建索引（默认跳过已有） | 第一次查询前必须先调 |
| `who_calls(symbol)` | 谁调用了它 | 「改这个函数签名会坏哪？」 |
| `impact(symbol, depth)` | 影响半径，按距离分三档 | 「这个改动波及多大？」 |
| `tests_for(symbol)` | 哪些测试覆盖它 | 「修好了该跑哪些测试？」 |

### 实测效果（m2 靶场，17 个文件）

```
节点：32 类 / 63 函数 / 76 方法 / 17 模块
边：  232 CALLS / 171 DEFINES / 50 IMPORTS
耗时：0.31 秒
```

真实查询——审计靶场里那个鉴权守卫 `_require_ops`：

```
who_calls(_require_ops) → 6 个调用方，全部标「直接读到」
  app.ops_api.handle_landing_files  (app/ops_api.py:30)
  app.ops_api.handle_probe          (app/ops_api.py:51)
  ...
```

**这直接证实了靶场里那个真实缺陷**：`_require_ops` 只被 `ops_api.py` 的 6 个接口用到，
而 `admin_api.py` 的三个运营接口**一次都没调**。grep 只能告诉你「这个名字出现过」，
图能告诉你「谁没用它」——这是两类不同的问题。

### 三条设计决定（都有实测理由）

**1. 边分 `EXTRACTED` / `INFERRED` + 置信度**

```
直接读到     conf=1.00   语法里明确写着（同一文件内调用、self.method）
import 解析  conf=0.85   通过 import 表解析到外部模块
同类方法     conf=0.70   在同类/父类里找到
裸名唯一     conf=0.45   全局只有一个同名符号，猜的
```

把推断的边当事实展示，就是「近失方案」：看起来给了影响范围，实际漏了关键调用点。

**2. `impact` 分深度环，不给平铺列表**

```
d=1 直接调用方（改了会坏）
d=2 间接依赖（很可能受影响）
d=3 传递影响（需要跑测试确认）
```

平铺 200 个文件等于噪音。措辞也是刻意的：d=1 说「一定」，d=2 说「很可能」，
d=3 说「需确认」——不要把推断当保证。

**3. 节点只存 span，正文按需回读**

索引小、能增量、不和仓库内容不同步。

### 必须知道的失效模式

这三条写进了工具描述里，模型每次调用都会看到：

1. **动态调用读不到**：`getattr` / 字典注册表 / 装饰器包装 / `**kwargs` 转发
2. **跨文件符号解析会猜错**：`from pkg.mod import name` 靠「模块路径 → 文件路径」推断，
   同名的多个包会错
3. **同名符号分不清**：三个类里都有 `run()`，请用限定名 `Class.run`

所以工具返回里都带一句：**「这不是『改这些就安全』的保证，只是缩小人工排查的范围」**。

---

## 三、跨 Agent 记忆：与「会话记忆」的区别

**这个模块不是 mem0 / agentmemory 那类会话记忆。** 区别用一句话说清：

| | 会话记忆（mem0 / hindsight / agentmemory） | 本模块 |
|---|---|---|
| 记什么 | 上次对话说了什么 | **这个仓库已知什么** |
| 输入 | chat 消息流 | run 归档（结构化） |
| 用途 | 跨会话个性化 | 跨 Agent 共享结论 |

行业里明确说这块是空白：「现在的 review 工具没有记忆，每次审查都独立，
你无法看出代码库在变好还是变坏。」

### 怎么做

```
run 归档（.runs/*.json，已有字段：repo / agent / result.artifacts / counts）
  → aggregate(repo)     按仓库聚合
  → 去重（指纹）          同一缺陷跨运行合并，记 seen_times
  → brief(repo, agent)  生成注入片段（默认 2000 token 预算）
```

### 四条纪律（每条都有踩坑依据）

**1. 去重指纹不含行号**

同一缺陷在两次运行里行号会漂。用行号当指纹的一部分，会把一条缺陷算成两条。

```
指纹 = hash(文件 + 规则id + 归一化描述)
归一化：去空白、数字替换成 #、小写
```

**2. 趋势只用「有效运行」——这条最关键**

实测发现归档里有 4 次 `status=finished / iterations=1 / findings=0` 的运行。
它们看起来是「正常完成、没发现问题」，实际是**模型第一轮就没调工具**
（当时用的中转端点不支持 function calling）。

如果不过滤，趋势会算出「缺陷数从 0 涨到 27」，
读者得出「代码库在恶化」的结论——而真相是**前几次根本没在审**。

**这是最坏的一类错误：用一个假基线算出真结论。**

判定规则：
```
status ∈ (failed, error, cancelled, stopped)  → 无效
iterations ≤ 1                                → 无效（没进入工具循环）
event_count ≤ 4                               → 无效（空转）
```

实际效果（m1 靶场 6 次运行）：
```
总运行 6 | 有效 1 | 去重缺陷 27
趋势：这个仓库只有 1 次**有效**运行记录，不足以判断趋势（至少需要 2 次）。
     （另有 5 次运行未计入：它们没进入工具循环或未正常结束）
```
**这是正确的结论。** 之前会输出「缺陷数在上升：0 → 27」—— 那是在说谎。

**3. 注入必须标「可能已过期」**

```
## 这个仓库的历史（自动汇总，**可能已过期**）
以下内容来自 N 次历史运行。它是**过去某次运行时的判断**，代码可能已经改了；
把它当作「值得复核的线索」，不要当作当前事实。
```

不标的话模型会把历史发现直接复述给用户，那是把过期信息当结论。

**4. 优先展示其他 Agent 的产出**

同一 Agent 自己的历史对它的增量最小（它本来就做类似的事）。
其他 Agent 的产出才是它的盲区——所以 `brief(mem, agent="review")` 会专门
列出 `architect` 在这个仓库上留下了什么。

### 为什么不做 LLM 压缩摘要

`agentmemory` / `hindsight` 都用 LLM 把观察压成摘要。这里**刻意不做**：

1. 我们的产出**本来就是结构化的**（finding 有 severity/file/line/rule_id）。
   压成自然语言再喂回去，是纯粹的信息损失。
2. 摘要错了后续判断全错，且**看不出来**——最坏的失败模式。
3. 每次写记忆都要调模型，成本和不确定性都上升。

所以：结构化字段直接拼进上下文，让模型自己读原始数据。
只在「原始事件轨迹」这种非结构化长文本上用**确定性规则**截断（留工具名与参数，砍正文）。

### 开关与可复现性

`run_agent(remember=True)`，**默认关闭**。三个理由：

1. 它会让同样的目标产生不同结果，破坏可复现性（8 靶场跑分要靠这个）
2. 历史可能过期，默认开着等于让模型默认相信旧结论
3. 公开讨论里有人明确批评过「不透明的自动注入会让 agent 基于过期前提行动」

---

## 四、顺带修掉的一个架构问题

接入 codegraph 时，`_enforce_read_only` 拦下了我：架构助手声明 `can_edit=False`，
但 `build_graph` 是 MEDIUM 风险（要写索引文件）。

**这个护栏是对的，不应该绕过。** 正确的修法是让「分析缓存」不落在被审工作区：

| 项 | 原方案（错） | 现方案 |
|---|---|---|
| 索引位置 | `工作区/.margin-graph/` | `.runs/codegraph/<仓库hash>.db` |
| 问题 | 污染审查范围（审查器会扫到自己的索引）；只读 agent 拿到写盘工具 | 不在被审目录，天然被 gitignore |

并给工具箱加了 `readonly` 参数：架构助手只拿三个查询工具，**不拿 `build_graph`**。
索引由审查助手或用户预先建好，两个 Agent 共用同一份图。

---

## 五、怎么验证

```bash
cd backend
python -m pytest tests/test_codegraph_memory.py -q    # 27 passed（新模块行为）
python -m pytest tests -q                             # 310 passed（全量）

# 手动验证跨文件追踪
python -c "
import sys; sys.path.insert(0,'.')
from pathlib import Path
from harness.sandbox import Workspace
from harness.tools_graph import build_graph_toolbox
ws = Workspace(Path('examples/m2-report-hub/repo'))
tb = build_graph_toolbox(ws)
print(tb.get('build_graph').handler().render())
print(tb.get('who_calls').handler(symbol='_require_ops').render())
"
```

---

## 六、下一步（按依赖顺序）

这两个模块是**地基**，接下来才能加新 Agent：

1. `tester` 测试助手 —— 直接吃 `tests_for` + `run_tests`
2. `planner` 策划助手 —— 吃 `impact` 估改动范围
3. 前端展示 —— 把 `impact` 的深度环做成可视化（现在是纯文本返回）
