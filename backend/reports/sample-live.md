# 代码审计报告

- **审计目标**：审查这个仓库中的缺陷，重点是会导致错误行为、资源泄漏或安全问题的实现。
- **仓库路径**：`C:\Users\34442\Desktop\porject\代码审查agent\backend\examples\vulnerable-service`
- **驱动模型**：`deepseek-flash（DeepSeek）`
- **迭代轮数**：11
- **结束原因**：`finished`
- **耗时**：87.6s
- **Token**：prompt 130003 / completion 14514 / 缓存命中 112896

## 结论

## 审计范围

仓库共 6 个文件（app/ 下 5 个模块 + tests/test_auth.py），已**全量读取**，无未覆盖文件。仓库头注释声明这些模块「故意包含典型缺陷」，缺陷点在行尾自带标注——我未直接采信标注，而是逐条回到代码本身核实机理、触发条件和影响面，并剔除了不成立/不值得报的项。

工作区与 git 历史中出现的 `index.html`、`scripts/auth.js` 并不存在于文件系统内（`read_file` 报 FileNotFound），因此不在可审计范围，未纳入结论。

## 主要发现（15 条：7 blocker / 7 warn / 1 note）

**资源泄漏（rate_limit.py，两条 blocker）**
- `check()` L50-51：命中限流时直接 `return`，绕过 L55 的 `release`。这是限流器的正常业务分支而非异常路径，泄漏必现。
- `check_batch()` L64-68：循环内 `acquire`，归还全部放在循环之后；任一轮 `zcount` 抛错则已取得的连接全不释放。
- 已核实 `acquire()` 只做 `_in_use += 1`，从不校验 `self.limit`（`limit` 仅在 `__init__` 赋值，全仓库无 enforce 点），故泄漏后果是 `in_use` 无限增长。

**认证与会话（auth.py）**
- L28-29 裸 `except` 中 `return True`：认证 fail-open，异常时无条件放行（blocker）。
- L13-14 API Key 与数据库口令明文硬编码（blocker）。
- L34 会话令牌用 `random.randint` 仅 90 万取值空间且非加密安全，L35 还拼接可枚举的 user id（warn）。
- L19 口令摘要用无盐 MD5（warn）；L40 文件句柄未用 `with`（warn）。

**注入与代码执行**
- `utils.py` L27 `eval(text)`、L33 `exec(text, namespace)`：对外部输入，属代码执行级问题（两条 blocker）。
- `repository.py` L9 字符串拼接 SQL、L15 f-string 拼接（blocker）。

**其余**：`guards.py` L17/18/24 用 `assert` 做参数校验，`-O` 下整体失效（warn）；`rate_limit.py` L74 窗口判断左右双闭导致边界重复计数、L78 `remaining()` 可返回负数（warn）；`repository.py` L23 `except ... pass` 静默吞异常（warn）；`utils.py` L9/L18 可变默认参数跨调用共享状态（warn）；`guards.py` L30/35 `== True/False`（note）。

## 显式未报告项

`tests/test_auth.py` 里同样存在 `open` 不带 `with`（L25）与硬编码字符串（L20），但该文件是测试夹具，按测试文件豁免约定未登记为发现——这也与本仓库「验证规则作用域边界」的用途一致。

## 不确定性与局限

- 未运行任何测试或动态验证：全部结论均来自静态阅读。`ConnectionPool` 的 `zcount/zadd/expire` 是返回常量桩，`repository.py` 的 `conn` 为未指定的外部对象，因此 SQL 注入、认证绕过等结论基于「入参来自外部」这一前提——若这些函数仅被内部可信数据调用，实际影响面会缩小，但缺陷本身成立。
- 未评估该仓库在生产中的实际调用方，无法给出真实暴露面的量化判断。

## 发现（共 15 条：阻断 7 / 警告 7 / 建议 1）

### 1. [阻断] 生产凭据（API Key 与数据库口令）硬编码在源码中

