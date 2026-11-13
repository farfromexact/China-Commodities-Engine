# China Commodities Engine

> 2026-11-12：已增加并完成本机 Mac 小样本验证的 [Trading Desk 共存采集模式](docs/WIND_DASHBOARD_COEXISTENCE.md)，支持看板运行时股票/商品串行限速取数。

China Commodities Engine 是一个面向中国商品期货的每日数据 bot 项目骨架。它的职责是采集、标准化、校验并保存可审计的数据，供后续雷达、研究和交易分析使用；数据引擎本身不把单日涨跌直接解释为交易结论。

**Wind迁移 / Windows接续：**先读 [Windows Wind配置与交接](docs/WINDOWS_WIND_HANDOFF.md) 和 [AGENTS.md](AGENTS.md)。近期数据已通过本机官方 WindPy 补齐；远端日常取数入口当前关闭，Wind 远端调度尚未部署。

本地每日与开机补采入口：`python scripts/run_wind_daily.py --plan`。完整配置、Windows登录启动和Dot通知协议见 [本地Wind每日采集](docs/LOCAL_WIND_DAILY.md)。远端Bridge与daily继续禁用；本地默认不启用，配置后由本机采集、校验并自动推送。

独立交易所日终备份试验见 [备份方案与实测结果](docs/EXCHANGE_BACKUP.md)。手动运行入口为 `python scripts/collect_exchange_backup.py --date YYYY-MM-DD`，结果单独保存到 `data/backup/`；覆盖仍未达到 80% 门槛，远端 Actions 当前禁用该模式。核心数据后续使用本机官方 WindPy，Windows 配置和验收状态见 [Wind 交接说明](docs/WINDOWS_WIND_HANDOFF.md)。

## 数据层级

数据设计按以下层级组织：

- 合约层：具体合约的交易日、开高低收、结算价、成交量、持仓量和成交额；连续主力仅用于研究，交易应落到具体合约。
- 期限结构层：主力与次主力、跨期价差、曲线变化和换月标记。
- 交割与现货层：仓单、库存、现货价格和基差，并保留地区、品质、单位、税口径和价格时点。
- 期权层：按目标目录逐品种采集核心样本，默认每品种一个活跃 series，最多15对平值附近 C/P 合约，预算约为目录合约数的10%，每合约10个字段。目录优先来自交易所 EOD，经 AKShare 适配；网页受阻时可使用 OpenCTP 当前字典。只要取得日期及结构合格的非空记录即可发布，缺少其他品种或分析指标不会阻止发布。`core_available` 只代表核心数据可读，完整曲面、持仓结构与执行能力另行判断。详见 [核心期权采集与发布](docs/options-core.md)。
- 数据质量层：来源、抓取时间、交易日、状态、新鲜度、回退标记和错误信息。

数据新鲜度按模块独立判断。失败或陈旧数据必须显式标记，不能伪造交易日，也不能让未经验证的陈旧数据进入当日异常扫描；快照在校验通过后才进入历史产物。

其中 `verified` 表示交易日、具体合约、完整交易所覆盖和核心结构校验通过；`scope_verified` 只表示所选交易所的核心期货数据通过。`core_futures_official_complete` 明确限定为核心期货行情均来自交易所日终接口；`scope_official_complete` 只有合约参数、仓单、会员排名和期权链等官方模块也达到完整标准时才为真。下游应读取 `module_quality`，不得用单一布尔值替代具体模块质量判断。

每个采集模块保留请求日期、源日期、抓取时间、接口名称、原始载荷 SHA-256 和模式签名。核心期货只有在源日期与请求日期一致时才可标记 `is_fresh=true`。`quality_metrics` 还披露日期匹配率、未知品种、重复合约、OHLC异常、负成交/持仓、排名对账和范围覆盖惩罚。

