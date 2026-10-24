# tester（测试补全）· 实现说明

> 2026-10-24 实现。第三个 Agent，`outcome = test_plan`。
> 选型依据见 `新Agent候选人调研-20261024.md` 候选 2。

---

## 一、它解决什么问题

**不是**「找代码缺陷」（那是 review），**不是**「设计架构」（那是 architect）。

它回答：**哪些行为还没有被测试锁住，一旦被改坏也没人会发现。**

这个区别很具体：

| | review | tester |
|---|---|---|
| 视角 | 缺陷视角：这里已经坏了 | **覆盖视角**：这里将来会被改坏 |
| 产出 | `finding`（有 blocker/warn/note） | `test_case`（**没有严重级别**） |
| 典型问句 | 「这行会不会崩？」 | 「这行改了谁会知道？」 |

一个完全正确的函数，只要没有测试锁住它的行为，就值得为它写用例。

---

## 二、核心设计：`should_fail_when` 是必填

这是整个助手里最重要的一个约束。

**它逼着作者回答「这条测试在什么具体条件下会红」。**

回答不出来，说明这条测试**没有断言任何东西** —— 那样的测试全绿也不构成证据。
理论依据（调研里的原文）：

> 一条测试通过，只有在**原始有缺陷的代码上它会失败**时，才算关于该缺陷的证据。

### 三层校验

| 情况 | 处理 |
|---|---|
| 少于 8 字 | 拒绝：`VagueShouldFailWhen` |
| 与 title 相似度 ≥ 0.85 | 拒绝：说明它是同义复述，没回答问题 |
| `kind=characterization` 但没填 `pins_behavior` | 拒绝：特征测试必须说清锁的是哪个当前行为 |

相似度用 `difflib.SequenceMatcher`，实测能挡住这类凑数：

```
✗ title=「退款金额按实收计算」  should_fail_when=「退款金额算错时」
✓ title=「退款金额按实收计算」  should_fail_when=「pay=0 未付款而 refund 传全额时仍然退款成功」
```

---

## 三、四种用例类型

| kind | 锁什么 | 何时用 |
|---|---|---|
| **characterization** | 代码**当前实际**的行为（包括看起来是错的） | **仓库几乎没测试时优先用它** |
| unit | 函数在给定输入下的预期输出 | 行为明确、可推导 |
| integration | 多个组件协作时的行为 | 跨模块契约 |
| regression | 「曾经坏过、已修好」的那个具体缺陷 | 防止复发 |

### 特征测试的双刃剑

它是本助手价值最高的部分，也是最容易用错的：

- 它锁的是**当前实际行为**，所以要如实写「现在到底发生了什么」，哪怕那是缺陷。
- **绝对不要**为了让测试通过而断言一个已知缺陷是正确行为 —— 那会把缺陷固化：
  修好之后测试反而红，下一个人会以为是你写错了。
- 它建立的是**安全网**（改动会立刻显形），不是**正确性认证**。
  报告里对这一点有专门的说明块。

---

## 四、工具集（10 核心 + 5 按需）

### 核心（进静态前缀）

```
list_files / read_file / grep / git_diff / read_symbols
record_test_case          ← 登记一条用例（含三层校验）
run_generated_tests       ← 真跑一遍（**必须进核心**，见下）
update_todo / finish / discover_tools
```

### 按需（检索后加载）

```
check_coverage            ← 机械扫描：哪些公开符号没被任何测试引用
who_calls / impact / tests_for   ← 来自 codegraph
read_skill
```

### 为什么不给 `apply_patch` 和 `run_command`

`can_edit=True` 但**写入能力被限制在一个会被删掉的临时目录里**：

- 没有 `apply_patch` → 改不了生产代码
- 没有 `run_command` → 执行不了任意命令（`python` 能代写文件、`git` 能 checkout 掉改动）
- 唯一的执行工具 `run_generated_tests` 只写 `.margin-test-tmp/`，跑完即删

这条边界有测试守着（`test_toolbox_has_no_patch_or_shell`）。

---