- **位置**：`app/auth.py:13`
- **编号**：`F_3275b770bc53`
- **机理**：第 13 行的 API_KEY 与第 14 行的 DB_PASSWORD 以明文常量写在模块顶部。凭据随源码进入版本库后任何有读权限者（含 CI 日志、镜像层）都能拿到，且无法在不改代码的前提下轮换，属于凭据泄露风险；第 12 行导入的 random 也印证这是运行时代码而非文档。
- **证据**：L13：`API_KEY = "sk-live-9f3a7c2b8e1d4056a7b8"`；L14：`DB_PASSWORD = "prod_pg_2026_x7Qm"`。
- **建议**：改为从环境变量或密钥管理服务读取，并立即轮换已泄露的这两组凭据。

### 2. [阻断] verify_user() 裸 except 中返回 True，异常时无条件放行认证

- **位置**：`app/auth.py:28`
- **编号**：`F_6f9c1b7fc565`
- **机理**：第 24-29 行的 try 块内任何异常（例如 user 不是 dict、user.get 不存在、口令编码失败等）都会落到第 28 行的裸 except，而第 29 行返回 True。这使 verify_user 在出错时把「校验失败」变成「认证通过」，是典型的 fail-open 认证绕过；同时裸 except 会连 KeyboardInterrupt / SystemExit 一并吞掉。对比第 27 行的正确逻辑（digest == stored），异常分支的行为与其完全相反。
- **证据**：L24-29：`try:` ... `return digest == stored` / `except:` / `return True`。异常路径的返回值 True 恒定，与口令是否匹配无关。
- **建议**：改为捕获具体异常（如 KeyError/TypeError/ValueError）并返回 False，并避免捕获 BaseException。

### 3. [阻断] check() 命中限流时直接 return，连接未归还导致连接池泄漏

- **位置**：`app/rate_limit.py:50`
- **编号**：`F_42f0088269af`
- **机理**：acquire 在第 46 行取得连接后，第 50-51 行在 hits >= LIMIT 时直接 return，绕过了第 55 行的 release。该分支是限流器的正常业务路径（每个被限流的请求都会走到），并非异常路径，因此连接泄漏会被稳定复现：每来一个超限请求就少一个连接，最终连接池耗尽后新请求全部拿不到连接。此外第 47-54 行整体没有 try/finally，zcount/zadd/expire 任一抛错同样会漏掉 release。
- **证据**：L46 `conn = await pool.acquire()`；L50-51 `if hits >= LIMIT:\n        return {"allowed": False, "hits": hits}`；L55 `await pool.release(conn)` 位于 return 之后，限流分支不可达。
- **建议**：把 return 值改为先赋值，再用 `try: ... finally: await pool.release(conn)` 包裹整个函数体，确保正常分支、限流分支和异常分支都归还连接。

### 4. [阻断] check_batch() 循环内 acquire 且无 try/finally，异常时批量泄漏连接

- **位置**：`app/rate_limit.py:64`
- **编号**：`F_1be21e457a43`
- **机理**：第 63-66 行在循环里逐个 acquire 并存入 sources，归还动作全部放在循环之后的第 67-68 行。一旦第 66 行的 zcount 在任意一轮抛错，函数直接向上抛异常，之前所有已 acquire 的连接都不会被释放；若 keys 较长，一次失败就可能耗尽连接池。这与 check() 是同一类资源生命周期缺陷。
- **证据**：L64 `conn = await pool.acquire()`（循环内）；L66 `results.append(await conn.zcount(key, 0, "+inf"))`；L67-68 `for conn in sources:\n        await pool.release(conn)` 只有在循环全程不抛错时才执行。
- **建议**：用 try/finally 包裹 for 循环，在 finally 中遍历 sources 归还所有已取得连接。

### 5. [阻断] find_user() 将外部输入字符串拼接进 SQL，存在注入