辅助模块在当前运行失败或同一交易日重复抓取明显退化时，不会用较差结果覆盖上一份有效记录。系统会保留上一份记录并显式增加 `carried_forward`、`carried_from_trade_date`、`current_collection_state`、`carry_forward_reason` 和 `is_stale`；跨交易日携带的数据不会计入候选证据数量。正式JSON仅因抓取时间变化时也不会被重写，避免Action制造无意义提交。

若大商所官方接口被拦截，系统只会在新浪返回同一交易日的全部可用具体合约时启用 AKShare 实时行情降级，并明确保留 `is_fallback=true`；该降级不会伪造官方结算价、成交额，也不会用收盘价构造官方结算期限结构。

`validate` 会校验已提升的 `latest.json`；首次运行若因单一交易所失败而没有可提升快照，只要 `last_run_status.json` 已完整记录非新鲜状态和阻断原因，结构校验仍通过，便于 Actions 提交可观察的失败状态。它不会把部分运行改写成成功数据。

也可以显式排除故障交易所进行范围化采集。例如排除大商所后，产物写入 `data/scoped/ex-dce/`，不会覆盖全市场文件。范围化快照使用 `scope_verified=true` 表示所选交易所通过验证，同时保留 `verified=false` 和 `data_fresh=false`，避免被误读为五所完整数据。

热力图中的 `cross_sectional_activity_score` 是单日横截面活跃度分数，不是历史异常分数。`score_rank` 按该分数排序，`display_order` 是兼顾产业板块覆盖后的展示顺序。curve、basis、warehouse、options分别记录在 `evidence` 中，不再合并成一个模糊证据层。会员排名只汇总已对账的1—20名，999总计行会被移除；即使对账通过，也只代表交易所公布的Top-N分布，不代表“新多”或“新空”。

## 主要产物

约定的正式 JSON 产物包括：

- `data/latest.json`：各交易所和具体合约的最新标准化**日盘 EOD** 数据；不混入夜盘字段。
- `data/radar_latest.json`、`data/radar_history.json` 与 `data/market_state_latest.json`：只保存日盘衍生指标和历史，绝不把夜盘当作 EOD 收益或换月数据。
- `data/night_session/latest.json`：最近一次已验证的中国夜盘收盘快照，是夜盘的唯一完整事实源。
- `data/night_session/YYYY-MM-DD.json`：按**当前交易日**归档的夜盘快照；例如 9 月 2 日晚至 9 月 3 日凌晨的夜盘保存为 `2026-09-03.json`。
- `data/night_session/last_run_status.json`：夜盘模块独立的新鲜度、覆盖率、校验和错误状态。
- `data/night_session/history.parquet`：按当前交易日、交易所和具体合约去重的夜盘长期历史。
- `data/report_input_latest.json`：下游晨报使用的只读汇总层；其中的 `night_session` 只提供按品种压缩的摘要引用和代表合约，完整合约明细仍只在 `data/night_session/latest.json`。

