# E2 · requests —— HTTP 客户端库（中等）

## 难度锚定

Medium 档的典型考法（System Design Primer 的 "Design a URL shortener/rate limiter" 一档、
techscreen 的 Easy-Medium）：**单个客户端系统内的多层抽象、连接复用、失败重试** ——
有真实的状态管理与传输抽象，但仍限定在单进程、单次请求生命周期内，不涉及多节点一致性。

requests 是这一档的标准样本：

- 分层是真的：`models`（请求/响应对象）→ `sessions`（连接池、Cookie、重试、挂载）→
  `adapters`（传输实现：urllib3 之上）→ `auth` / `packages`（扩展点）；
- 有明确的状态生命周期（Session 的连接池、CookieJar 的持久化）；
- 失败策略（重试、超时、抛错语义）散落在多层 —— 正好考验 agent 能否指出
  「一个横切关注点被放在哪一层、代价是什么」。

## 仓库

- 上游：https://github.com/psf/requests （Python 生态事实标准 HTTP 客户端）
- 规模：37 个 Python 文件 / 约 12k 行（含测试）
- 测试方式：全量（无 scope 限制）

## 给架构助手的目标（原文）

> 为这个 HTTP 客户端库出一版可评审的架构方案：分层、组件、关键决策与未定的问题。
> 重点讲清四件事：传输适配层（adapters）把什么抽象掉了、换掉 urllib3 的代价是什么；
> 连接池与会话状态分别由谁持有、生命周期怎么结束；重试与超时策略分别落在哪一层、
> 为什么不在同一层；Cookie 与认证的扩展边界在哪里。

## 评审这份蓝图时看什么

1. **是否识别出 Session 是「配置 + 连接池 + Cookie」的三合一容器** —— 这是 requests 里
   最容易被说错的架构事实（把它当成「一个类」就丢了重点）。
2. **adapters 层的可替换性**有没有被识别为关键决策（`HTTPAdapter` 的 mount 机制）。
3. **重试的真相**：requests 本身不重试，重试在 urllib3 —— 蓝图如果说「requests 有重试层」
   就是没读代码（这是本案例的照妖镜）。
4. **开放问题**是否点到真实的边界（例如 `PreparedRequest` 与 `Request` 的双对象设计的债务）。
