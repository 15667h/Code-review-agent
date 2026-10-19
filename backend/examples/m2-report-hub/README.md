# m2-report-hub

多租户报表聚合服务（Python 标准库）。考的是路径穿越、报表 DSL 注入、
租户归属校验缺失、数据一致性与导出任务的状态机，并为静态检测规则留了
其中静态规则只有 3 处能直接命中。

## 刻意布置的干扰项

- `app/ops_api.py` 里 `ls` 与诊断脚本都通过 `shlex.split` + `shell=False`
  执行，参数来自白名单字典与 `int()` 转换后的值，不存在命令注入；
- `app/api.py` 的 `_translated` 把 `PermissionDenied`/`NotFoundError`
  统一翻译成 403/404，权限异常不会因为缺少 except 而冒泡；
- `app/cache.py` 的 `get_or_load` 是带单飞锁的正确实现（防击穿），
  注释里出现「并发」「锁」不构成缺陷；
- `app/sources.py` 的 `SqlSource` 表名只来自配置，SQL 文本里没有任何插值；
- `app/export.py` 的 `sanitize_name` 是白名单（只保留字母数字与 `-`、`_`），
  运营侧自行修补过的调用点用它拼路径，不能反过来当成「已经有防护」的依据；
- `app/aggregate.py` 中「多源取最大而不是求和」是刻意的业务选择
  （镜像源，不是分片），不是重复计数缺陷。