报告输入 schema v3 增加 `execution_prices` 与执行校验边界：产业、现货、库存和基差仍完整用于研究；明确合约的执行计划必须用快照溯源报价，并经过 `china_commodities.execution.gate_card`。可选输入 `data/trade_cards/candidates.json` 的失败卡保留研究、清空执行对象；外部生成器应在发布前调用 `scripts/validate_trade_cards.py`，不能把未校验的自由文本当作执行建议。EOD 校验通过也只代表指定交易日的参考计划，下单前仍需刷新报价。完整结构、调查证据及接入方法见 [SA/JM 价格调查与执行边界](docs/sa-jm-price-rca-20261026.md)。
- `data/contract_meta.json`：合约乘数、最小变动价位、交易与到期属性等日盘元数据；不承载夜盘快照。
- `data/history/futures.parquet`：按交易日、交易所和具体合约去重的长期日线历史；不受20日 JSON 窗口限制，可回填252个交易日。
- `data/physical/latest.json`、`attempt_latest.json` 和 `history.parquet`：20个核心品种的固定指标矩阵、产业序列、官方仓单引用、`Spot - Futures` 基差和显式空值结论。
- `data/external/latest.json`、`attempt_latest.json` 和 `history.parquet`：固定海外标的日频序列；连续或远月上下文序列不得直接进入进口平价。
- `data/last_run_status.json`：日盘期货及独立日频模块的状态、错误和新鲜度；夜盘状态在 `data/night_session/last_run_status.json`。
- `data/snapshots/YYYY-MM-DD.json`：通过校验的日级快照。
- `data/options/latest.json`：最近一次取得有效数据的商品期权小型索引。默认 `core_v1` 只采集每品种一个活跃 series 的核心行权价；有合格记录即发布，不要求最低品种覆盖率。`collection_scope`、`collection_plans` 和质量字段明确记录采集范围，不代表完整链。
- `data/options/latest_shards/YYYY-MM-DD/EXCHANGE/PRODUCT.json.gz`：索引引用的当日逐品种压缩链；目录只保留 `latest` 对应的一个交易日，便于选择性读取并避免每日重写50MB级 JSON。
- `data/options/attempt_latest.json.gz`：显式设置非零覆盖门槛时可能产生的部分链尝试；也可能是旧运行遗留文件。必须读取自身日期、`coverage`、`attempt_only` 和 `promotion_eligible`，不得当作最新正式数据。
- `data/options/quality_latest.json`：期权链、覆盖范围、曲面、模型 Greeks 和执行价格的独立就绪状态。
- `data/options/surface_latest.json`：按交易所、品种、标的和到期日分组的 EOD 摘要。核心采样模式随有效记录同步发布；即使 IV、到期日或偏度不可用也保留摘要并标空。`core_atm_ready` 与完整 `surface_ready` 分开，采样不证明完整曲面或持仓结构。
- `data/options/surface_attempt_latest.json.gz`、`surface_last_run_status.json` 和 `surface_shadow_state.json`：当次摘要与质量状态。核心模式发布不受曲面影子门槛约束；显式 `--option-profile full` 仍保留原曲面质量提升机制。
- `data/options/history/year=YYYY/month=MM/YYYY-MM-DD.parquet`：已提升 EOD 期权链的逐日 Parquet 分区；同日重跑按交易所和合约去重，滚动保留最近252个交易日。
- `data/options/last_run_status.json`：每个目标品种的尝试结果、错误、源日期、成功覆盖率和是否允许提升 `latest`。
- `data/options/snapshots/YYYY-MM-DD.json.gz`：达到发布门槛后保存的压缩逐合约期权快照。
- `data/options/history.json`：按交易日汇总的期权历史记录。

期权采集使用独立目录，但每日最后的 `report-input` 步骤会把同交易日、已发布的期权链状态回写到根级 `data/last_run_status.json`、`data/latest.json` 和 `data/radar_latest.json`。这只同步链条采集状态；`surface_ready`、`positioning_ready` 和 `execution_ready` 仍然分别由期权质量文件决定。

正式 JSON 历史统一滚动保留最近20个交易日：`data/snapshots/` 保存完整日盘快照，`data/night_session/YYYY-MM-DD.json` 保存完整夜盘快照，两者按各自的当前交易日去重。`latest.json` 始终仅指向最近一个已验证的日盘 EOD；夜盘只能从 `data/night_session/latest.json` 读取。夜盘 Parquet 和日盘期货 Parquet 均保留最近252个交易日。晨间缓存命中时会补齐夜盘的按日归档和清理旧版顶层复制，但不会发起额外供应商请求或制造无意义的 Git 提交。商品期权的压缩快照和紧凑摘要同样只滚动保留最近20个成功发布交易日，`latest_shards` 只保留当前一天；默认任何非空合格核心样本都可发布，空结果保留上一份。商品期权 Parquet 使用逐日分区并滚动保留最近252个交易日，保留采样范围标记。

