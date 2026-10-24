# planner（实施计划）· 实现说明

> 2026-10-25 实现。第四个 Agent，`outcome = roadmap`。
> 选型依据见 `新Agent候选人调研-20261024.md` 候选 1。

---

## 一、它填的是链路第一个空位

现有四个 Agent 的分工：

| Agent | 时态 | 回答什么 |
|---|---|---|
| `review` | 现在 | 哪里**已经**坏了 |
| `architect` | 现在（应然） | 结构**应该**怎么切 |
| `tester` | 现在 | 哪些行为**还没**被锁住 |
| **`planner`** | **未来** | **要改成什么样，按什么顺序落地** |

前三个都在描述现状，`planner` 是唯一面向未来的产出。

### 与 architect 的边界（最容易混）

差别不在颗粒度，在**可验收性**：

| | architect | planner |
|---|---|---|
| 输入 | 「帮我设计这个系统的结构」 | 「我要把退款改成按实收算，怎么做」 |
| 产出 | 分层、组件、取舍 | 有顺序的步骤 + 每步完成条件 |
| 验收 | 评审讨论（**主观**） | 跑测试 / 看输出（**客观**） |

所以 prompt 里明确写了：**不要**输出分层图，**不要**写「建议引入缓存层」这种没有完成条件的建议。

---

## 二、核心约束：`done_when` 必须可判定

计划的头号失败模式不是「步骤错了」，而是「**步骤不可判定**」。

```
✗ 优化错误处理           ← 做完了吗？不知道
✗ 重构支付模块           ← 同上
✓ 所有外部调用都包在 try/except 里，且新增 3 条异常路径测试通过
✓ refund 不再读取 order.total，现有 6 条 service 测试全绿
✓ ruff check 无告警
```

### 三层校验

| 校验 | 规则 | 拒绝码 |
|---|---|---|
| 非空 | 必须有值 | `MissingDoneWhen` |
| 可判定 | 必须含可观测信号 | `UnfalsifiableDoneWhen` |
| 指向性 | `touches` 必须指到文件/符号，只给目录不行 | `VagueTouches` |

### 「可观测信号」的判定演进（踩过两次坑）

判据是：**含可观测信号就不算空话**。信号包括数字、测试结果、返回值、报错、
字段名、标识符（`order.total`）、命令（`pytest` / `ruff`）。

第一版太窄，误杀了两类合法写法：

- 「修改 refund 使其不再读取 `order.total`」——有标识符，可查证
- 「删除 unused import，`ruff check` 无告警」——有命令

第二版补了标识符与命令的正则。**宁可放过，不可误杀** ——
误杀会让模型反复改措辞去绕校验，白烧轮次。

### 一个顺序 bug（很隐蔽）

```python
# 错的：先看长度
if len(t) < 8: return True
if _CONCRETE_SIGNALS.search(t): return False
```

「6 条测试全绿」只有 **7 个字符**，被长度检查拦掉 ——
而这正是最常见的合法完成条件。

模型收到「无法判定真假」的错误，会去改措辞（加几个字）而不是改内容，
结果既浪费轮次又没解决真问题。

**修法**：先查信号再看长度。顺序反了，再好的判据也白搭。

---

## 三、第二个约束：`touches` 必须指到具体位置

「改哪里」指不到文件和符号，通常说明作者还没想清楚要改什么。
这是从 tester 那边学到的：**写不出 `file:line` 就是还没读懂**。

只给目录（`app/`）会被拒——那等于「改整个模块」。

---

## 四、风险登记：假设而不是问题

`record_plan_risk` 复用 `OpenQuestion` 的数据形状（都是「不确定、需要人判」的东西），
但用**独立的产出桶**（`plan_risk`），因为语义不同：

- 架构助手的 `open_question` = **问题**（我还没想好怎么切）
- planner 的 `plan_risk` = **假设**（我当前按它做计划）

`if_wrong` 必填：只说「有这个风险」没有信息量，要说清**不成立会怎样、怎么调整**。

**零前提的计划会被提醒**：一个「零假设」的计划通常说明作者还没真正评估过不确定性。

---

## 五、工具集（9 核心 + 6 按需）

### 核心

```
list_files / read_file / grep / git_diff / read_symbols
record_plan_step          ← 登记一步（三层校验）
update_todo / finish / discover_tools
```

### 按需

