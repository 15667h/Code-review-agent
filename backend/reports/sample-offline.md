# 代码审计报告

- **审计目标**：审查这个仓库中的缺陷，重点是会导致错误行为、资源泄漏或安全问题的实现。
- **仓库路径**：`C:\Users\34442\Desktop\porject\代码审查agent\backend\examples\vulnerable-service`
- **驱动模型**：`heuristic-offline（规则驱动）`
- **迭代轮数**：16
- **结束原因**：`finished`
- **耗时**：0.0s
- **Token**：prompt 0 / completion 0 / 缓存命中 0

## 结论

离线规则模式检查了 6 个 Python 文件，记录 19 条问题。该结论来自确定性规则集，只覆盖规则能表达的模式（密钥硬编码、SQL 拼接、异常吞掉、句柄未关闭、可变默认参数等），不包含跨文件数据流与业务逻辑缺陷 —— 用真实模型跑时覆盖面会更广。

## 发现（共 19 条：阻断 4 / 警告 8 / 建议 7）

### 1. [阻断] 硬编码密钥（SEC001）

- **位置**：`app/auth.py:13`
- **编号**：`F_c040ef417f85`
- **机理**：源码中直接写入了密码、令牌或 API Key。一旦仓库公开或进入交付物，凭据即泄露。
- **证据**：API_KEY = "***"
- **建议**：改为从环境变量或密钥管理服务读取，并把该凭据立即轮换。

### 2. [阻断] 硬编码密钥（SEC001）

- **位置**：`app/auth.py:14`
- **编号**：`F_1e86f2217599`
- **机理**：源码中直接写入了密码、令牌或 API Key。一旦仓库公开或进入交付物，凭据即泄露。
- **证据**：DB_PASSWORD = "***"
- **建议**：改为从环境变量或密钥管理服务读取，并把该凭据立即轮换。

### 3. [阻断] SQL 语句字符串拼接（SEC002）

- **位置**：`app/repository.py:9`
- **编号**：`F_46232156972d`
- **机理**：把变量直接拼进 SQL 文本，输入可控时会形成注入点。
- **证据**：sql = "SELECT id, name FROM users WHERE name = '" + name + "'"
- **建议**：改用参数化查询（占位符 + 参数元组），不要用 f-string 或 % 拼接 SQL。

### 4. [阻断] SQL 语句字符串拼接（SEC002）

- **位置**：`app/repository.py:15`
- **编号**：`F_192d4879f360`
- **机理**：把变量直接拼进 SQL 文本，输入可控时会形成注入点。
- **证据**：sql = f"SELECT id FROM orders WHERE status = '{status}' LIMIT {limit}"
- **建议**：改用参数化查询（占位符 + 参数元组），不要用 f-string 或 % 拼接 SQL。

### 5. [警告] 裸 except（ERR001）

- **位置**：`app/auth.py:28`
- **编号**：`F_571752e8ee89`
- **机理**：`except:` 会同时捕获 KeyboardInterrupt 与 SystemExit，使程序无法被正常中断，也掩盖真实错误类型。
- **证据**：except:
- **建议**：捕获明确的异常类型；若确实需要兜底，用 `except Exception:` 并记录日志。

### 6. [警告] 文件句柄未使用 with 管理（RES001）

- **位置**：`app/auth.py:40`
- **编号**：`F_2d6c32c78a2d`
- **机理**：直接 `open()` 赋值给变量时，异常路径上不会关闭文件描述符，长期运行会耗尽句柄。
- **证据**：handle = open(path, "r", encoding="utf-8")
- **建议**：改用 `with open(...) as fh:`；若必须手动管理，请放进 try/finally。

### 7. [警告] 异常被静默吞掉（ERR002）

- **位置**：`app/repository.py:24`
- **编号**：`F_0b4e1dacdb1b`
- **机理**：`except ...: pass` 把错误完全丢弃，故障会以更晚、更难定位的形式出现。
- **证据**：except Exception: → pass
- **建议**：至少记录一条日志，或把异常向上抛出。

### 8. [警告] 裸 except（ERR001）

- **位置**：`app/repository.py:32`
- **编号**：`F_3cff30bd6e43`
- **机理**：`except:` 会同时捕获 KeyboardInterrupt 与 SystemExit，使程序无法被正常中断，也掩盖真实错误类型。
- **证据**：except:
- **建议**：捕获明确的异常类型；若确实需要兜底，用 `except Exception:` 并记录日志。

### 9. [警告] 可变对象作为默认参数（PY001）

