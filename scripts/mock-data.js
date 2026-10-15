/* ==========================================================================
   Margin · 演示数据
   全部为静态假数据，用于验证界面结构与交互，不连接任何真实服务。
   场景：meridian-labs/payments-api 的 PR #482（重构限流中间件）。
   ========================================================================== */

window.MARGIN_DATA = {
  /* ---------- 工作区 ---------- */
  workspace: {
    repo: "meridian-labs/payments-api",
    branch: "feat/rate-limit-refactor",
    pullRequest: 482,
    author: "linzhe",
    changedFiles: 7,
    additions: 312,
    deletions: 148,
  },

  /* ---------- 可切换模型 ----------
     以下 id / 显示名 / 上下文长度均来自 GET https://api.deepseek.com/models 的
     账号实测返回，不是占位值：
       deepseek-flash    → DeepSeek-V4.1-Flash，1,048,576 上下文，支持图像输入
       deepseek-v4-pro   → DeepSeek-V4-Pro，    1,048,576 上下文
     另有 deepseek-chat / deepseek-reasoner 两个旧名，官方标注 2026-07-24 弃用。 */
  models: [
    {
      id: "deepseek-flash",
      name: "DeepSeek V4.1 Flash",
      desc: "速度优先 · 1M 上下文",
      current: true,
    },
    {
      id: "deepseek-v4-pro",
      name: "DeepSeek V4.1 Pro",
      desc: "复杂推理 · 1M 上下文",
    },
    {
      id: "deepseek-chat",
      name: "DeepSeek Chat（旧）",
      desc: "2026-07-24 弃用",
    },
    {
      id: "deepseek-reasoner",
      name: "DeepSeek Reasoner（旧）",
      desc: "2026-07-24 弃用",
    },
  ],

  /* ---------- 空间：每个空间是一个被持续审查的仓库 ---------- */
  spaces: [
    {
      id: "payments-api",
      owner: "meridian-labs",
      name: "payments-api",
      open: true,
      sessions: [
        {
          id: "payments-api-482",
          title: "#482 限流中间件重构",
          time: "今天 09:41",
          current: true,
          branch: "feat/rate-limit-refactor",
          pr: 482,
          files: 7,
          add: 312,
          del: 148,
        },
        {
          id: "payments-api-478",
          title: "#478 对账任务幂等性",
          time: "10月12日",
          branch: "fix/reconcile-idempotent",
          pr: 478,
          files: 4,
          add: 96,
          del: 31,
        },
        {
          id: "payments-api-455",
          title: "#455 退款回调重试",
          time: "10月8日",
          branch: "feat/refund-retry",
          pr: 455,
          files: 11,
          add: 402,
          del: 210,
        },
      ],
    },
    {
      id: "ledger-core",
      owner: "meridian-labs",
      name: "ledger-core",
      open: false,
      sessions: [
        {
          id: "ledger-core-1291",
          title: "#1291 日终结算顺序",
          time: "10月14日",
          branch: "fix/eod-ordering",
          pr: 1291,
          files: 9,
          add: 188,
          del: 77,
        },
      ],
    },
    {
      id: "web-console",
      owner: "meridian-labs",
      name: "web-console",
      open: false,
      sessions: [
        {
          id: "web-console-76",
          title: "#76 权限矩阵重构",
          time: "10月11日",
          branch: "feat/permission-matrix",
          pr: 76,
          files: 23,
          add: 640,
          del: 512,
        },
      ],
    },
  ],

  /* ========================================================================
     开场：一轮已完成的审查
     ======================================================================== */
  opening: {
    user: "审查这个 PR。重点看并发安全和资源泄漏，命名和格式不用管。",

    reviewedAt: "2026-10-15T09:41:00+08:00",

    intro:
      "已读取 PR #482 的 7 个变更文件。变更集中在限流中间件，从固定窗口切到滑动窗口，涉及 Redis 连接复用，我先跑一遍静态检查，再针对连接生命周期做压测复现。",

    plan: [
      { label: "读取 PR 变更", meta: "7 个文件，+312 −148", duration: "1.2s" },
      { label: "静态扫描与依赖检查", meta: "0 个新增依赖", duration: "0.9s" },
      { label: "追踪 Redis 连接生命周期", meta: "4 条调用链", duration: "3.4s" },
      { label: "压测复现连接占用", meta: "200 QPS × 90s", duration: "94.1s" },
      { label: "运行 rate-limit 测试套件", meta: "18 通过 / 0 失败", duration: "12.6s" },
      { label: "汇总发现", meta: "5 条", duration: "0.4s" },
    ],

    tools: [
      {
        name: "read_file",
        arg: "src/middleware/rate-limit.ts",
        meta: "214 行",
        duration: "0.3s",
        output:
          "src/middleware/rate-limit.ts\n" +
          "  L1    import type { Request, Response, NextFunction } from \"express\";\n" +
          "  L2    import { pool } from \"../lib/redis\";\n" +
          "  L3    import { nonce } from \"../lib/nonce\";\n" +
          "  ...\n" +
          "  L44   async function check(key: string): Promise<Verdict> {\n" +
          "  L45     const conn = await pool.acquire();\n" +
          "  ...\n" +
          "  [已省略 208 行，完整内容见右侧变更视图]",
      },
      {
        name: "grep",
        arg: "acquire\\(|release\\(",
        meta: "命中 9 处 / 3 个文件",
        duration: "0.2s",
        output:
          "src/middleware/rate-limit.ts:45   const conn = await pool.acquire();\n" +
          "src/middleware/rate-limit.ts:55   await pool.release(conn);\n" +
          "src/middleware/rate-limit.ts:78   const conn = await pool.acquire();   ← 无对应 release\n" +
          "src/lib/redis.ts:31               export async function acquire() {\n" +
          "src/lib/redis.ts:44               export async function release(c) {\n" +
          "src/jobs/reconcile.ts:22          await pool.acquire();                 ← 同上\n" +
          "src/jobs/reconcile.ts:58          await pool.release(conn);\n" +
          "tests/rate-limit.test.ts:14       vi.spyOn(pool, \"acquire\")\n" +
          "tests/rate-limit.test.ts:28       vi.spyOn(pool, \"release\")",
      },
      {
        name: "read_file",
        arg: "src/lib/redis.ts",
        meta: "88 行",
        duration: "0.2s",
        output:
          "src/lib/redis.ts\n" +
          "  L12   async function connect(attempt = 1) {\n" +
          "  L13     while (true) {\n" +
          "  L14       try { return await createClient(URL); }\n" +
          "  L15       catch { await sleep(attempt * 120); attempt += 1; }\n" +
          "  L16     }\n" +
          "  L17   }\n" +
          "  ...\n" +
          "  L31   export async function acquire() {\n" +
          "  ...\n" +
          "  [已省略 74 行]",
      },
      {
        name: "run_bench",
        arg: "--qps 200 --duration 90s --profile pool",
        meta: "复现成功",
        duration: "94.1s",
        output:
          "qps=200 duration=90s connections=64(limit)  pool.timeout=1184\n" +
          "t=0s    active=12  idle=1   wait=0\n" +
          "t=30s   active=38  idle=0   wait=14\n" +
          "t=60s   active=57  idle=0   wait=203\n" +
          "t=90s   active=64  idle=0   wait=1184   ← 池已耗尽\n" +
          "p99 latency: 41ms → 3,020ms",
      },
      {
        name: "run_tests",
        arg: "--suite rate-limit",
        meta: "18 通过 / 0 失败",
        duration: "12.6s",
        output:
          "tests/rate-limit.test.ts (18)\n" +
          "  ✓ allows requests under the limit\n" +
          "  ✓ blocks requests over the limit\n" +
          "  ✓ resets after the window elapses\n" +
          "  ...\n" +
          "Test Files  1 passed (1)\n" +
          "     Tests  18 passed (18)\n" +
          "  注意：现有用例未覆盖 acquire 后的异常路径，因此未捕获 F-01。",
      },
    ],

    findings: [
      {
        id: "F-01",
        severity: "blocker",
        file: "src/middleware/rate-limit.ts",
        line: 47,
        title: "异常路径不归还 Redis 连接，连接池会被逐步耗尽",
        detail:
          "acquire() 之后的 await 链上任何一步抛错，都会让 release() 被跳过。生产上表现为随机超时，且重启前不会自行恢复。",
        evidence:
          "200 QPS 持续 90 秒：连接占用从 12 单调升到上限 64，pool.timeout 触发 1,184 次，p99 延迟从 41ms 恶化到 3,020ms。",
        fix: "把 acquire 与 release 收进同一个 try/finally，或改用连接池自带的 withConnection 包装器。",
        diff: {
          file: "src/middleware/rate-limit.ts",
          startLine: 44,
          lines: [
            { type: "ctx", num: 44, text: "async function check(key: string): Promise<Verdict> {" },
            { type: "ctx", num: 45, text: "  const conn = await pool.acquire();" },
            { type: "add", num: 46, text: "  try {" },
            { type: "ctx", num: 47, text: "    const windowStart = Date.now() - WINDOW_MS;" },
            { type: "ctx", num: 48, text: "    const hits = await conn.zcount(key, windowStart, \"+inf\");" },
            { type: "ctx", num: 49, text: "" },
            { type: "ctx", num: 50, text: "    if (hits >= limit) {" },
            { type: "ctx", num: 51, text: "      return { allowed: false, hits };" },
            { type: "ctx", num: 52, text: "    }" },
            { type: "ctx", num: 53, text: "" },
            { type: "ctx", num: 54, text: "    await conn.zadd(key, Date.now(), `${Date.now()}-${nonce()}`);" },
            { type: "ctx", num: 55, text: "    await conn.expire(key, WINDOW_S);" },
            { type: "del", num: 56, text: "    await pool.release(conn);" },
            { type: "ctx", num: 57, text: "    return { allowed: true, hits: hits + 1 };" },
            { type: "add", num: 58, text: "  } finally {" },
            { type: "add", num: 59, text: "    await pool.release(conn);" },
            { type: "add", num: 60, text: "  }" },
            { type: "ctx", num: 61, text: "}" },
          ],
        },
      },
      {
        id: "F-02",
        severity: "warn",
        file: "src/middleware/rate-limit.ts",
        line: 63,
        title: "窗口边界用了闭区间，临界请求会被计两次",
        detail:
          "windowStart <= t 让恰好落在窗口边缘的请求同时属于前后两个窗口，限流阈值在边界处实际放宽为 limit + 1。",
        evidence: "构造 t = windowStart 的请求，连续两次调用返回 hits 均为 limit，第三次才被拒绝。",
        fix: "左边界改为开区间，只保留右闭。",
        diff: {
          file: "src/middleware/rate-limit.ts",
          startLine: 62,
          lines: [
            { type: "ctx", num: 62, text: "function inWindow(t: number, windowStart: number, now: number) {" },
            { type: "del", num: 63, text: "  return windowStart <= t && t <= now;" },
            { type: "add", num: 63, text: "  return windowStart < t && t <= now;" },
            { type: "ctx", num: 64, text: "}" },
          ],
        },
      },
      {
        id: "F-03",
        severity: "warn",
        file: "src/lib/redis.ts",
        line: 12,
        title: "重连没有次数上限，Redis 抖动时会无限重试",
        detail:
          "while (true) 配合随次数线性增长的间隔，在 Redis 长时间不可用时会让调用方永久挂起，而不是快速失败。",
        evidence: "断开 Redis 后调用 acquire()，180 秒内未返回且未抛错，进程无法进入降级分支。",
        fix: "设定最大重试次数，超出后抛出，让上层走降级逻辑。",
        diff: {
          file: "src/lib/redis.ts",
          startLine: 12,
          lines: [
            { type: "del", num: 12, text: "async function connect(attempt = 1) {" },
            { type: "add", num: 12, text: "const MAX_ATTEMPTS = 5;" },
            { type: "add", num: 13, text: "" },
            { type: "add", num: 14, text: "async function connect() {" },
            { type: "del", num: 13, text: "  while (true) {" },
            { type: "del", num: 14, text: "    try { return await createClient(URL); }" },
            { type: "del", num: 15, text: "    catch { await sleep(attempt * 120); attempt += 1; }" },
            { type: "del", num: 16, text: "  }" },
            { type: "add", num: 15, text: "  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {" },
            { type: "add", num: 16, text: "    try {" },
            { type: "add", num: 17, text: "      return await createClient(URL);" },
            { type: "add", num: 18, text: "    } catch (err) {" },
            { type: "add", num: 19, text: "      if (attempt === MAX_ATTEMPTS) throw err;" },
            { type: "add", num: 20, text: "      await sleep(Math.min(120 * 2 ** (attempt - 1), 3_000));" },
            { type: "add", num: 21, text: "    }" },
            { type: "add", num: 22, text: "  }" },
            { type: "ctx", num: 23, text: "}" },
          ],
        },
      },
      {
        id: "F-04",
        severity: "note",
        file: "src/middleware/rate-limit.ts",
        line: 88,
        title: "Promise.all 会让单个分片失败拖垮整批",
        detail:
          "所有分片的计数被一个 Promise.all 包住，任意分片超时都会让整次判定失败并向上抛错，而其余分片其实已经成功。",
        evidence: "注入单个分片 2 秒延迟后，check() 整体抛错，其余 7 个分片的结果被丢弃。",
        fix: "改用 allSettled，对成功分片正常汇总，失败分片单独记录并降级。",
        diff: {
          file: "src/middleware/rate-limit.ts",
          startLine: 88,
          lines: [
            { type: "del", num: 88, text: "const results = await Promise.all(shards.map(checkShard));" },
            { type: "add", num: 88, text: "const settled = await Promise.allSettled(shards.map(checkShard));" },
            { type: "add", num: 89, text: "const results = settled" },
            { type: "add", num: 90, text: "  .filter((r) => r.status === \"fulfilled\")" },
            { type: "add", num: 91, text: "  .map((r) => r.value);" },
            { type: "add", num: 92, text: "settled" },
            { type: "add", num: 93, text: "  .filter((r) => r.status === \"rejected\")" },
            { type: "add", num: 94, text: "  .forEach((r) => logger.warn(\"shard degraded\", r.reason));" },
          ],
        },
      },
      {
        id: "F-05",
        severity: "pass",
        file: "tests/rate-limit.test.ts",
        line: 1,
        title: "测试套件与依赖检查通过",
        detail:
          "18 个用例全部通过，无新增依赖，无锁文件变更。缺口在于异常路径没有用例覆盖，建议随 F-01 一并补上。",
        evidence: "18 passed / 0 failed；package.json 无 diff。",
        fix: null,
        diff: null,
      },
    ],

    summary:
      "4 条需要处理，其中 1 条阻断合并。建议顺序：先修 F-01（连接归还），它同时是压测里唯一能造成生产可用性下降的问题；再合并 F-03 的重试上限，两者都在连接生命周期上。F-02 和 F-04 可以放在同一个后续 PR。",
  },

  /* ========================================================================
     追问快捷入口
     ======================================================================== */
  followUps: [
    { label: "只展开阻断项", action: "filter", value: "blocker" },
    { label: "补一份异常路径的测试用例", action: "ask", value: "针对 F-01 补一份异常路径的测试用例，覆盖 acquire 之后抛错的情况。" },
    { label: "这会影响现有线上流量吗", action: "ask", value: "这个连接泄漏会影响现有线上流量吗？给出影响面判断。" },
  ],

  /* ========================================================================
     追问时的后续回复（轮换使用）
     ======================================================================== */
  replies: [
    {
      intro:
        "先看 F-01 的生产影响面。要点在于泄漏只发生在异常路径上，而异常本身来自分片抖动，所以泄漏速率约等于抖动频率，属于缓慢累积型，不是瞬间雪崩。",
      plan: [
        { label: "统计异常路径触发频率", meta: "近 14 天日志", duration: "1.8s" },
        { label: "估算连接耗尽时间", meta: "按当前抖动率", duration: "0.6s" },
        { label: "确认降级行为", meta: "无降级分支", duration: "0.4s" },
      ],
      tools: [
        {
          name: "grep_logs",
          arg: "\"shard timeout\" --since 14d --count",
          meta: "1,204 条 / 14 天",
          duration: "1.8s",
          output:
            "2026-10-01  71\n2026-10-02  64\n...\n2026-10-14  118\n日均 86 次，无下降趋势。",
        },
      ],
      findings: [
        {
          id: "F-06",
          severity: "warn",
          file: "src/middleware/rate-limit.ts",
          line: 47,
          title: "按当前抖动率，连接池约 11 天耗尽",
          detail:
            "日均 86 次异常路径泄漏，池上限 64 但在正常流量下会周期性回收空闲连接，净增长约每天 6 条，因此耗尽时间以周计而非以天计。",
          evidence: "日均 86 次泄漏，扣除空闲回收后净增约 6 条/天，距离上限 64 还有约 11 天余量。",
          fix: "F-01 修复后该累积路径消失；在修复上线前，建议把池上限从 64 临时下调触发告警。",
          diff: null,
        },
      ],
      summary:
        "结论是缓慢累积型，不会立刻打挂线上，但按当前速率约 11 天会到顶。而到顶时没有降级分支，表现是整站超时，不是限流失效。建议把 F-01 提到本周内处理。",
    },

    {
      intro:
        "换一个角度，我把这个变更跟 origin/main 的行为差异做了一次对照。差异只有两点，其中一点是这次重构想修的，另一点是顺带引入的。",
      plan: [
        { label: "对比 origin/main 的两个实现", meta: "1 个文件", duration: "0.5s" },
        { label: "列出行为差异", meta: "2 处", duration: "0.3s" },
      ],
      tools: [
        {
          name: "run_tests",
          arg: "--before origin/main --after HEAD --suite rate-limit",
          meta: "1 处行为回退",
          duration: "24.3s",
          output:
            "同构用例 18/18 结果一致。\n" +
            "差异用例 2 处：\n" +
            "  窗口边界计数   before=limit  after=limit+1   ← 回退\n" +
            "  高负载限流精度 before=±3%    after=±0.4%     ← 改善",
        },
      ],
      findings: [
        {
          id: "F-07",
          severity: "note",
          file: "src/middleware/rate-limit.ts",
          line: 63,
          title: "重构改善了限流精度，但在边界上引入了一个单位误差",
          detail:
            "固定窗口改为滑动窗口后整体精度从 ±3% 提升到 ±0.4%，这是重构的目标。但闭区间写法在窗口边界放宽了一个请求的余量，属于本次顺带引入。",
          evidence: "同构用例下边界计数值 limit → limit + 1。",
          fix: "把左边界改成开区间即可，与 F-02 同源，一并修掉。",
          diff: {
            file: "src/middleware/rate-limit.ts",
            startLine: 62,
            lines: [
              { type: "ctx", num: 62, text: "function inWindow(t: number, windowStart: number, now: number) {" },
              { type: "del", num: 63, text: "  return windowStart <= t && t <= now;" },
              { type: "add", num: 63, text: "  return windowStart < t && t <= now;" },
              { type: "ctx", num: 64, text: "}" },
            ],
          },
        },
      ],
      summary:
        "净效果是正向的：精度大幅提升，代价是边界多算一个请求。修掉 F-02 之后就只剩改善，没有回退。",
    },
  ],

  /* ---------- 运行阶段文案 ---------- */
  phases: {
    reading: "读取变更…",
    planning: "生成审查计划…",
    running: "执行计划…",
    done: "审查完成",
    stopped: "已停止",
  },
};
