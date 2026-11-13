# Chat 小文件入口

固定本次商品仓 Git Commit 后，先读 `data/chat/manifest.json`，再按其中真实路径读取。
所有导出 JSON 不超过 48 KiB（UTF-8 字节）；这是本项目的文件大小约定，不代表已在所有 Chat 连接器实测通过。

1. 读 `overview.json`：当前行情模式、模块日期、期权能力与执行限制。
2. 读 `report/index.json`，取得 `sections` 包含 `products` 的所有分片，完成全品种初筛；同时读取 `quality`、`options` 等适用模块，不能只看核心20品种或排行。
3. 重点品种按索引的 `product_keys` 下钻期货、Market State、夜盘、合约参数和 `options_交易所_品种`。执行报价位于 report 的 `execution_prices` 分片，仍为 EOD 参考，不因导出变为实时可成交报价。

每个模块 index 记录原件路径/Hash、日期、列表条数、品种及分片路径/字节数/Hash。原件JSON的Hash统一按LF换行计算，gzip按压缩字节计算，详见source_hash_basis；content_sha256核对解析后的完整内容。导出目录禁用Git换行转换，分片Hash在Windows/Mac及远端保持一致。
顶层 manifest 很小，不放全量行情。完整原件继续保留。

## 文件内容

分片为 `{"fragments":[{"path":["products",0],"value":{...}}]}`。
`path` 是原 JSON 的键/数组下标；`value` 是未经改写的原值。
过大的对象/数组先给 `container: "object"/"array"`，后续按顺序填入其子项。
小对象整体保留，空值、空数组、单位和质量说明均保留。需要还原完整模块时按 index.parts 顺序读全，不能用选择性下钻冒充全量取得。
期权 gzip 分片转成普通 JSON；原压缩文件 Hash 与解压内容 Hash 分开记录。

同一批所有文件必须从同一固定 Commit 读取，实际 Commit 由读取方记录；不向提交内写一个无法自引用的“本提交 SHA”。
不要凭模型重新抄写 JSON 后声称通过原字节 Hash 校验。文件可直接读取但无法原样落盘时，可继续已取得的局部研究，明确未完成的计算/校验。

## 时间含义

`current_market_snapshot` 指定当前 EOD 或盘中快照。`intraday` 现在只表示该指针选中了盘中；`has_night_session` 单独表示汇总包含夜盘。
收益、RV、OI 等原有指标仍是 EOD 口径；`frequency` 描述这些基础模块，不代表指针选择的最新行情频率。
盘中 5分钟 bar 成交量不等于全天量，实际行情时间不等于截点，缺结算/OI/Greeks继续为空。
夜盘按源字段 `price_semantics` / `ohlcv_scope` 解释；Wind 的 `last_completed_bar_close` / `completed_night_5min_bars` 不套用旧实时供应商快照口径，也不冒称交易所核验收盘。
不同模块可合法保留不同日期。`data_fresh` 是原模块状态，不自动证明适用于本次研究截点。

## 更新

正常 `report-input` / Wind 日常发布自动生成导出，不另加启动步骤。
仅从当前已有文件重新导出（不提数、不联网）：

```powershell
.\.venv\Scripts\python.exe scripts/export_chat_data.py
```

检查只做文件大小和拆分还原相等；Hash 用于读取方核对。缺模块明确列出，已声明但缺失的期权分片报错，不静默漏掉。
没有新增锁、调度、重试或远端触发。真实 Chat 的读取、落盘和报告归档仍须在对应环境验收。
