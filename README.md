# Margin · 研发辅助 Agent 工作台

> 全国高校 AI Web 开发挑战赛参赛作品 · 赛题方向 7（AI 代码开发辅助）

一个工作台 + 多个**可插拔**研发辅助 Agent。内置两个：

| Agent | 产出形态 | 说明 |
|---|---|---|
| `review` 代码审查 | 发现列表（带行号与证据） | 读仓库、跑规则、按严重级给出可评审的发现 |
| `architect` 架构设计 | 分层图 + 组件 + 决策 + 开放问题 | 只读：把一个仓库的结构讲成可评审的方案草稿 |

接一个新助手 = 在 `backend/harness/agents.py` 写一份 `AgentSpec` 并调一次 `register`，
**不改既有代码**（工具、渲染、HTTP、前端轨道全部按声明走）。

---

## 团队信息

| 项 | 内容 |
|---|---|
| 参赛组别 | B 组 |
| 学校 | 百色学院 |
| 团队成员 | 黄维涛（队长）、黄建康 |
| 指导教师 | 蒋锐 |
| 联系方式 | 3444203102@qq.com |

---

## 快速开始

### 1. 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | 3.11+（开发环境 3.12 / 3.13） | 用到 `list[str]` 与 `dataclass(slots=True)` 等语法 |
| 浏览器 | Chrome / Edge 现代版本 | 前端用原生 ES2020+ 与 CSS 嵌套，无构建步骤 |
| Node.js | **不需要** | 前端零 npm 依赖，无打包器 |
| 操作系统 | Windows / macOS / Linux | 开发于 Windows，代码无平台特定路径 |

### 2. 安装依赖

```bash
cd backend
pip install -r requirements.txt          # 运行时：只有 httpx
pip install -r requirements-dev.txt      # 开发/测试：额外 pytest
```

依赖刻意做到最小：**平台核心（`harness/`、`eval/`）只用 Python 标准库** ——
这个服务本身也是要被审计的代码，依赖越少越好审。前端同理：零依赖、零构建。

### 3. 配置模型

**只需要三项**，协议固定是 OpenAI 兼容，所以 DeepSeek、OpenAI、通义、Moonshot、
智谱、vLLM、Ollama、LM Studio 与各类中转服务都能直接接：

```bash
cp backend/.env.example backend/.env
```

```dotenv
LLM_BASE_URL=https://api.deepseek.com   # 接口地址（贴完整 /chat/completions 也能识别）
LLM_API_KEY=sk-...                      # 密钥；留空则跑离线规则模式
LLM_MODEL=deepseek-chat                 # 模型名，必须是服务端确实提供的
```

也可以不碰文件：启动后打开 **配置中心**（侧栏底部），三个输入框 + 「探测」
（自动拉取可用模型名）+ 「测试连接」（真实请求一次 `/models`）。

| 变量 | 必填 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | 否 | 接口地址，默认 `https://api.deepseek.com`。自动剥掉尾斜杠与误贴的 `/chat/completions` |
| `LLM_API_KEY` | 否 | 不填则跑离线规则模式（确定性规则 + 启发式模型），界面功能完整。不要求 `sk-` 前缀 |
| `LLM_MODEL` | 否 | 模型名，默认 `deepseek-chat` |
| `AUDIT_TEMPERATURE` | 否 | 采样温度，默认 0.2（审查任务建议 0.1–0.3） |
| `AUDIT_TIMEOUT` | 否 | 单次请求超时（秒），默认 120 |
| `AUDIT_MAX_ITERATIONS` | 否 | 单次运行的最大迭代轮数（硬约束，防死循环），默认 40 |
| `AUDIT_CONTEXT_WINDOW` | 否 | 上下文窗口，用于触发压缩（默认 80% 阈值），默认 131072 |
| `AUDIT_REPEAT_LIMIT` | 否 | 连续相同工具调用 N 次即判定陷入循环，默认 3 |
| `AUDIT_WORKSPACE_ROOT` | 否 | 允许 Agent 读写的根目录；留空则用命令行 `--repo` 指定的目录 |
| `AUDIT_ALLOW_COMMANDS` | 否 | 可执行命令白名单（逗号分隔） |
| `MARGIN_REQUIRE_AUTH` | 否 | 置 1 时未登录访问业务接口返回 401（默认不设，本机直开即用） |

