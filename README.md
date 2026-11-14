# China Commodities Engine

面向研究报告的本地 Wind 数据仓库：覆盖中国商品期货/期权、股指衍生品、国债期货和回购资金面，保存原始输入、程序计算指标、数据日期、范围及缺失状态。

**请区分“代码已接入”和“数据已采集成功”。** 新模块的数据文件可能尚未生成，已有文件也可能来自旧schema。当前范围以本说明及配置为准，实际可用数据以同一Git提交下的manifest、源文件日期、schema、采样范围和质量字段为准。

## 给外部AI的读取入口

1. 固定一个Git提交，整个分析过程从同一提交读取，记录该提交SHA。
2. 先读 [`data/chat/manifest.json`](data/chat/manifest.json)，按其中真实路径读overview，再读report及模块索引/分片。`missing_sources`表示没有对应产物，不允许补造。
3. 报告输入：[ `data/report_input_latest.json` ](data/report_input_latest.json)。它是统一摘要与来源入口；各模块可能有不同日期，不等于全部市场都已更新到同一时点。
4. 商品读futures/market_state/night及options分片；股指读index_radar/index_history；国债读treasury_futures；回购读funding。需要逐合约或盘口细节时再下钻对应来源。
5. 分片每份不超过48 KiB，遵守索引顺序、JSON路径及Hash；完整还原必须读取全部分片。实际可用来源名称和文件路径以manifest为准，不猜路径。

先区分事实、程序计算、模型假设和缺失项，再写报告。`null`不是0；样本不代表全市场；当前查询时间不等于行情实际时间；`validation_passed`不自动证明无缺失、数据新鲜或可执行。
完整协议见 [Chat数据入口](docs/CHAT_DATA.md)。

## 仓库范围

| 模块 | 覆盖范围与默认取舍 | 原始输入与计算 | 数据位置 |
|---|---|---|---|
| 商品期货 | SHFE、INE、DCE、CZCE、GFEX中配置的商品，目标日全部有效具体合约 | Wind OHLC、结算、成交、持仓；程序计算期限结构、收益、波动率和状态 | `data/latest.json`、`snapshots/`、`market_state_latest.json` |
| 商品期权 | [产品配置](config/products.json)中的目标品种，默认`core_v1`活跃series核心样本 | Wind目录及报价；IV/Greeks/曲面能力按实际质量字段读取 | `data/options/` |
| 已完成商品夜盘 | 有夜盘安排的商品合约，实际会话完成后采集，无夜盘明确记录 | Wind分钟数据与源时间；夜盘交易日与自然日期分开 | `data/night_session/` |
| 股指期货 | IF、IH、IC、IM全部有效具体合约 | Wind报价；主力、期限结构、现金基差及同月期权联动 | `data/index_derivatives/` |
| 股指期权 | IO、HO、MO；默认`report_v1`，通常84个、最多126个合约；全链显式可选 | 程序按结算价、平价远期、Black-76计算IV/Delta/Gamma及PCR/RR/BF | 同上；摘要`radar_latest.json`、`radar_history.json` |
| 指数与ETF | 指数000016/000300/000905/000852；ETF510050/510300/510500/512100 | 指数/ETF行情和原始份额；份额单位未核实，人民币申赎估算禁用 | 股指EOD的`cash_market` |
| 股指期权盘口 | 仅所选样本，独立WSQ观测 | 买一/卖一、挂单量及供应商日期/时钟；时间语义未确认，不参与EOD定价 | `data/index_derivatives/books/latest.json` |
| 国债期货 | TS（2年）、TF（5年）、T（10年）、TL（30年），全部有效具体合约 | Wind报价；程序计算主力、同品种跨期价差和持仓变化 | `data/treasury_futures/` |
| 回购资金面 | 银行间DR/R及沪深交易所回购，各1/7/14天，共12条序列 | 利率水平、1/3/5/20日BP变化与明确口径的利差；不代表资金净流量 | `data/funding/` |
| Physical / External | 既有国内现货/基差代理及海外期货代理 | 独立AKShare模块，保留真实来源与日期，不随Wind入口刷新 | `data/physical/`、`data/external/` |
| 元数据与辅助层 | 合约参数、仓单、会员排名、核对等既有产物 | 独立读取其来源/日期/完整性，不因核心行情更新而宣称同步更新 | `data/contract_meta.json`、`data/crosschecks/`等 |

