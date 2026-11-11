# Windows Wind 接续说明

**最新接续入口：** [本地每日采集与Dot通知](LOCAL_WIND_DAILY.md) 已实现日常逐日目录、漏日补采、早间夜盘和自动推送。明天优先按该文档配置 `run_wind_daily.py` 及当前用户登录任务。下文历史补数工具与原迁移待办保留作背景；远端Actions/Bridge继续关闭，不需要为本地采集恢复远端触发。

2026-11-11交接。用户要求未来在办公Windows电脑使用Wind取数，并保留原trading desk的运行。
本说明参考 [trading-desk-wind-API](https://github.com/farfromexact/trading-desk-wind-API)
的 `scripts/configure_wind.py`、`docs/CROSS_PLATFORM.md` 和Windows历史运行记录。
这是同一个商品数据仓库的跨平台配置，不需要复制仓库或创建长期Windows分支。

## 已完成与尚未完成

- 本机Mac已实测官方WindPy的WSD期货/期权历史数据、WSET目录、WSI历史分钟线；本次回补11月10/11日三层数据。数量与最终发布结果见 [验证记录](WIND_VALIDATION_20261111.md)。
- 期货amt原值为人民币元；兼容历史turnover字段时除以10000，显式保留两种单位。
- 期权IV转换为百分数，Delta/Gamma保留Wind供应商口径；缺失不填零。WSD Times验证历史日期，不把WSS查询时刻当作历史源日期。
- 期权为64个配置品种的core_v1样本，按当天期货持仓选活跃标的、配对行权价。Wind目录范围明确为选中标的，非全月份全链；不开放全链持仓/执行就绪标记。
- 夜盘为上一交易日晚开始的已完成时段，按自然日时间戳跨午夜聚合五分钟线，与前一交易日EOD比较，不混入当天日盘或正在进行的夜盘。
- **本仓库的新脚本尚未在Windows实机验收。** trading desk原版Windows验证不等于本仓库已经验收。
- **每日Wind调度尚未切换。** `.github/workflows/daily.yml` 仍调用旧iFinD路径；本次提交回补结果不会自动改变云端采集方式。
- **旧iFinD提数已设为idle（用户要求）。** 仓库变量`IFIND_COLLECTION_STATE=idle`；缺省也idle。Bridge不派发，daily手动触发只显示idle，不申请token或改数据。`active`会恢复旧iFinD整套采集，因此Windows Wind部署时不能用修改此变量代替实现Wind入口。独立`exchange_backup`手动模式保留。
- **远端入口进一步整体关闭（用户明确要求）。** GitHub工作流`chatgpt-trigger.yml`与`daily.yml`均为`disabled_manually`，不再接受自动/手动派发；备份代码也暂不执行。idle开关作为误启用后的二次防护保留。部署Windows时不要自行启用这两个旧工作流；后续调度方式需按用户的新决定配置。

## Windows首次安装（PowerShell，在本仓库根目录）

先 `git status`、`git pull --ff-only`，保留本地修改。安装/登录官方Wind终端及Python API插件，确认账户有相应历史期货、期权和分钟数据权限。不要从PyPI安装同名WindPy包。

```powershell
py -3.12 -c "import struct,sys; print(sys.version, struct.calcsize('P')*8)"
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
```

第一行应为Python3.12、64位。不要复用trading desk的`.venv`，也不要从Mac复制`.venv`、SDK动态库或登录配置。

trading desk记录的默认SDK目录如下；它是已有项目的配置默认值，不保证每台电脑都装在这里：

```powershell
$env:WIND_PYTHON_DIR = Join-Path $env:USERPROFILE 'Wind\Wind.NET.Client\WindNET\x64'
Test-Path (Join-Path $env:WIND_PYTHON_DIR 'WindPy.py')
.\.venv\Scripts\python.exe scripts\configure_wind.py --check
.\.venv\Scripts\python.exe scripts\configure_wind.py
```

如果Test-Path为False，找到本机官方安装的WindPy.py目录后修改上述变量，或传 `--sdk-dir '实际目录'`（兼容 `--wind-python-dir`）。不扫描/复制凭据。
配置程序只写 **本仓库** `.venv` 下的 `WindPy.pth`；`--check`只检查路径，不登录、不发请求。运行时也支持 `--sdk-dir`、`WIND_PYTHON_DIR`；早期`WIND_PYTHON_PATH`作为兼容别名。
Windows缺少DLL、位数不匹配或SDK权限问题应通过官方安装修复，不从Mac复制二进制。

## 登录与小样本验收

先只读检查trading desk当前状态，确认没有在两个项目间同时安排大批回补。当前没有跨项目统一请求队列；不要用重启/注销/取消订阅的方法争抢会话。
本仓库的canary没有 `w.stop()`，不要照搬trading desk的`wind_check.py`：其历史版本会在结尾调用stop。

```powershell
# 示例是已完成的历史交易日，代码为具体合约；可按最新目录换成有效合约。
.\.venv\Scripts\python.exe scripts\probe_wind.py --date 2026-11-11 --codes AU2612.SHF,SC2612.INE,M2701.DCE,SA701.CZC,SI2701.GFE
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

首次SDK调用可能出现官方登录窗口，由用户完成登录；不要要求把密码放进仓库。成功标准是5/5样本返回、源日期一致、字段有限，不只是import成功或应用窗口已打开。不要把模拟路径测试写成Windows实机验证。

## Windows Codex下一步要做什么

1. 阅读根目录AGENTS.md及本说明，查看远端最新数据日期，避免重拉已发布日期。本次 `tmp/` 原始缓存不在Git中，不要假设Mac缓存已同步。
2. 按上述配置通过Windows连接与小样本；记录SDK版本、Python位数、实际路径（不要记录凭据）、错误码、日期与数据范围。只修改本仓库。
3. 日常入口已另行实现为`run_wind_daily.py`：按目标日WSET目录校验上市/到期、唯一合约及配置覆盖，不依赖806合约。Windows需实测此入口，不修改固定日期历史探针来冒充每日逻辑。
4. 日盘WSD、core期权WSET+WSD、已完成夜盘WSI保持串行并断点缓存。生成到独立staging目录，三层日期/范围均通过后再发布。不能只把iFinD名称换成Wind而继续调用iFinD，也不能为凑齐数据伪造日期或零值。
5. 按现有发布器生成最新/历史/状态和统一report_input；保留Physical/External及尚未采集辅助层的真实来源与日期。补数脚本用法见 [WIND_CANARY.md](WIND_CANARY.md)，其中命令是本次稳定短窗口的记录，不可直接作为明天日常任务。
6. 按用户最新决定，只配置本机当前用户登录任务和Dot通知；远端Bridge/daily继续禁用，不配置远端runner或恢复cron。只启用一台日常采集电脑，避免重复运行与争抢账号。
7. 测试通过后提交代码与规范化数据，推送前重新同步远端，不强推。将Windows实测结果追加到本文件或新验证记录，明确仍未验证的登录后重启/锁屏/退出用户会话场景。

若需要trading desk上下文，查同名仓库的上述配置文件与跨平台文档；Codex原chat为 `01a1c51b-5a61-75e3-bebd-e33ec78c63cb`（Mac本地host，另一台电脑未必能读取）。必要信息已经写入本仓库，不依赖跨电脑chat可访问。