## 五、实测踩到的五个 bug（都不是模型的问题）

### bug 1：`run_generated_tests` 用了系统临时目录 → 7 次尝试全部失败

第一版用 `tempfile.mkdtemp()`（系统 `/tmp`）。而 `CommandRunner.run(cwd=...)`
会校验 cwd 必须在工作区内 —— 系统临时目录直接撞 `PathEscape`。

**后果**：测试代码一次都没被执行。模型连续尝试 7 次（包括最小的 `assert 1+1==2`），
全部拿到同一个环境错误，然后**如实报告「这是工具侧故障，不是测试写错」**，
42 条用例全部停在 `unverified`。

**修法**：临时目录建在工作区下的 `.margin-test-tmp/`，跑完立即删除。
既满足沙箱边界，又真的能跑。

### bug 2：pytest 返回 exit=1 却被报成「测试通过」

`CommandRunner` 的 `ok` 只表示「命令跑完了」，不判断业务成败。
所以 pytest 断言失败（exit=1）在 runner 看来是 success ——
工具如实转述成「测试通过」，**模型据此以为断言成立**。

这是最坏的误导：一条假绿的测试比没有测试更糟。

**修法**：从 runner 输出里解析 `(exit=N)`，自己判断：
- `exit=0` → 通过
- `exit=5` → pytest 没收集到任何测试（不是通过，也不是代码有错）
- 其他 → 没全绿，并提示先分清「测试写错」还是「代码有缺陷」

### bug 3：`run_generated_tests` 躺在按需工具里 → 模型不知道它存在

第一版它和 `check_coverage` 一起放在按需工具里。结果：模型**从没想过要验证**，
42 条用例全部只有描述、零可运行代码。

**结论**：渐进披露在这里是反效果。「验证」是这个助手的核心动作，不是可选能力。
**已提升为核心工具**，并且 prompt 里写明了「它不在初始列表里时用 discover_tools 搜」
（现在它在核心列表里了，这句话保留作为兜底）。

### bug 4：schema 忘加 `verified`，而 handler 有 → 18 条全停在 unverified

给 `record_test_case` 加 `verified` / `verify_output` 时，我改了 **handler 的函数签名**，
却漏了 **tool schema 的 `properties`**。

后果链条很完整：
1. 模型从 schema 里看不到这两个字段 → 永远不会传；
2. 所有用例落在默认值 `unverified`；
3. **而模型在 summary 里明确写了「18 条全部用 run_generated_tests 真跑通过，
   实测输出 13 passed / 21 passed / 6 passed」**；
4. 报告头部显示「验证状态：未验证 18」—— 和人读的总结直接矛盾。

最阴险的是：这类不一致**不会有任何报错**，只是数据静默地缺一半。
而且模型第二次运行时**准确指出了这个缺陷**（「record_test_case 的 schema 不接受
verified / verify_output 字段」）—— 它比我更早发现。

**修法**：补 schema，并加一条测试守死：

```python
def test_record_test_case_schema_matches_handler(repo):
    """schema 必须覆盖 handler 的全部参数。"""
    declared = set(tool.parameters["properties"])
    actual = set(inspect.signature(tool.handler).parameters)
    assert not (actual - declared)
```

这类「签名改了、声明没改」的漂移，光靠人看代码是抓不住的。

### bug 5（同源）：重复登记被丢弃，验证状态永远补不上去

早期用 `state.add_artifact(...)`，重复项直接丢弃。于是：
模型先登记一条（还没跑，`unverified`）→ 跑通后想补 `verified` 重新登记 → 被当成重复丢掉
→ 验证状态永远是 `unverified`。

**修法**：改成「重复时更新可观测字段」（代码、验证状态、输出），
且验证状态**只允许单向前进**（不可降级），因为「跑过」是发生过的事实。

---

## 六、验证状态：三态而不是布尔

`TestCase.verified` 有三个值：

| 值 | 含义 |
|---|---|
| `verified` | 用 `run_generated_tests` 真跑过且通过 |
| `failed` | 跑过但没过（可能是代码有缺陷，也可能是测试写错） |
| `unverified` | 只有描述，没跑过（**默认值**） |