> 旧的 `DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` 仍然有效，
> 已经配好的 `.env` 不用改（新变量名优先）。

**`.env` 不入库**（`.gitignore` 已覆盖），仓库里只有 `.env.example` 模板。
提交前可用 `git check-ignore -v backend/.env` 复核这条边界。

### 4. 启动

```bash
cd backend
python -m harness.server --port 8000
# 打开 http://127.0.0.1:8000/ —— 服务同源托管前端，实时模式
```

三种运行模式：

- **实时模式**（推荐）：上面这条命令，前端与 API 同源
- **离线模式**：不配 API Key 也能跑，产出由确定性规则集给出
- **演示模式**：直接双击 `index.html`（`file://`），走 `scripts/mock-data.js` 的演示数据

### 5. 测试账户

首次启动时（账号表为空）自动创建，登录页上也有「测试账号」一键登录按钮：

| 邮箱 | 密码 | 名字 |
|---|---|---|
| `demo@margin.dev` | `Margin-demo-2026` | 演示账号 |
| `reviewer@margin.dev` | `Margin-review-2026` | 林哲 |

账号存在 `backend/.auth/users.json`（**不入库**），密码用 scrypt 加盐哈希保存，
明文不落盘。删掉该文件重启即可重新生成测试账户。

认证接口：`POST /api/auth/register` · `POST /api/auth/login` ·
`POST /api/auth/logout` · `GET /api/auth/me`。会话靠 HttpOnly + SameSite=Strict
的 Cookie 维持，令牌只存在服务端内存里（重启需要重新登录）。

---

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
    auth.py         账号与会话（scrypt + 内存会话）
    rules.py        确定性规则集（兼作 LLM 结果的交叉验证器）
  arch-cases/       架构助手的三档难度实测（选题依据 + 结论，repo 自行克隆）
  examples/         审查靶场（repo/ + labels.json，标注在审计根之外）
  eval/             评估链路：run_bench → make_review → make_report
  tests/            回归（当前 175 项）
scripts/ styles/    前端（原生 JS + CSS，无构建）
mockups/            前端原型（含设计依据 PLAN.md）
```

## 竞赛提交材料

| 材料 | 位置 | 状态 |
|---|---|---|
| 代码仓库 | 本仓库 | ✅ |
| README（介绍 / 架构 / 运行指南 / 团队信息） | 本文件 | ✅ |
| 环境配置（变量模板 + 依赖清单） | `backend/.env.example` · `backend/requirements*.txt` | ✅ |
| 技术文档（PDF，≤30 页） | `docs/Margin-技术文档.pdf` | ✅ |
| 演示视频（5–8 分钟） | `系统展示视频/final.mp4`（5:42 / 1920×1080 / 普通话） | 📦 已制作，不入库：127MiB 超 GitHub 单文件 100MiB 上限，单独渠道提交 |
| 在线演示地址（可选加分） | — | 未提交 |

## 两条不能破的纪律

1. **标注绝不能出现在被审计的代码里** —— 答案印在题目下面，指标就是假的。
   `test_audit_roots_do_not_leak_labels` 守着这条边界。
2. **失败不能伪装成空结果** —— 「目录不存在」说成「没有匹配内容」会驱动假阴性结论。
   工具层一律显式失败信封（类型 + 参数 + 建议）。

更多约定（测量纪律、隔离模型、平台化契约）见 `.workbuddy/memory/MEMORY.md`。
交付数字见 `backend/BENCHMARK.md`（由 `eval/make_report.py` 现算，不手抄）。
