# Margin · 研发辅助 Agent 工作台

一个工作台 + 多个**可插拔**研发辅助 Agent。内置两个：

| Agent | 产出形态 | 说明 |
|---|---|---|
| `review` 代码审查 | 发现列表（带行号与证据） | 读仓库、跑规则、按严重级给出可评审的发现 |
| `architect` 架构设计 | 分层图 + 组件 + 决策 + 开放问题 | 只读：把一个仓库的结构讲成可评审的方案草稿 |

接一个新助手 = 在 `backend/harness/agents.py` 写一份 `AgentSpec` 并调一次 `register`，
**不改既有代码**（工具、渲染、HTTP、前端轨道全部按声明走）。

## 快速开始

```bash
cd backend
python -m harness.server --port 8000
# 打开 http://127.0.0.1:8000/ —— 服务同源托管前端，实时模式
```

- 真实模型：在 `backend/.env` 配 `DEEPSEEK_API_KEY`（见 `.env.example`，`.env` 不入库）
- 离线规则模式：不加 key 也能跑（确定性规则 + 启发式模型），适合先看界面
- 演示模式：直接双击 `index.html`（`file://`），走 `scripts/mock-data.js` 的演示数据

### 测试账户

首次启动时（账号表为空）自动创建，登录页上也有「测试账号」一键登录按钮：

| 邮箱 | 密码 | 名字 |
|---|---|---|
| `demo@margin.dev` | `Margin-demo-2026` | 演示账号 |
| `reviewer@margin.dev` | `Margin-review-2026` | 林哲 |

账号存在 `backend/.auth/users.json`（**不入库**），密码用 scrypt 加盐哈希保存，
明文不落盘。删掉该文件重启即可重新生成测试账户。

默认**不强制登录**（本机工具，直开即用）；想要「未登录不能访问」就把
`MARGIN_REQUIRE_AUTH=1` 写进 `.env`，业务接口会要求有效会话，前端自动跳登录页。

认证相关接口：`POST /api/auth/register` · `POST /api/auth/login` ·
`POST /api/auth/logout` · `GET /api/auth/me`。会话靠 HttpOnly + SameSite=Strict
的 Cookie 维持，令牌只存在服务端内存里（重启需要重新登录）。

## 目录

```
backend/
  harness/          平台核心（零 web 框架，HTTP 层也是标准库）
    agents.py       Agent 注册表：一份声明 = 一个助手
    loop.py         主循环：Agent = Model + Harness
    tools_common.py 两个助手共享的工具（唯一实现）
    tools_audit.py  审查特有（record_finding / apply_patch …）
    tools_design.py 架构特有（record_layer / component / decision …）
    server.py       HTTP + SSE + 静态托管（同源，无 CORS）
  arch-cases/       架构助手的三档难度实测（选题依据 + 结论，repo 自行克隆）
  examples/         审查靶场（repo/ + labels.json，标注在审计根之外）
  eval/             评估链路：run_bench → make_review → make_report
  tests/            回归（当前 151 passed）
scripts/ styles/    前端（原生 JS + CSS，无构建）
mockups/            前端原型（含设计依据 PLAN.md）
```

## 两条不能破的纪律

1. **标注绝不能出现在被审计的代码里** —— 答案印在题目下面，指标就是假的。
   `test_audit_roots_do_not_leak_labels` 守着这条边界。
2. **失败不能伪装成空结果** —— 「目录不存在」说成「没有匹配内容」会驱动假阴性结论。
   工具层一律显式失败信封（类型 + 参数 + 建议）。

更多约定（测量纪律、隔离模型、平台化契约）见 `.workbuddy/memory/MEMORY.md`。
交付数字见 `backend/BENCHMARK.md`（由 `eval/make_report.py` 现算，不手抄）。
