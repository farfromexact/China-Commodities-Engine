# 启动、Wind 提数、同步 Git

先登录官方 Wind，然后双击仓库根目录的 **START_WIND.cmd**。
也可以在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe scripts\run_wind_daily.py
```

程序一次运行完成：同步远端 → 补齐已完成交易日及夜盘 → 校验 → 自动提交并推送 `data/` → 退出。
已有数据齐全时不重复请求 Wind。漏日逐日刷新合约目录，期权使用明确标注的 `core_v1`。
当天日盘收盘前只补历史及已完成夜盘，不再额外采集不会发布的盘中候选；收盘后源日期、结算等字段合格才发布当天 EOD。

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

2026-11-13 旧入口已实机完成夜盘采集推送（806 目录、503 有 bars、303 无成交、0 未解决）。精简入口经过模拟采集/发布分支检查，尚未重新实机提取行情。
