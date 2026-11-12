# 接续本仓库的 Codex 先读这里

- 数据源迁移方向已由用户确定：期货、期权、夜盘使用官方 **WindPy**，不再把 iFinD 作为未来主采集方案。Physical / External 仍是独立模块，不能因这次迁移伪造新日期或改回 iFinD。
- Windows 配置与下一阶段任务见 [docs/WINDOWS_WIND_HANDOFF.md](docs/WINDOWS_WIND_HANDOFF.md)。沿用 trading-desk-wind-API 的 Python 3.12 x64、独立 `.venv`、官方 SDK 和 `WIND_PYTHON_DIR` 约定；两个仓库的虚拟环境、业务代码与本地配置分别维护。
- 用户已接受 Wind 供应商口径，不再要求外部数据数值对照作为发布门槛。源日期、唯一合约、覆盖、单位说明及缺失值检查仍必须保留。
- 这次11月10/11日补数要求期货、期权、已完成夜盘都补齐后 **一起发布**；不要只推期货。期权沿用明确标注的 `core_v1`，不冒充全月份全行权价链。
- 不影响正在运行的 trading desk：不得对其执行退出、注销、重启、取消订阅，不调用共享会话的 `w.stop()` / `cancelRequest()`。采集进程串行、缓存可恢复；遇会话问题先诊断。当前无跨仓库全局互斥，不能宣称并发已解决。
- GitHub 的 `daily.yml` 目前仍为旧 iFinD 入口，只有 `workflow_dispatch`，无 cron；定时由 GPT Automation + Scheduler Bridge 负责。不要未经实现与验证就声称 Actions 已切换 Wind，也不要恢复两条 cron。
- 用户已要求旧iFinD提数设为 **idle**。仓库变量 `IFIND_COLLECTION_STATE=idle`；未设置变量也默认idle。Bridge不派发，daily手动触发也只报告idle；仅显式设为`active`才会恢复旧采集，不得因配置Windows Wind而误开它。手动交易所备份入口独立保留。此状态不表示Wind自动调度已上线。
- 用户进一步要求停止远端所有日常取数触发入口：GitHub上的`chatgpt-trigger.yml`和`daily.yml`都已设为`disabled_manually`。上面的idle是二次防护；当前手动备份也不会运行（代码保留）。不要自行重新启用工作流或切换`active`，Windows Wind配置不等于允许恢复远端旧采集。
- `scripts/backfill_wind_*.py` / `publish_wind_backfill.py` 是本次短窗口补数工具，含806合约稳定目录的限制；`audit_wind.py` 是固定2026-11-11的诊断。后续每日生产必须先实现按目标日目录刷新和动态覆盖检查，不能硬编码沿用此次目录。
- 新的本地每日入口已实现：`scripts/run_wind_daily.py`，详见[docs/LOCAL_WIND_DAILY.md](docs/LOCAL_WIND_DAILY.md)。逐日目录、断点缓存、漏日整批发布、早间夜盘、自动提交推送及Dot登录通知均走此入口；不要再把上述固定窗口脚本当作每日入口。用户已授权校验后自动推送data。
- 本地默认disabled，Windows接手后配置独立环境、enabled=true并实机验收，再安装当前用户登录任务。Dot通知协议为`--signal-login`；本机watch收到有效通知后才尝试连接，有看板占用则等待。不要把进程存在当作已登录，不启用远端旧workflow；也不要宣称已替用户开启Dot监控。
- 本机登录凭据、`.venv`、`.pth`、SDK、原始 `tmp/` 缓存不提交；验证后的规范化 `data/` 按本仓库既有方式提交。推送前核对远端，保留他人提交，不强推。
- 验证：`python -m unittest discover -s tests -q`；发布后 `python -m china_commodities.cli report-input --repair-futures-history`、`python -m china_commodities.cli validate`。Windows 实机未验证的内容要明确标注。

## 2026-11-12 用户授权更新（优先于上文旧端口限制）

用户明确要求允许 Trading Desk 运行时低速取数。新增显式 `allow_dashboard=true` 模式，
详见 `docs/WIND_DASHBOARD_COEXISTENCE.md`；默认旧模式保持兼容。
股票/商品日常原生 worker 已使用同一跨仓库本机用户锁；不得声称覆盖所有旧 backfill 或跨机器。
不得为了共存关闭/重启/注销看板或 Wind；小样本验收后才把共存模式接入一键启动器。

11月12日晚用户进一步授权商品与其他采集任务并行测试：显式
`allow_parallel_collectors=true`可让商品worker不持有跨仓库锁；不删除锁文件，
本仓库重复运行锁保留。用户本次同时选择`request_interval_seconds=0`，即串行但无额外等待。
默认配置仍保守；未实际观察两库同时取数，不得声称跨采集任务并行已验证。