- **位置**：`app/utils.py:9`
- **编号**：`F_9ea4618efe8c`
- **机理**：默认参数在函数定义时求值一次并在调用间共享，多次调用会累积同一个列表/字典。
- **证据**：def append_event(event: dict, target: list = []) -> list:
- **建议**：默认值用 None，在函数体内 `if x is None: x = []`。

### 10. [警告] 可变对象作为默认参数（PY001）

- **位置**：`app/utils.py:18`
- **编号**：`F_72c26dac23cb`
- **机理**：默认参数在函数定义时求值一次并在调用间共享，多次调用会累积同一个列表/字典。
- **证据**：def build_index(items: list = {}) -> dict:
- **建议**：默认值用 None，在函数体内 `if x is None: x = []`。

### 11. [警告] 使用 eval / exec（SEC003）

- **位置**：`app/utils.py:27`
- **编号**：`F_c4631a056c30`
- **机理**：动态执行字符串代码，输入可控时等同于任意代码执行。
- **证据**：return eval(text)
- **建议**：改用显式解析（ast.literal_eval、json.loads）或白名单分派表。

### 12. [警告] 使用 eval / exec（SEC003）

- **位置**：`app/utils.py:33`
- **编号**：`F_5fa7969c9b7f`
- **机理**：动态执行字符串代码，输入可控时等同于任意代码执行。
- **证据**：exec(text, namespace)
- **建议**：改用显式解析（ast.literal_eval、json.loads）或白名单分派表。

### 13. [建议] 生产代码中的 assert（PY002）

- **位置**：`app/guards.py:17`
- **编号**：`F_21625cbe143a`
- **机理**：Python 以 -O 运行时 assert 会被整体移除，用它做参数校验会失效。
- **证据**：assert limit > 0, "limit must be positive"
- **建议**：改成显式的 if + raise，或用类型/校验库。

### 14. [建议] 生产代码中的 assert（PY002）

- **位置**：`app/guards.py:18`
- **编号**：`F_6d746a35c655`
- **机理**：Python 以 -O 运行时 assert 会被整体移除，用它做参数校验会失效。
- **证据**：assert limit <= 100_000, "limit too large"
- **建议**：改成显式的 if + raise，或用类型/校验库。

### 15. [建议] 生产代码中的 assert（PY002）

- **位置**：`app/guards.py:24`
- **编号**：`F_f0878f72687a`
- **机理**：Python 以 -O 运行时 assert 会被整体移除，用它做参数校验会失效。
- **证据**：assert window_ms >= 1000, "window too small"
- **建议**：改成显式的 if + raise，或用类型/校验库。

### 16. [建议] 与布尔常量直接比较（PY003）

- **位置**：`app/guards.py:30`
- **编号**：`F_42f21c0abbbf`
- **机理**：`x == True` 是值比较而非真值判断，传入 1、'yes' 等真值时行为与直觉不符。
- **证据**：return flag == True
- **建议**：直接写 `if x:` / `if not x:`；若必须区分 True 与其他真值，用 `is True`。

### 17. [建议] 与布尔常量直接比较（PY003）

- **位置**：`app/guards.py:35`
- **编号**：`F_5d871746b44a`
- **机理**：`x == True` 是值比较而非真值判断，传入 1、'yes' 等真值时行为与直觉不符。
- **证据**：return flag == False
- **建议**：直接写 `if x:` / `if not x:`；若必须区分 True 与其他真值，用 `is True`。

### 18. [建议] 资源获取后未见 finally 归还（RES002）

- **位置**：`app/rate_limit.py:46`
- **编号**：`F_ce28af0d8bed`
- **机理**：acquire/checkout 之后如果异常路径没有归还，资源占用会随异常频率单调增长。静态上看不出完整控制流，这里只做提示。
- **证据**：conn = await pool.acquire()
- **建议**：把获取与归还收进 try/finally，或改用上下文管理器（with）。

### 19. [建议] 资源获取后未见 finally 归还（RES002）

- **位置**：`app/rate_limit.py:64`
- **编号**：`F_13afd0268841`
- **机理**：acquire/checkout 之后如果异常路径没有归还，资源占用会随异常频率单调增长。静态上看不出完整控制流，这里只做提示。
- **证据**：conn = await pool.acquire()
- **建议**：把获取与归还收进 try/finally，或改用上下文管理器（with）。

## 附录

- 静态前缀指纹：`b3c2ce2fff2ded65`
- 静态前缀 token：约 4378
- 轨迹消息数：47

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