```
record_plan_risk          ← 登记前提
check_plan_gaps           ← 机械自检（序号连续、字段齐全）
impact / who_calls / tests_for   ← 来自 codegraph，impact 是最常用的
read_skill
```

### 为什么 `can_edit=False`

计划是「建议怎么改」，不是「已经改好了」。工具箱里**没有 `apply_patch`**。
与架构助手同一类边界：产出是可评审的建议，不是已实施的结果。

---

## 六、端到端实测（真实模型 deepseek-v4.1-flash）

**输入**：「把退款的可退金额从『按应付金额』改成『按实收金额』。现在未付款订单也能全额退款。」

**输出**：17 轮 / **56 秒** / **6 步 + 5 条前提（1 条阻塞）** / **0 步缺 verify**

### 质量实例：第 1 步的完成条件

```
标题：给 Order 增加「实收金额」与「已退金额」两个派生属性，
      并把 refundable 的来源从 payable 换成实收
完成：Order(paid_amount=0,  payable=100).refundable == 0
      Order(paid_amount=100, payable=100).refundable == 100
      Order(paid_amount=100, payable=100, refunded_amount=30).refundable == 70
      Order(paid_amount=120, payable=100).refundable == 120（不按 payable 截断）
      models.py 中 refundable 的实现不再出现 payable 标识符
```

这不是「改完就好」，是**可以直接写成断言**的验收条件。

### 三处值得单独说的表现

**1. 它自己识别出了「口径未定义」是阻塞问题**

> [阻塞] 「实收金额」指支付流水里 pay 动作的累计入账（并扣除 refund 动作的累计出账），
> 而不是 order.payable，也不含未成功的退款意向（refund_intent）。
>
> 不成立时：若业务口径是「实收 = payable 且必须全额一次付清」，那第 2 步算出的
> 支付沉淀额会偏小（分笔支付场景），退款会被错误拒绝。

这正是 planner 该做的事：**需求没说清的部分，不能假装明确地照做。**

**2. 它发现仓库没有测试，于是换了验证方式**

> 仓库里没有任何测试文件，因此本次改动的验收只能靠手工脚本跑服务方法，不能靠 pytest 回归。

没有硬写一条跑不了的 `pytest` 命令，而是把 `verify` 换成可执行的内联脚本判据。

**3. `may_invalidate` 写出了真实的步骤依赖**

第 3 步：
> 若第 2 步最终没有把实收持久化到订单行（改为运行时从账本反查），
> 这里的 `order.refundable` 会退化成一次 IO 查询，「在锁内」这个前提要重新评估。

这不是模板话，是**这条计划自己的失效路径**。

### 落地顺序的理由（summary）

> 口径必须先从「展示逻辑」变成「可持久化的数据」，否则改不动。
> `models.py:98` 的 `refundable` 读 `payable`，而真正的问题在于 `pay`
> 只把金额写进账本、订单行上没有实收字段 —— 所以先立属性（第 1 步）、
> 再给它一个持久来源（第 2 步），然后才在校验点换口径并加锁（第 3 步）。

---

## 七、离线模式

`HeuristicModel._complete_plan`：按文件机械拆步骤（每文件一步，完成条件=该文件改完且测试全绿）。

**它明确标注了局限**（写在 summary 里）：

> 步骤是按文件机械划分的，**没有识别真实依赖顺序**；真实模型运行时会读代码后按依赖排序。

这是刻意的：离线模式的价值是「让链路能跑通」，不是「产出好计划」。

---

## 八、怎么验证

```bash
cd backend

# 单元与行为测试（35 条）
python -m pytest tests/test_planner_agent.py -q

# 全量
python -m pytest tests -q    # 388 passed

# 离线
python -m harness --agent planner --offline --repo examples/m1-order-desk/repo

# 真实模型
python -m harness --agent planner --repo examples/m1-order-desk/repo
```

---

## 九、已知边界（如实说明）

1. **不判断计划对不对**。`check_plan_gaps` 只查结构（序号、字段齐全），
   **不判断**步骤是否合理、优先级是否对、有没有漏掉关键步骤。
   自检通过 ≠ 计划可行，渲染时明确写了这句。
2. **不执行**。计划不会被验证「照做能不能成」—— 那需要人工或另一个 Agent。
3. **`done_when` 的可判定性靠正则启发式**，不是形式化校验。
   「调整一下日志格式」这类边界写法可能被放过（可接受：误放优于误杀）。
4. **离线模式不识别依赖顺序**，按文件机械划分。
