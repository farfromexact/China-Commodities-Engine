# 本地Wind每日采集、登录通知与漏日补采

> 2026-11-12 更新：新增用户授权的显式看板共存模式，见 [共存采集](WIND_DASHBOARD_COEXISTENCE.md)。下文端口阻挡/独占条件适用于默认旧模式。

远端 `daily.yml`、`chatgpt-trigger.yml` 继续禁用，`IFIND_COLLECTION_STATE=idle`继续保留。
此入口只在本机运行官方WindPy，不触发Actions、不启用旧iFinD、不修改trading desk。
三层校验通过后自动提交并推送已获用户授权；仅提交data，不提交本机配置/凭据/缓存。

## 运行方式

`scripts/run_wind_daily.py` 是监督进程；原先固定日期的 `backfill_wind_*.py` 继续保留为历史工具。

- `--plan`：只读检查缺失日期，不登录、不请求行情、不运行Git同步。
- `--status`：读取本地状态，适合Dot监控。
- `--signal-login`：Dot/用户确认官方窗口已经登录后发送一次通知，不包含账号密码，也不会立即登录或启动取数。
- `--once`：检查一次，恢复待推送结果、同步远端、判断任务、检查看板占用和登录通知，然后按需运行。
- `--watch`：每60秒执行上述检查，适合Windows登录后常驻。
- `--init`：创建本机配置，默认enabled=false；不会自动启用或覆盖配置。

WindPy `isconnected()`检查当前Python进程，不是跨进程桌面登录探测。因此不凭“Wind进程存在”宣称已登录。
登录通知有效期默认15分钟，成功启动前消费一次；只有收到通知且确有缺失数据、看板未占用时才执行一次`w.start(waitTime=15)`。
连接失败不反复弹框、不自动重连、不存密码。等待15分钟退避及新的登录通知；首次官方认证窗口由用户处理。
这不保证未登录时完全无官方弹框，Windows静默连接必须实机验收。

## Windows明天的配置步骤

先按 [Windows交接说明](WINDOWS_WIND_HANDOFF.md) 安装本项目独立Python3.12 x64环境和官方SDK。
拉取最新代码后，在本仓库根目录PowerShell运行：

```powershell
.\.venv\Scripts\python.exe scripts\configure_wind.py --check
.\.venv\Scripts\python.exe scripts\configure_wind.py
.\.venv\Scripts\python.exe scripts\run_wind_daily.py --init
.\.venv\Scripts\python.exe scripts\run_wind_daily.py --plan
```

编辑 `tmp\wind-daily\config.json`：

- 确认 `desk_host=127.0.0.1`、`desk_port=8765` 与本机trading desk实际监听地址一致。
- 设置 `enabled=true`。`start_date=null` 会以第一次运行时仓库最新EOD日期为补采起点并记住它；若需要更早回补，显式设置起点。不会无界扫描十年历史。
- `branch=main`；确认当前主分支干净、Git作者配置及推送凭据可用。无需把Git凭据填进本配置。
- `sdk_dir=null`沿用项目WindPy.pth / WIND_PYTHON_DIR；不得复制另一项目的虚拟环境。
- 默认每次最多补5个缺失交易日、请求间隔1秒、单次原生请求120秒超时、整批2小时上限。超时只停止本次工作进程，缓存保留。
- `include_foundation=true`保留Physical/External的AKShare独立采集；它们缺失/超时如实记录并保留旧有效快照，不冒充当日数据，也不改用iFinD。

登录Wind后，在trading desk没有占用时先验证一次：

```powershell
.\.venv\Scripts\python.exe scripts\run_wind_daily.py --signal-login
.\.venv\Scripts\python.exe scripts\run_wind_daily.py --once
.\.venv\Scripts\python.exe scripts\run_wind_daily.py --status
```

确实有缺失数据才会请求Wind；已齐全则up_to_date，不为了测试重拉。验证通过后安装登录启动任务：

```powershell
& .\scripts\install_wind_daily_task.ps1
Start-ScheduledTask -TaskName 'ChinaCommodities-WindDaily'
```

任务使用当前用户交互登录会话、普通权限，不存密码；开机未登录Windows时不能取数。
如本机PowerShell策略限制脚本执行，由Windows Codex按办公环境规则处理，不擅自修改全局策略。
没有在Mac上注册常驻任务，也没有在Windows实机执行本安装器。

暂停本地采集：先把配置`enabled=false`（在途工作不会被强杀，下次检查生效）；紧急停止本任务可用
`Stop-ScheduledTask -TaskName 'ChinaCommodities-WindDaily'`。移除登录任务：
`& .\scripts\install_wind_daily_task.ps1 -Remove`。这些命令不操作trading desk。