- **位置**：`app/repository.py:9`
- **编号**：`F_fbf023947cb1`
- **机理**：第 9 行用 `+ name +` 把调用方传入的 name 直接拼进 SQL 文本。name 若来自请求参数，输入 `' OR '1'='1` 之类即可改变查询语义，实现注入读取/绕过。第 15 行的 list_orders 用 f-string 拼接 status 和 limit 属同一问题，二者应统一改为参数化查询。
- **证据**：L9：`sql = "SELECT id, name FROM users WHERE name = '" + name + "'"`；L15：`sql = f"SELECT id FROM orders WHERE status = '{status}' LIMIT {limit}"`。
- **建议**：改为 `conn.execute("SELECT id, name FROM users WHERE name = %s", (name,))`，limit 作为参数或先 int() 转换并校验范围。

### 6. [阻断] parse_payload() 用 eval 执行外部传入的文本

- **位置**：`app/utils.py:27`
- **编号**：`F_3c587e2aa6b2`
- **机理**：第 27 行 `return eval(text)` 把任意外部输入当 Python 表达式求值。text 可控时攻击者可执行任意代码（如 __import__('os').system(...)），属于远程代码执行级别的安全问题，而非风格问题。同文件第 33 行的 exec(text, namespace) 是同一类缺陷。
- **证据**：L25-27：`def parse_payload(text: str):` / `return eval(text)`。<br>L33：`exec(text, namespace)`。
- **建议**：用 json.loads 等安全解析替代 eval；若必须处理字面量，用 ast.literal_eval。exec 应彻底移除或改为显式白名单解析。

### 7. [阻断] parse_config() 用 exec 执行外部传入的文本

- **位置**：`app/utils.py:33`
- **编号**：`F_0a9ffc318ff9`
- **机理**：第 33 行 `exec(text, namespace)` 在调用方提供的文本上执行任意 Python 代码，与第 27 行的 eval 属同一类代码执行缺陷。传入的 text 若可控，可读写命名空间中的任意键、导入模块并执行系统命令；返回的 namespace 还会把执行过程中产生的所有变量暴露给调用方。
- **证据**：L30-34：`def parse_config(text: str) -> dict:` / `namespace: dict = {}` / `exec(text, namespace)` / `return namespace`。
- **建议**：配置解析改用 json.loads / tomllib 等受限格式，不要对不可信输入使用 exec。

### 8. [警告] 口令摘要使用 MD5，无盐且计算过快

- **位置**：`app/auth.py:19`
- **编号**：`F_ce5950a41af2`
- **机理**：第 19 行 `hashlib.md5(password.encode()).hexdigest()` 用于口令存储。MD5 计算极快且该实现未加盐，可被彩虹表/GPU 暴力破解；同时 MD5 存在已知碰撞。口令存储应使用专门的慢哈希。
- **证据**：L17-19：`def hash_password(password: str) -> str:` / `return hashlib.md5(password.encode("utf-8")).hexdigest()`。
- **建议**：改用 bcrypt / scrypt / argon2（或 hashlib.pbkdf2_hmac 配每用户随机盐）。

### 9. [警告] 会话令牌用 random.randint 生成，可被预测

- **位置**：`app/auth.py:34`
- **编号**：`F_b6972c1ef87d`
- **机理**：第 34 行用 random.randint 只有 90 万个取值空间，且 random 基于 Mersenne Twister，不是加密安全随机源；结合第 35 行把 user id 明文拼接为 `{id}-{token}`，攻击者可在少量观测后预测或暴力枚举令牌，从而伪造会话。
- **证据**：L32-35：`def issue_session_token(user: dict) -> str:` / `token = random.randint(100000, 999999)` / `return f"{user.get('id')}-{token}"`；L11 `import random`。
- **建议**：改用 secrets.token_urlsafe(32) 生成高熵令牌，且不要把用户 id 作为可枚举前缀。

