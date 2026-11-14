# 启动、Wind 提数、同步 Git

先登录官方 Wind，Windows 双击仓库根目录的 **START_WIND.cmd**，Mac 使用 **START_WIND.command**。两边调用同一个 Python 入口。
也可以在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe scripts\run_wind_daily.py
```

程序一次运行完成：同步远端 → 按启动时间选择盘中或日终数据 → 校验 → 自动提交并推送 `data/` → 退出。

发布时同时更新 `data/chat/manifest.json` 及小文件分片，供 Chat 直接读取；无需另行启动，详见 [Chat 数据入口](CHAT_DATA.md)。

- 盘中（北京时间 09:00–15:00，含午间休市；已安排夜盘的 21:00–次日 02:30）：提取启动时间减 5 分钟、再向下对齐到 5 分钟边界的完整行情。例如 10:20 启动取截至 10:15，10:23 启动也取截至 10:15。时间在 Git 同步前固定，采集中跨过收盘也不改变模式。不同品种结束交易时间不同，每条数据保留真实行情时间及落后截止时间的秒数；休市或无成交不伪造报价。
- 日盘收盘后及其他非盘中时间：沿用已完成日盘/夜盘补采，数据齐全则只同步 Git。历史缺口在此模式补齐；盘中启动优先取本次快照。
- 刚开盘尚无符合延迟要求的完整 bar 时明确提示，重新启动即可。

盘中期货目录按目标交易日取 Wind WSET；期权沿用 `core_v1`，以本次期货价格选择平值附近样本，历史持仓量只用于选择活跃标的并注明日期。价格与成交量来自 Wind WSI，成交量是该根 5 分钟 bar 的成交量，不是全天累计；结算价、持仓量和希腊值在盘中快照明确为空。没有对应行情的合约记录 missing/query_error，不用旧收盘价补位。

盘中数据保存为 `data/intraday/latest.json` 和每交易日一份 `data/intraday/YYYY-MM-DD.json`，不覆盖正式 EOD。`data/report_input_latest.json` 的 `current_market_snapshot` 给出当前应读取的文件路径、模式和截止时间；其他原有日终分析字段仍保持日终口径。正式 EOD 到齐后，该指针自动回到日终数据。

漏日逐日刷新合约目录，日终期权仍为 `core_v1`；源日期、结算等字段合格才发布 EOD。

周末启动也会检查已结束的夜盘。周五晚至周六凌晨的夜盘按下周一交易日保存，实际起止日期单独记录；例如 2026-11-13 晚的夜盘保存为 `night_session/2026-11-16.json`，日终数据仍保持 11 月 13 日。沿用夜盘窗口完成检查，节前无夜盘及已经采集完成的批次不会重复请求。

启动时直接连接 Wind，不需要 YES、Dot 通知、enabled 开关、锁文件、15 分钟冷却或后台监督进程。
没有 watch、开机常驻任务或 `progress.json` 进度文件依赖。结果直接显示在终端，状态文件写入失败不影响提数和推送。
Wind 返回连接或数据错误时本轮退出，修正后重新启动；同步 SDK 请求耗时由 Wind 决定，没有后台监督进程强制中止。

保留按请求缓存，失败后下次复用已完成请求；已生成提交但推送失败时，下次先恢复推送。
日期、来源、合约身份、覆盖与缺失字段校验继续执行。Git 只自动提交数据；代码有未提交修改或远端冲突时明确报错，不强推。
Physical / External 保留独立 CLI 和已有快照，不再随 Wind 入口附带请求 AKShare。股票仓库、Trading Desk 和远端旧采集工作流均不由此入口操作。

只查看缺失日期（不连接 Wind 或 Git）：

```powershell
.\.venv\Scripts\python.exe scripts\run_wind_daily.py --plan
```

独立 `.venv` 与官方 SDK 的安装见 [Windows 环境说明](WINDOWS_WIND_HANDOFF.md)。
本机可选配置位于忽略目录 `tmp/wind-daily/config.json`：`start_date`、`max_days_per_run`、`request_interval_seconds`、`branch`、`sdk_dir`、`allowed_empty_products`。
旧 enabled、登录通知和调度字段被忽略。缺省每批最多 5 个交易日；有更多缺口时本次启动继续分批完成。
凭据、SDK、虚拟环境和原始缓存不提交。

2026-11-13 旧入口已实机完成夜盘采集推送（806 目录、503 有 bars、303 无成交、0 未解决）。盘中分支经过时间边界、分钟数据、缺失/越界、日终回归和自动发布的定向检查；新增盘中完整流程尚未在 Windows/Mac 实机采集验证。