**为什么必须记这个**：
「42 条用例，0 条验证过」和「42 条都跑通了」是两件完全不同的事。
不记状态，报告读起来两者一模一样。

报告里：
- 头部固定显示 `验证状态：跑通 N / 跑过但失败 N / 未验证 N`
- 全部未验证时额外给一段显式警告
- 每条用例单独标注状态

---

## 七、离线模式

`HeuristicModel` 有显式的 tester 分支（`_complete_test`）：

```
列计划 → check_coverage 扫缺口 → 解析清单 → 逐个登记特征测试 → 收尾
```

**为什么必须显式分派**：没有分支时会掉进 `_complete_audit` 去调 `record_finding`，
而测试助手的工具箱里根本没这个工具 —— 直接报错，且错误信息指向不明。

---

## 七点五、端到端实测（真实模型 deepseek-v4.1-flash）

在 `examples/m1-order-desk/repo`（14 个模块、100 个公开符号、**零测试文件**）上跑了四轮，
每一轮都暴露一个 bug，修完再跑：

| 轮次 | 产出 | 带代码 | 验证状态 | 当轮暴露的问题 |
|---|---|---|---|---|
| 1 | 42 条 | 0 | 全部 unverified | `run_generated_tests` 在按需工具里，模型不知道它存在 |
| 2 | 23 条 | 23 | 全部 unverified | 工具用系统临时目录 → 7 次尝试全撞 PathEscape；exit=1 被报成通过 |
| 3 | 12 条 | 12 | 全部 unverified | `verified` 字段没进 schema（模型准确指出了这一点） |
| 4 | **11 条** | **11** | **全部 verified** | — |

最终一轮：31 轮迭代、228 秒、11 条用例全部真跑通过。

产出实例（都是靶场里的**真实缺陷**被钉成测试）：

```
[verified] 座席查看权限当前对所有客户都放行
  has_permission_to_view  (app/service.py:265)
  会红于: 非 supervisor 角色对不属于自己辖区的 owner_id 返回 False
  输出: 12 passed in 0.19s

[verified] 仓库层依赖的行转对象方法必须存在
  Store.to_order  (app/repository.py:30)
  会红于: Store 缺少 to_order 方法，OrderRepository.get 等抛 AttributeError
  输出: 替代实现下 10 passed in 0.25s

[verified] 已支付订单取消走不到退款意向分支
  OrderService.cancel_order  (app/service.py:120)
  会红于: 状态机允许 PAID→CANCELLED，已支付订单取消成功并释放库存
  输出: 4 passed in 1.35s
```

**这条链的意义**：把「审查发现的缺陷」变成「改坏就会立刻报的测试」。
`review` 说这里有问题，`tester` 把它钉住 —— 两个 Agent 第一次形成了闭环。

---

## 八、怎么验证

```bash
cd backend

# 单元与行为测试（38 条）
python -m pytest tests/test_tester_agent.py -q

# 全量
python -m pytest tests -q    # 353 passed

# 离线跑一遍（不需要模型）
python -m harness --agent tester --offline --repo examples/s1-obvious-danger/repo

# 真实模型
python -m harness --agent tester --repo examples/m1-order-desk/repo
```

---

## 九、已知边界（如实说明）

1. **只支持 Python / pytest**。其他语言跑不了。
2. **`check_coverage` 是粗粒度的**：只判断「名字有没有在测试文件里出现过」，
   出现过 ≠ 测得好。它给的是排序起点，不是覆盖率报告。
3. **`run_generated_tests` 隔离是进程级的**，不是容器级。测试代码与被审代码同权限运行。
   这是项目现状（`sandbox.py` 一开始就写明了「Python venv 不是沙盒」）。
4. **特征测试锁的可能是缺陷**。这是设计使然（安全网优先），但报告里必须让读者看见
   这个区别 —— 已通过「锁定行为」字段 + 说明块 + summary 要求做到。