### 10. [警告] load_roles() 打开文件未用 with，异常时句柄泄漏

- **位置**：`app/auth.py:40`
- **编号**：`F_3068713346c4`
- **机理**：第 40 行 open 返回的 handle 没有用 with 管理，也未见任何 close 调用。第 41 行的 read() 若抛错（编码错误、IO 错误），句柄不会被关闭；即使不抛错，句柄也只能依赖 CPython 的引用计数回收，在其它实现或长生命周期下会累积文件描述符。
- **证据**：L38-42：`handle = open(path, "r", encoding="utf-8")` / `content = handle.read()` / `return {"raw": content}`，全程无 with / close。
- **建议**：改用 `with open(path, "r", encoding="utf-8") as handle: content = handle.read()`。

### 11. [警告] 用 assert 做参数校验，-O 下校验被整体跳过

- **位置**：`app/guards.py:17`
- **编号**：`F_0d4cab408124`
- **机理**：第 17、18、24 行用 assert 做参数校验。assert 在 `python -O` 或设置了 PYTHONOPTIMIZE 时会被整体剔除，导致 limit <= 0、limit > 100000、window_ms < 1000 等非法输入不再被拦截，直接进入限流逻辑（例如负窗口会让窗口起点跑到未来）。校验类断言应改为显式 raise，assert 只用于内部不变量。
- **证据**：L17 `assert limit > 0, "limit must be positive"`；L18 `assert limit <= 100_000, "limit too large"`；L24 `assert window_ms >= 1000, "window too small"`。
- **建议**：改为 `if limit <= 0: raise ValueError(...)` 形式。

### 12. [警告] 滑动窗口边界双闭区间导致重复计数；remaining() 可返回负数

- **位置**：`app/rate_limit.py:74`
- **编号**：`F_8ae3d21fc701`
- **机理**：第 74 行窗口判断用 `window_start <= timestamp and timestamp <= now`，两端都是闭区间。位于 window_start 或 now 这一毫秒上的请求会同时落入相邻两个窗口被计数两次，使限流阈值提前被触发（或边界时序上计数不一致）。限流窗口应统一为半开区间。

第 77-78 行的 remaining() 直接返回 `LIMIT - hits`：第 50 行的判断是 `hits >= LIMIT`，hits 完全可能大于 LIMIT（例如并发写入后），此时 remaining 返回负数。若调用方用它做额度扣减或展示，会出现负额度。
- **证据**：L74 `return window_start <= timestamp and timestamp <= now`；L78 `return LIMIT - hits`；L50 `if hits >= LIMIT:`。
- **建议**：窗口判断改为 `window_start <= timestamp < now`；remaining 改为 `max(0, LIMIT - hits)`。

### 13. [警告] 异常被静默吞掉，调用方无法感知删除/写入失败

- **位置**：`app/repository.py:23`
- **编号**：`F_83f719547d10`
- **机理**：第 23-24 行捕获 Exception 后 `pass`，delete_session 在数据库删除失败时静默返回 None。调用方无法区分「删除成功」与「删除失败」，会话记录可能仍然有效却让上层误以为已登出，属于安全相关的静默失败。第 32-33 行的裸 except 则更宽：它连 KeyboardInterrupt / SystemExit 一起吞掉并返回 False，会掩盖循环中断等控制流异常，也可能让 sync_ledger 在部分行已写入后返回 False 而不做回滚提示。
- **证据**：L21-24：`try:` / `conn.execute("DELETE FROM sessions WHERE token = %s", (token,))` / `except Exception:` / `pass`；L29-33：`except:` / `return False`。
- **建议**：至少记录日志并向上抛出（或返回明确的失败状态），裸 except 改为具体异常类型。

### 14. [警告] append_event/build_index 使用可变对象作默认参数，调用间共享状态