`market_state_latest.json` 的 1D 沿用日结算收益。3D、5D、20D 按国内交易日历定位 T−N，固定 T 日的交易所和具体主力合约，用两端结算价计算 `(settle[T] / settle[T−N] − 1) × 100%`。详细指标列出两端日期、结算价及有效端点数；起点日的 JSON 未保留时，可使用长期 Parquet 中同日且来源日期核实的记录。中间缺日不影响端点收益，任一端缺有效结算价则为 `null`。报告输入的 `quality.market_state_history_window` 列出各窗口起点与最近 20 个交易日缺少的快照日期。

同一产物还给出实现波动率、成交量与持仓量 z-score、持仓变化、`volume/OI`、价仓四象限线索、近次月价差 z-score，以及主力/曲线合约对换月标记。价仓四象限仅用于归因研究。

当前状态向量仅覆盖 `Market` 层。基差、仓单、社会库存、产业利润和进口平价未验证前，`fundamental_score` 必须为空；期权曲面未达到门槛前，`convexity_score` 也必须为空。下游不能把单日横截面活跃度或 Market 状态向量直接当成交易建议。

## 数据源原则

核心期货、核心期权和夜盘的后续主采集路径为本机官方 WindPy，由 `scripts/run_wind_daily.py` 按交易日串行采集并校验。2026-11-11 最新已发布的三层快照均标记为 Wind 来源。当前本机配置默认关闭，Windows 登录任务尚未安装；远端 `daily.yml` 与 `chatgpt-trigger.yml` 也已禁用，因此这些快照不表示无人值守调度已经上线。Physical 与 External 仍是独立 AKShare 模块。历史 iFinD 适配器仅保留兼容和诊断用途；所有来源仍须记录来源、时间和口径，不得静默混合。

本仓库不建设分钟、逐笔或逐笔盘口历史；夜盘层是**会话结束后取得的供应商最新报价快照**，不是已核验的交易所夜盘收盘价。`night_last_price` 为明确字段，`night_close` 保留为兼容别名；供应商时间戳须落在该会话窗口，但时间戳不自动等于最后成交时间。OHLC/成交量保留供应商值并标记 `vendor_realtime_unverified_session_scope`，不宣称纯夜盘口径。`trading_date` 按交易日历归属，`session_start_date`、`session_end_date` 是真实自然日期：例如2026-10-16周五晚至17日凌晨属于19日周一交易日。节前停夜盘不请求；当前维护2025—2026日历，超出范围显式失败。夜盘与 `data/latest.json` 的日终层分开保存，缺失或失败不覆盖最后有效快照。

每日新增独立官方期货核对：复用已取得的 iFinD 数据，仅下载五所公开日终文件，按同日期、同准确合约核对结算价、收盘价、前结算价。结果保存到 `data/crosschecks/futures_latest.json`，报告入口带紧凑状态。差异及单所失败只提示、不阻断发布、不静默切换主源；成交量、持仓、金额因口径差异暂不比较。原始官方响应保存在忽略目录 `tmp/exchange-crosscheck/raw/`。

Physical 与 External 使用 AKShare 的100ppi与Sina接口，不使用 iFinD token。核心期权有效记录可立即发布，ATM/偏度能力分别判断，不能将低门槛发布理解为完整曲面已就绪。

### 遗留 iFinD 采集适配器

以下说明的是旧 iFinD 采集路径及其诊断工具。它不属于新的本机 Wind 日常入口；远端旧工作流保持禁用，`IFIND_COLLECTION_STATE` 缺省为 `idle`。系统根据静态品种—交易所目录生成宽范围具体合约候选，通过官方 `cmd_history_quotation` 批量查询，并删除没有任何日终行情字段的未上市或无效代码。这样可以同时保留具体合约和期限结构，又不需要由 AKShare 提供合约列表。历史 Action 使用的 refresh token 只从 Secret 读取；每次 Action 只换取一个 access token，并通过 Runner 的临时环境文件在期货和期权步骤间共享，任务结束后随 Runner 销毁。