## 采集与发布规则

1. 北京时间18:15前只补已完成日盘；当天已完成夜盘可在早上单独更新。18:15后当天日盘加入队列，周末/节假日不制造新交易日。日历未知年份会停止并要求维护，不猜测交易日。
2. 漏日按时间顺序补：每个交易日分别获取WSET期货目录，不再依赖固定806合约；只允许配置里明确的空目录例外（目前CZCE:LR）。目录覆盖仍以config/products.json为范围，不宣称自动发现所有新上市品种。
3. 期货WSD、core_v1期权WSET/WSD、对应已完成夜盘WSI串行；新上市合约没有前日参考时收益为空，不借用其他合约或未来日盘。节前无夜盘的交易日明确记no_scheduled_night_session。
4. 原始请求按方法/参数哈希缓存；有效缓存可断点恢复。日期不匹配或结算未就绪的EOD不会永久缓存失败结果。WSI 对单一合约返回 `-40520007`（无可用数据）时，缓存供应商错误码并按空 bars 生成 `no_night_trade`，不伪造夜盘；其他分钟接口错误仍阻断整批。
5. 全部计划批次三层成功后生成统一报告，再发布；任何一层失败不提交半批结果。Physical/External维持原有“可缺失、明确标注”的规则。历史补洞不回退较新的latest指针。
6. 成功日期写入 `data/wind_daily/completed/` 回执，避免快照正常滚动清理后被当成新缺口；首次已完成起点也保存在本机anchor.json。
7. 自动发布使用独立本地Git副本，只提交data，然后普通push并快进同步主工作区。主工作区有未提交修改则等待；远端并发更新不强推、不自动解决数据冲突。
8. 推送失败保留outbox，下一次先重试已有提交，不重新请求Wind。不要手动删除未解决的outbox.json/outbox；让Codex查看状态并处理分支冲突。已推送而本地同步失败时也保留日志，先恢复同步。

## trading desk优先与多机器限制

每次连接与原生请求前检查本地看板端口。端口有服务、检查结果不确定或看板在途中启动时，采集等待/中止后续请求；已经在途的原生调用不能瞬间抢占，最长受请求超时限制。
不调用w.stop/cancelRequest，不关闭或启动Wind/trading desk，不修改看板配置。此处是保守避让，不是两个项目共享一个Wind队列的实现。
**如果看板全天运行，本采集会一直等待。** 需要使用看板空闲窗口，或后续另行配置独立授权采集机/统一请求队列；不能通过关闭端口检查“解决”。
锁只防止本仓库本机重复运行，不能解决不同电脑的同账号登录限制。只启用一台日常采集电脑；Mac/Windows账号能否同时在线仍需按实际授权验证。

## 交给Dot的说明（需要本机命令执行权限）

可以把下面这段作为Dot的持续监控任务说明。这里只提供调用协议，未替用户在Dot中创建或开启监控任务。
仅有网页浏览权限的Dot不能执行本机命令；不要因此开放无认证网络控制接口。

> 在这台Windows电脑监控China-Commodities-Engine的本地采集状态。先读仓库AGENTS.md和docs/LOCAL_WIND_DAILY.md。
> 使用仓库自身.venv的Python运行 scripts/run_wind_daily.py --status；需要判断缺口时运行--plan。
> 不启用GitHub Actions/Bridge，不运行iFinD，不操作trading desk。只有你实际看到官方Wind窗口已登录，且状态为waiting_login_signal、trading desk未占用时，才运行一次--signal-login。
> 已安装--watch时无需再启动一个watch；它将在下一轮（默认60秒内）检查通知。若没有常驻任务，可在发送通知后运行--once。
> running时不要重复启动；up_to_date保持安静；desk_busy等待，不关看板；retry_wait遵守retry_after，之后重新确认登录再通知，不连续重试；blocked或outbox_retained时报告具体错误并保留缓存，不删数据、不强推。
> 只在完成发布、持续失败或需要用户操作时通知用户。不要仅凭Wind进程存在判断登录成功；登录窗口、验证码和账户问题交给用户。

## 状态和验收

本机文件均在Git忽略的`tmp/wind-daily/`：config.json、status.json、progress.json、worker.log、foundation.log、raw/、outbox.json/outbox。
`--status`是简要阶段，长请求期间的具体操作见progress.json；日志不应写账号密码。
当前代码经过离线假SDK/本地Git远端测试，并用11月11日真实缓存完整回放806期货合约、64品种核心期权和夜盘；该回放没有加载SDK、没有网络取数、没有推送数据。
Windows首次实际连接、登录任务、休眠恢复和Dot本机执行权限仍须在那台电脑验收，不能称为已部署完成。
