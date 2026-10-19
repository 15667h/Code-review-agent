# m1-order-desk

小型电商订单后台（Python 标准库 + 伪 ORM）。考的是在一套真实的
handler → service → repository → store 分层里，找出资源泄漏、边界条件
缺失、并发写交错与错误处理不当，以及一条从请求参数一路走到响应体的越权读取。

## 刻意布置的干扰项

- `app/config.py` 用 `ast.literal_eval` 解析模板配置（而不是 `eval`），
  看起来像反序列化危险，但解析结果只用于取键名；
- `app/hooks.py` 用 `subprocess.run` 调本地通知脚本，参数是列表、`shell=False`，
  订单号只作为单个 argv 元素传递，命令注入不成立；
- `app/orm.py` 的 `reserve_stock` 是标准的乐观锁读改写，注释里提到「版本号」
  容易让人把它和缺陷混为一谈，但它本身的语义是完整的；
- `app/repository.py` 的 `find_for_owner` 是正确写法，与只按 id 取单的
  `find` 并存，用来区分「有没有真的看归属校验」；
- `app/ledger.py` 每条流水都 `with open(...)` 独立落盘，不再长期持有句柄；
- `app/inventory.py` 的 `_reserve_one` 重试循环、`app/pricing.py` 的
  `split_refund` 余数处理都是正确的边界实现。