本机安装官方 `iFinDPy` 后，可以用隐藏输入运行窄范围权限探针：

```powershell
python -X utf8 scripts\probe_ifind_commodities.py --provider http --date YYYY-MM-DD
python -X utf8 scripts\probe_ifind_full_universe.py --date YYYY-MM-DD
python -X utf8 scripts\probe_ifind_dce_universe.py --date YYYY-MM-DD --universe-date YYYY-MM-DD
python -X utf8 scripts\shadow_compare_ifind_futures.py
```

HTTP 探针从隐藏输入或当前进程的 `IFIND_REFRESH_TOKEN` 读取 refresh token；SDK 探针使用 `--provider sdk` 并读取 `IFIND_USERNAME`、`IFIND_PASSWORD`。不要把凭据写入仓库、命令行参数或日志；`.env` 和 `.env.*` 已被忽略。探针只输出登录状态、错误码、返回列、行数和源日期，不保存原始商业数据。

`src/china_commodities/collectors/ifind_http_adapter.py` 保留旧的 iFinD 字段映射与校验逻辑，供兼容调用和诊断使用。Wind 采集使用独立适配器，不把 Wind 结果伪标成 iFinD；`core_futures_official_complete` 仍只代表交易所 EOD 官方接口完整。

Physical 通过 AKShare 的 `futures_spot_price` 直接读取 100ppi 现货/基差日表，覆盖配置中的 20 个国内商品；它是公开现货代理，保留 `basis_quality=C`，不能替代已核验地区、等级、税口径的实货定价。External 通过 AKShare 的 `futures_foreign_hist` 直接读取 Sina 海外期货日线，已配置 WTI、Brent、LME、COMEX、SGX、CBOT、BMD Palm、原糖和棉花等 17 条公开合约。Dubai/Oman、两条新加坡燃料油、USD/CNH 与 DXY 没有精确且稳定的公开 AKShare 路由时，保持显式 `unavailable`，不以近似品种补值。生产任务不依赖自然语言搜索或 iFinD token；`config/data_foundation.json` 中的 iFinD ID 仅保留给旧快照/诊断兼容。合约参数与仓单由交易所接口经 AKShare 适配，DCE 合约信息优先走当前官方 portal，失败时再走兼容接口并保留明确错误。商品期权的 Wind 目录和核心报价由本机 Wind API 入口采集，范围标记为 `core_v1`。

### 商品期权全市场采集（目标范围）

商品期权目标目录为64个已上市品种，其中包含焦煤期权，不包含尚无正式挂牌合约的低硫燃料油期权。旧 iFinD 工作流曾按品种隔离目录和报价错误；该工作流目前禁用。Wind 本机入口按 `core_v1` 为配置品种采集核心样本，不宣称完整月份和行权价链；实际覆盖以各交易日发布状态为准。

具体期权合约目录优先来自交易所 EOD，经 AKShare 适配；若交易所网页在 GitHub Runner 被阻断，则整批只下载一次 OpenCTP 当前有效合约字典，并仅把它用于合约发现和到期日元数据。逐合约的收盘/结算、成交量、持仓量、标的结算价、源日期、IV 和 vendor Greeks 仍来自 iFinD，缺一合约即阻断该品种。目录来源和行情来源必须分别标记，不能把 OpenCTP 或 AKShare 的目录写成 iFinD 行情。

默认 `--minimum-product-coverage 0`：非空合格核心样本即可更新 `data/options/latest.json`，源日期错配和结构无效的记录单独记原因。IV、OI、到期日等分析字段缺失不阻断发布。核心样本标记为 `core_available`，不宣称完整链；ATM 和合格偏度按 series 独立判断，单月平值样本不支持全链持仓或期限结构结论。只有显式设置非零覆盖门槛才可能将有效结果留在 attempt 层。