商品具体产品以 [config/products.json](config/products.json) 为准；Physical/External以 [config/data_foundation.json](config/data_foundation.json) 及实际快照为准。仓库不采个股，不联动股票或其他期权仓库。

## 期权如何控制采集耗时

商品维持`core_v1`。股指默认`report_v1`：

- 最近两个未到期月份，另补主力期指对应月，最多三个月。
- 每月最多七对执行价，优先ATM、25/10 Delta两翼和先前已采样的持仓集中位置。
- 用完整目录选择样本，只查询所选报价。先前Delta偏离目标过远时，用明确标注的价格/波动率假设选执行价，不将选样假设当实际IV。
- 通常84个合约，相对2026-11-13实测760合约全目录，盘中逐合约请求数约少89%；这不是实机耗时保证。日终按多合约、单指标批量请求。
- 完整链仅在本机忽略配置中显式设置`index_options_scope=full_chain_v2`时采集。

PCR包括认沽/认购持仓比和成交比；Gamma是程序重算，再结合持仓与合约乘数得到无方向的集中节点。默认均只描述样本，不能写成全市场PCR、全部Gamma峰值或做市商净持仓。
股指模型利率是明确的1.5%固定假设；IV不可解或输入不合格时留空。记录两翼实际Delta、执行价与目标距离。样本执行价集合变化时，历史PCR/成交/持仓合计变化留空，避免把采样变化当资金迁移。
详见 [股指模块](docs/INDEX_DERIVATIVES.md)、[商品核心期权](docs/options-core.md)。

## 国债与回购口径

国债净价按每百元面值报价，不是收益率。缺少CTD、转换因子和DV01输入时，不计算IRR、CTD基差或风险中性跨品种套保比率。
国债通常15:15收盘，最后交易日仅上午；交易所回购15:30截止，分别处理，不能沿用股指15:00收盘判断。

回购覆盖DR001/DR007/DR014、R001/R007/R014、沪市GC001/GC007/GC014、深市R-001/R-007/R-014。
银行间日成交加权利率与交易所收盘报价分开，不以FR/FDR定盘利率替代DR/R。数据数值原样保留，单位按配置的年化百分数声明并记录依据，具体Wind路由/单位待首次实机验收，不按数值大小猜换算。
组合回购EOD保守在18:00后尝试，仍要求目标日真实返回值。缺项记录partial和空值，不用旧值填当日；同一次启动不循环重试，下一次用户启动再尝试。
盘中仅查询六条交易所回购的延迟分钟报价，银行间日加权利率仍独立保留其EOD日期。

本轮没有央行公开市场投放/到期/净投放、Shibor、买断式回购或现金国债收益率曲线。回购利率与利差不能写成资金流入/流出金额。
来源规则、接口与限制见 [国债及回购模块](docs/RATES.md)。

## 启动、时间与发布

用户明确发送`data go`或要求启动后，单次前台执行：同步Git → 按启动时间选择数据 → 缺日补采/计算 → 校验 → 提交并推送`data/` → 退出。讨论状态或修改文档不启动采集。

- Windows：登录官方Wind后双击`START_WIND.cmd`；Mac：`START_WIND.command`。均调用同一个Python入口。
- 盘中截止为启动时间减5分钟并向下对齐5分钟边界；采集中跨过收盘也不改变已固定截点。保留每条实际行情时间及滞后。
- 股指09:30开盘、15:00收盘，国债15:15，交易所回购15:30；收盘后依模块自身边界与源日期处理EOD。回购组合EOD另有18:00保守尝试门槛。
- 日终补历史缺口；已完整发布的批次不重复采集。周六启动可补已结束周五夜盘，按下个交易日归档，实际自然起止日期另存。
- 国债、回购独立检查缺日，不因商品或股指已完整而跳过。
- 盘口WSQ观测与延迟分钟快照及结算价分析分开。盘中不拿旧OI/Greeks补当前空值；5分钟bar量不等于全天累计量。
- `data/intraday/`及各模块`intraday/`每交易日保留一份、另有latest，后一次合格快照可覆盖同日文件，不是完整分钟历史库。