- **位置**：`app/utils.py:9`
- **编号**：`F_e63dadebbe62`
- **机理**：第 9 行 `def append_event(event: dict, target: list = [])` 与第 18 行 `def build_index(items: list = {})` 都把可变对象写成默认参数。默认值在函数定义时创建一次并被所有调用共享，因此 append_event 第二次调用会看到第一次追加进去的元素（docstring 第 12 行自己承认了这一点），build_index 对 items 的原地写回（第 21 行）同样会污染下一次调用，形成跨请求/跨调用的状态残留。
- **证据**：L9 `def append_event(event: dict, target: list = []) -> list:`；L14 `target.append(event)`；L18 `def build_index(items: list = {}) -> dict:`；L21 `items[index] = (index, item)`。
- **建议**：默认值改为 None，在函数体内 `if target is None: target = []`。

### 15. [建议] 用 == True / == False 做开关判断，非布尔入参下语义反直觉

- **位置**：`app/guards.py:30`
- **编号**：`F_c6d2b35b1add`
- **机理**：第 30 行 `return flag == True` 与第 35 行 `return flag == False` 用等值比较代替真值判断。Python 中 `1 == True`、`0 == False` 成立，因此传入 1/0 时会得到与直觉相反的类型混淆结果；compare_flag 对返回 1 的「开关」判定为 True，而 is_disabled 对 0 判定为 True、对 1 判定为 False，两个函数在非布尔入参下语义不闭合。这一项属健壮性问题，影响可控，故定为 note。
- **证据**：L30 `return flag == True`；L35 `return flag == False`。
- **建议**：compare_flag 直接 `return bool(flag)`；is_disabled 用 `return not flag`，若担心非布尔入参可先做显式类型校验。

## 运行提示

以下内容不是审计发现，而是本次运行的健壮性说明：

- 运行中发生 2 次工具失败：
  - list_files: PathEscape - 路径越出工作区：.. -> C:\Users\34442\Desktop\porject\代码审查agent\backend\examples（工作区根目录 C:\Users\34442\Desktop\porject\代码审查agent\
  - read_file: FileNotFound - 文件不存在：scripts/auth.js
- 确定性规则命中但本次未报告的位置（疑似漏报，需人工复核）：
- app/auth.py:14 SEC001 硬编码密钥
- app/repository.py:15 SEC002 SQL 语句字符串拼接
- app/repository.py:24 ERR002 异常被静默吞掉
- app/repository.py:32 ERR001 裸 except
- app/utils.py:18 PY001 可变对象作为默认参数

## 附录

- 静态前缀指纹：`b3c2ce2fff2ded65`
- 静态前缀 token：约 4378
- 轨迹消息数：46

### 可用工具

```
  apply_patch      [写/执行  ] [按需]  对文件做精确的字符串替换补丁（不唯一则拒绝）
  discover_tools   [只读    ] [核心]  按需求检索可用工具，返回 3-5 个候选的完整 schema
  finish           [只读    ] [核心]  结束本次审计并给出总结
  git_diff         [只读    ] [核心]  查看 git 差异（默认工作区未提交改动）
  grep             [只读    ] [核心]  按正则搜索文件内容，返回 路径:行号:内容
  lint_file        [写/执行  ] [按需]  对单个文件做语法与静态检查
  list_files       [只读    ] [核心]  列出目录下的文件（默认跳过 .git / node_modules 等）
  read_file        [只读    ] [核心]  读取文件内容，带行号，支持分段
  read_skill       [只读    ] [按需]  按需读取审计方法论文档（分级披露）
  read_symbols     [只读    ] [按需]  列出 Python 文件的函数/类及签名（比读全文省 token）
  record_finding   [只读    ] [核心]  记录一条审计发现（severity/file/line/title/evidence 必填）
  run_command      [高风险   ] [按需]  执行白名单内的命令（不走 shell）
  run_tests        [写/执行  ] [按需]  运行 pytest，返回失败用例摘要
  update_todo      [只读    ] [核心]  重写任务列表（每项含 text 与 status）
```
