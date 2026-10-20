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

两种运行模式：

- **实时模式**（默认，也是唯一有数据的模式）：上面那条命令，前端与 API 同源
- **离线模式**：不配 API Key 也能跑，产出由确定性规则集给出（`--offline`）

> 用 `file://` 直接双击 `index.html` **不会**显示任何样例数据 —— 界面会告诉你
> 「后端没连上」并给出启动命令。这是有意的：一份看起来跑完了的审查，比一句
> 「没连上」更容易让人误判系统能力。浏览器还会因跨域与 PNA 策略拦住 `file://`
> 下的接口请求，这点改不了，只能起服务。

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
  harness/           平台核心（零 web 框架，HTTP 层也是标准库）
    agents.py        Agent 注册表：一份声明 = 一个助手
    loop.py          主循环；收尾按「位置核验 → 判官复核 → 证据核验」依次做
    model.py         OpenAI 兼容端点（不绑定厂商）
    config.py        配置：接口地址 / API Key / 模型名
    context.py       上下文构造与压缩（静态前缀恒定，以保缓存命中）
    guardrails.py    确定性核验：引用位置、行号内容、完成声明自证
    judge.py         独立判官：只删不加，逐条问「这段代码能证明这个结论吗」
    arch_check.py    架构产出：证据核验 + 权衡真实性（稻草人备选 / 只写好处）
    diffmode.py      增量审查：变更行 → 符号 → 反查改动之外的调用点
    sketch.py        一次 ast 扫描产出仓库结构清单（分层前的中间制品）
    pacing.py        轮次预算：按剩余比例提醒收尾
    tools_common.py  两个助手共享的工具（唯一实现）
    tools_audit.py   审查特有（record_finding / apply_patch …）
    tools_design.py  架构特有（record_layer / component / decision …）
    sandbox.py       路径围栏 + 命令白名单
    auth.py          账号与会话（scrypt + 服务端内存会话）
    server.py        HTTP + SSE + 静态托管（同源，无 CORS）
  arch-cases/        架构助手三档难度实测；上游代码用 fetch-cases.sh 拉取
  examples/          审查靶场（repo/ 是审计根，labels.json 在根之外）
  eval/              评估链路：run_bench → make_review → make_report
  tests/             回归（282 项）
scripts/ styles/     前端（原生 JS + CSS，无构建步骤，无依赖）
docs/                技术文档与调研报告
```

## 两条不能破的纪律

1. **标注绝不能出现在被审计的代码里** —— 答案印在题目下面，指标就是假的。
   `test_audit_roots_do_not_leak_labels` 守着这条边界。
2. **失败不能伪装成空结果** —— 「目录不存在」说成「没有匹配内容」会驱动假阴性结论。
   工具层一律显式失败信封（类型 + 参数 + 建议）；界面连不上后端时也会明说，
   不降级到样例数据。

## 关于数字

交付指标见 `backend/BENCHMARK.md` 与 `backend/arch-cases/RESULTS-scored.md`，
两者都**由脚本从原始结果现算**，不手抄：

```bash
cd backend
python -m eval.make_report        # 审查侧
python -m eval.make_arch_report   # 架构侧
```

指标对行号容差高度敏感，单次跑分不可引用。**引用任何数字前请先读这两份文档的
「已知局限」一节**，那里写清了样本规模、口径与失效条件。

---

## 许可与参与

MIT License —— 见 [LICENSE](LICENSE)。

想改代码请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。那一页不是模板，写的是这个项目
真实踩过的坑（标注泄露、测量口径、玻璃层级、宽度测量等），照着做能省不少返工。
