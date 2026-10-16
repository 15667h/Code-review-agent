# E3 · celery —— 分布式任务队列（困难）

## 难度锚定

Hard 档的共识考法（techscreen: "Payment/ledger, Message Queue, Task Scheduler = Hard"；
spawn08 难度表: "Distributed Message Queue ⭐⭐⭐⭐ Hard, Task Scheduler ⭐⭐⭐"）：
**多进程/多节点、可替换的基础设施、失败语义（至少一次/至多一次）、状态机**。

celery 全占：worker 并发池（prefork/gevent/eventlet/threads 四种模型）、消息中间件抽象
（kombu 之下的 broker 可替换）、结果后端可替换、任务状态机（PENDING→STARTED→SUCCESS/
FAILURE/RETRY）、重试与确认语义、canvas 工作流（group/chain/chord）。

## 仓库

- 上游：https://github.com/celery/celery （Python 生态最常用的分布式任务队列）
- 规模：447 个 Python 文件 / 约 123k 行（源码包 `celery/` 约 100 文件 / 6 万行，
  其余是 tests 与 docs）
- **测试方式：scope = ["celery"]**（只读源码包）。理由：12 万行全量超出任何 agent 的
  阅读预算，而 tests/docs 不承载架构决策。这一点如实写在这里，不假装全量读过。

## 给架构助手的目标（原文）

> 为这个分布式任务队列出一版可评审的架构方案：分层、组件、关键决策与未定的问题。
> 重点讲清五件事：一个任务从 submit 到被执行完，消息在系统里走的完整路径；
> worker 的并发模型（prefork/gevent/eventlet/threads）是在哪一层被抽象的、替换代价是什么；
> 消息中间件与结果后端如何做到可替换、这层抽象的泄漏点在哪里；
> 失败重试与消息确认的语义属于谁；任务状态机由谁维护、状态存在哪里。

## 评审这份蓝图时看什么

1. **路径完整性**：producer → broker → worker pool → task 执行 → result backend，
   五个环节是否都出现，且顺序/职责正确。
2. **`celery/app` 与 `celery worker` 是两个入口**（同一个 app 对象既是客户端也是 worker 容器）——
   能否识别这一点是区分「读过代码」与「背过架构图」的关键。
3. **可替换性的代价**：broker/backend 的抽象（kombu / backends 基类）有没有被识别，
   以及「泄漏点」（例如 prefork 才支持的某些特性在 gevent 下不可用）。
4. **决策记录**：至少应出现「为什么任务默认至少一次」「为什么 prefork 是默认池」这类
   带代价的判断；**开放问题**应点到真实的不确定性（例如 chord 的实现依赖、
   `billiard` 进程模型的边界）。
5. **难度对照**：与 E1/E2 对比，层数、组件数、决策数、开放问题数应有量级差异 ——
   如果三者产出规模差不多，说明 agent 没有按仓库实际复杂度调整（这是难度测试的核心观测点）。
