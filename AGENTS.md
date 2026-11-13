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

## 2026-11-12 后续默认更新（优先于上述旧默认）

用户要求此后日常默认`allow_dashboard=true`、`allow_parallel_collectors=true`、
`request_interval_seconds=0`，一键启动器同时运行股票与商品，分别校验和发布。
本仓库runner.lock、股票daily/session锁和启动器锁仍保留；不启用后台任务或远端触发。
不改变Trading Desk代码、进程、订阅或登录，不调用stop/cancel/logout。
启动器维护源在`scripts/wind_oneclick.py`，安装副本及Windows准备包需保持一致。


## 最新用户决定：停止股票联动

用户已要求股票仓库自行处理：商品一键启动器只调用商品入口，只校验/提交/推送商品数据；不读取股票配置或profile，不触发股票采集、上传或重试，不依赖股票虚拟环境。此决定覆盖上述双仓库并行启动的旧安排。股票已有数据与待推送提交原样保留，不修改stock仓库。商品0秒额外等待、自身防重复锁、数据校验与自动推送保留，Trading Desk及远端触发状态不动。


## 2026-11-13 最新用户决定：移除 runner.lock

用户明确要求移除商品每日入口的 `runner.lock` 本机防重复锁。`run_wind_daily.py` 不再创建或获取该锁，相关 `single_runner` 实现已删除；此决定覆盖上文保留该锁的旧说明。启动器自己的锁和数据校验不在此次变更范围。


## 2026-11-13 最新用户决定：只保留启动、提数、同步 Git

用户要求精简，本节覆盖上文的旧启动/调度约定。双击根目录 START_WIND.cmd 或运行 scripts/run_wind_daily.py 即直接同步、连接 Wind、补数、校验、自动提交并推送 data，然后退出。
已移除 runner/launcher 锁、YES 确认、Dot 登录通知、enabled 门槛、watch/登录任务安装器、重试冷却和监督 worker；不再把进度/状态文件写入当发布门槛。日常入口不再附带 AKShare 或盘中 provisional 请求，Physical/External 独立功能及既有快照保留。
保留日期/合约/覆盖/缺失检查、断点缓存、待推送恢复和普通 Git 推送。用户不接受每次操作都跑全仓测试：按实际改动做必要的定向检查，日常提数只做数据校验。


## 2026-11-13 最新用户决定：按启动时间采集盘中/日终

用户已授权：盘中启动提取启动时间减 5 分钟的完整分钟快照，收盘后提取日终数据，校验后自动提交推送；Mac/Windows 使用同一 Python 入口，分别通过 START_WIND.command / START_WIND.cmd 启动。此规则覆盖上文“不采集盘中”的旧说明。
盘中截止时间在启动时固定，向下对齐 5 分钟边界；记录实际行情时间，缺失不使用旧收盘价填充。盘中快照独立存 data/intraday/，正式 EOD 不覆盖；报告 current_market_snapshot 指向较新且合格的数据。盘中优先快照，日终模式补历史缺口。保持前台一次运行，不恢复锁、通知、后台监督或远端触发。