没有定时任务、登录/开机启动、后台常驻、GPT Automation、Scheduler Bridge或远端工作流触发。旧iFinD远端入口保持禁用，`IFIND_COLLECTION_STATE`默认为idle；旧脚本仅作兼容，不属于当前Wind入口。
不修改Trading Desk进程、订阅或登录，不调用stop/cancel/logout，不新增runner/launcher锁或登录通知。

## 文件、历史和质量

| 路径 | 用途 |
|---|---|
| `data/chat/manifest.json`、`overview.json`及模块分片 | 外部AI的小文件入口，保留原件路径和Hash |
| `data/report_input_latest.json` | 商品、股指、国债、回购及独立辅助层的统一摘要 |
| `data/latest.json`、`data/snapshots/` | 商品日盘EOD，不混夜盘或国债 |
| `data/night_session/` | 夜盘独立快照、日期和历史；按price_semantics/ohlcv_scope解释 |
| `data/options/` | 商品期权样本、质量、曲面能力与历史 |
| `data/index_derivatives/` | 股指期货/期权、分析、现金市场、摘要、历史及独立盘口 |
| `data/treasury_futures/` | 国债EOD、逐日快照和独立分钟快照 |
| `data/funding/` | 回购EOD面板、缺项状态、利差及独立交易所分钟快照 |
| `tmp/wind-daily/raw/`、`normalized/` | 本机可恢复请求缓存，不提交Git，不作为外部AI正式入口 |

商品JSON快照/紧凑历史通常滚动保留20个交易日，期货/商品期权/夜盘长期Parquet通常252日；股指radar_history保留60个合格交易日。股指、国债和回购的dated snapshots不套用商品20日清理规则。
商品Market State的端点收益与股指/回购的严格连续日比较使用不同口径，按各字段窗口、日期和说明读取，不将不同模块的同名变化直接混用。

旧股指schema 1/core_v1仍可读；升级前原样保留到`index_derivatives/legacy_core/snapshots/`。新股指schema 2按`options_scope`区分报告样本与全链。
不同来源、旧版供应商数据与新Wind数据保留各自来源，不改旧日期，不把旧快照重新标成Wind。

## 验证状态与当前限制

- 既有Wind商品/夜盘与股指基础EOD有本机采集记录；实际日期和覆盖读已提交数据。
- 报告选样、结算价分析、历史比较与新入口经过定向测试，以及保留760合约实测数据的离线重放。新入口整轮Windows/Mac采集尚未验收。
- 国债/回购已接入代码和报告，尚未完成官方WindPy实机取数、账户权限、银行间路由/名称及单位验收。文件不存在或partial必须如实保留。
- ETF份额单位及盘口供应商时钟未闭环：不生成规范化ETF资金流，不把盘口观测称为15点收盘或可执行报价。
- Physical/External及其他辅助层不自动刷新；读取其真实日期。商品期权不是全月份全执行价链。
- 源日期、唯一合约、目录/样本覆盖、单位、缺失和OHLC检查必须保留；供应商错误不能制造日期、零值或虚假成功。

## 本地环境与维护

使用独立Python 3.12 x64虚拟环境及官方WindPy SDK；不从PyPI安装同名WindPy包，不提交凭据、SDK、`.venv`或原始缓存。
环境安装见 [Windows交接](docs/WINDOWS_WIND_HANDOFF.md)；运行规则见 [本地Wind入口](docs/LOCAL_WIND_DAILY.md) 和 [AGENTS.md](AGENTS.md)，最新用户约定优先于历史文档。

只查看计划，不连接Wind或同步Git：

```powershell
.\.venv\Scripts\python.exe scripts\run_wind_daily.py --plan
```

从已有数据重建报告并校验，不提取新市场数据：

```powershell
.\.venv\Scripts\python.exe -m china_commodities.cli report-input --repair-futures-history
.\.venv\Scripts\python.exe -m china_commodities.cli validate
```

代码修改做必要的定向测试，日常取数做数据校验，不要求每次运行全仓测试。Git推送前同步远端、保留他人提交，不强推；有未提交代码时先处理代码变更，再运行数据发布。
单次耗时记录在本机 `tmp/wind-daily/run-timings/`，包括各模块墙钟时间、SDK耗时/请求数、缓存命中及Git发布时间；代码准备不计入本次提数入口耗时。该记录不作为发布门槛。
独立交易所备份见 [EXCHANGE_BACKUP](docs/EXCHANGE_BACKUP.md)，不随Wind入口运行，也不自行启用远端旧工作流。