旧 GitHub Action 曾从仓库 Secret `IFIND_REFRESH_TOKEN` 注入凭据；当前禁用的 Wind 本机入口不读取该 token。供应商商业数据的保存和再分发仍须遵守对应许可，本仓库不提交本机 SDK、登录凭据或原始请求缓存。

## 本地运行

项目要求 Python 3.12。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
```

运行单元测试、每日采集和数据校验：

```powershell
python -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\run_wind_daily.py --plan
python -m china_commodities.cli foundation --provider akshare --audit-only
python -m china_commodities.cli foundation --provider akshare --scope physical --date YYYY-MM-DD
python -m china_commodities.cli foundation --provider akshare --scope external --date YYYY-MM-DD
python -m china_commodities.cli backfill --end-date YYYY-MM-DD --days 252 --history-limit 20 --snapshot-limit 20
python -m china_commodities.cli history-rebuild --retention-days 252
python -m china_commodities.cli report-input --repair-futures-history
python -m china_commodities.cli validate
```

商品期权全品种先做无写入探针：

```powershell
python scripts/collect_ifind_options.py --all-products --date YYYY-MM-DD --dry-run
```

正式执行全品种核心采样，默认成功品种覆盖率门槛为0，有合格记录即可发布：

```powershell
python scripts/collect_ifind_options.py --all-products --date YYYY-MM-DD
```

GitHub 上的 `chatgpt-trigger.yml` 和 `daily.yml` 当前为 `disabled_manually`，没有远端每日触发。旧桥接和 iFinD 工作流保留在代码中，不能视作当前运行入口。Wind 日常采集由 Windows 本机 `run_wind_daily.py` 管理；启用配置、登录通知及当前用户登录任务见 [本地 Wind 每日采集](docs/LOCAL_WIND_DAILY.md)。

夜盘及核心期权均按有效记录可用即发布，缺字段或局部失败保留说明，不以整体覆盖率扣住有效数据；日期、来源、合约身份仍须正确。各模块日期和能力状态独立，不以单所权限失败概括全市场。

旧 Action 的 `full` 模式曾检查期货、期权、Physical 和 External 四个模块；当前远端工作流已禁用。Wind 本机入口使用 `core_v1`，并在期货、期权及对应夜盘校验完成后统一发布。已取消18:15供应商提数时间门槛；收盘前当天请求仅保留本机 provisional 候选，真实日盘收盘后重新采集并通过三层校验才发布 EOD。详见本地采集文档。

Physical 每次以一个 AKShare 批量请求获取 100ppi 现货/基差表；External 对每条已配置海外合约读取公开日线。`last_run_status.json` 的每个 series 状态会记录 `request_made`、`query_start_date`、`query_end_date`、源日期和陈旧状态，便于审计实际请求范围。生产工作流以 `--shadow-days 1` 在通过校验后立即提升 AKShare 快照；无数据或无法精确映射的目标会保留显式状态。

旧 iFinD 回填工具仍保留供兼容维护使用。Wind 本机日常入口按缺失交易日逐日刷新合约目录，使用串行请求和可恢复缓存；配置与发布规则见 [本地 Wind 每日采集](docs/LOCAL_WIND_DAILY.md)。

仅采集上期所、上期能源、郑商所和广期所：

```powershell
python -m china_commodities.cli run --provider akshare --date 2026-08-14 --exclude-exchange DCE
python -m china_commodities.cli validate --scope ex-dce
```

当前正式产物发布五所具体期货合约的日终价量仓、期限结构和由此计算的异常候选；未映射的辅助模块保持空值与显式状态。只有 `data/` 下通过相应校验且确有变化的文件才会提交和推送；任一核心市场失败时保留上一份已验证快照，并把本次失败写入 `data/last_run_status.json`。
