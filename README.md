# China Commodities Engine

China Commodities Engine 是一个面向中国商品期货的每日数据 bot 项目骨架。它的职责是采集、标准化、校验并保存可审计的数据，供后续雷达、研究和交易分析使用；数据引擎本身不把单日涨跌直接解释为交易结论。

独立交易所日终备份试验见 [备份方案与实测结果](docs/EXCHANGE_BACKUP.md)。运行入口为 `python scripts/collect_exchange_backup.py --date YYYY-MM-DD`，结果单独保存到 `data/backup/`；GitHub Actions 可选择 `exchange_backup` 模式。当前实测覆盖 45/64 个目标期权品种，尚未达到 80% 门槛，生产链路继续使用 iFinD。

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

`market_state_latest.json` 的历史收益只复利同一个具体合约每天已发布的结算收益，不把换月前后的两个主力价格拼成连续涨跌。它同时给出 1/3/5/20 日收益、20日实现波动率、成交量与持仓量 z-score、持仓变化、`volume/OI`、价仓四象限线索、近次月价差 z-score，以及主力/曲线合约对换月标记。观察不足时字段保持 `null` 并披露实际样本数；价仓四象限只是归因线索，不是“新多”“新空”的事实。

当前状态向量仅覆盖 `Market` 层。基差、仓单、社会库存、产业利润和进口平价未验证前，`fundamental_score` 必须为空；期权曲面未达到门槛前，`convexity_score` 也必须为空。下游不能把单日横截面活跃度或 Market 状态向量直接当成交易建议。

## 数据源原则

核心期货的自动发布采用 **iFinD-primary** 原则：上期所、上期能源、大商所、郑商所和广期所的具体商品期货日终行情统一从 iFinD Quant API 提取，AKShare 不参与核心期货正式行情。商品期权是明确标记的例外：交易所 EOD 合约目录经 AKShare 适配，受阻时使用 OpenCTP 目录后备；逐合约报价、源日期、IV 和 vendor Greeks 仍由 iFinD 提供。任何来源切换都必须保留来源、时间和口径标记，不得静默混合。

本仓库不建设分钟、逐笔或逐笔盘口历史；夜盘层是**会话结束后取得的供应商最新报价快照**，不是已核验的交易所夜盘收盘价。`night_last_price` 为明确字段，`night_close` 保留为兼容别名；供应商时间戳须落在该会话窗口，但时间戳不自动等于最后成交时间。OHLC/成交量保留供应商值并标记 `vendor_realtime_unverified_session_scope`，不宣称纯夜盘口径。`trading_date` 按交易日历归属，`session_start_date`、`session_end_date` 是真实自然日期：例如2026-10-16周五晚至17日凌晨属于19日周一交易日。节前停夜盘不请求；当前维护2025—2026日历，超出范围显式失败。夜盘与 `data/latest.json` 的日终层分开保存，缺失或失败不覆盖最后有效快照。

每日新增独立官方期货核对：复用已取得的 iFinD 数据，仅下载五所公开日终文件，按同日期、同准确合约核对结算价、收盘价、前结算价。结果保存到 `data/crosschecks/futures_latest.json`，报告入口带紧凑状态。差异及单所失败只提示、不阻断发布、不静默切换主源；成交量、持仓、金额因口径差异暂不比较。原始官方响应保存在忽略目录 `tmp/exchange-crosscheck/raw/`。

Physical 与 External 使用 AKShare 的100ppi与Sina接口，不使用 iFinD token。核心期权有效记录可立即发布，ATM/偏度能力分别判断，不能将低门槛发布理解为完整曲面已就绪。

### iFinD 主源接入

正式工作流使用只读的 iFinD HTTP 适配器。系统根据静态品种—交易所目录生成宽范围具体合约候选，通过官方 `cmd_history_quotation` 批量查询，并删除没有任何日终行情字段的未上市或无效代码。这样可以同时保留具体合约和期限结构，又不需要由 AKShare 提供合约列表。refresh token 只从 Secret 读取；每次 Action 只换取一个 access token，并通过 Runner 的临时环境文件在期货和期权步骤间共享，任务结束后随 Runner 销毁。

本机安装官方 `iFinDPy` 后，可以用隐藏输入运行窄范围权限探针：

```powershell
python -X utf8 scripts\probe_ifind_commodities.py --provider http --date YYYY-MM-DD
python -X utf8 scripts\probe_ifind_full_universe.py --date YYYY-MM-DD
python -X utf8 scripts\probe_ifind_dce_universe.py --date YYYY-MM-DD --universe-date YYYY-MM-DD
python -X utf8 scripts\shadow_compare_ifind_futures.py
```

HTTP 探针从隐藏输入或当前进程的 `IFIND_REFRESH_TOKEN` 读取 refresh token；SDK 探针使用 `--provider sdk` 并读取 `IFIND_USERNAME`、`IFIND_PASSWORD`。不要把凭据写入仓库、命令行参数或日志；`.env` 和 `.env.*` 已被忽略。探针只输出登录状态、错误码、返回列、行数和源日期，不保存原始商业数据。

`src/china_commodities/collectors/ifind_http_adapter.py` 将 iFinD 返回值映射到现有期货字段，再经过具体合约、源日期、OHLC、成交量、持仓量和全交易所覆盖校验。iFinD 是当前核心期货行情的主源，但不被标记为交易所官方直连，因此 `core_futures_official_complete` 保持为 `false`，`module_quality.futures` 使用 `verified_vendor_primary`。

Physical 通过 AKShare 的 `futures_spot_price` 直接读取 100ppi 现货/基差日表，覆盖配置中的 20 个国内商品；它是公开现货代理，保留 `basis_quality=C`，不能替代已核验地区、等级、税口径的实货定价。External 通过 AKShare 的 `futures_foreign_hist` 直接读取 Sina 海外期货日线，已配置 WTI、Brent、LME、COMEX、SGX、CBOT、BMD Palm、原糖和棉花等 17 条公开合约。Dubai/Oman、两条新加坡燃料油、USD/CNH 与 DXY 没有精确且稳定的公开 AKShare 路由时，保持显式 `unavailable`，不以近似品种补值。生产任务不依赖自然语言搜索，也不申请 iFinD token；`config/data_foundation.json` 中的 iFinD ID 仅保留给旧快照/诊断兼容，已不作为生产 Physical 或 External 来源。合约参数与仓单由交易所接口经 AKShare 适配，DCE 合约信息优先走当前官方 portal，失败时再走兼容接口并保留明确错误。商品期权按下方独立流程采集，目录优先由交易所 EOD 经 AKShare 适配、受阻时使用 OpenCTP 后备，逐合约字段由 iFinD 提供。

### 商品期权全市场采集（目标范围）

商品期权目标目录为64个已上市品种，其中包含焦煤期权，不包含尚无正式挂牌合约的低硫燃料油期权。每日 GitHub Action 按品种逐一尝试：目录和数据错误隔离到单品种；iFinD 明确返回某交易所行情权限不足时只停止该交易所，继续采集其他交易所；认证、额度、传输及无法识别的 HTTP 错误仍按全局失败停止，避免重复消耗额度。每个品种的目录、源日期、合约数、报价覆盖和错误写入 `data/options/last_run_status.json`。这64个品种是目标范围，不代表已经完成64个品种的实采；实际覆盖必须以 Action 实跑后的状态文件为准。

具体期权合约目录优先来自交易所 EOD，经 AKShare 适配；若交易所网页在 GitHub Runner 被阻断，则整批只下载一次 OpenCTP 当前有效合约字典，并仅把它用于合约发现和到期日元数据。逐合约的收盘/结算、成交量、持仓量、标的结算价、源日期、IV 和 vendor Greeks 仍来自 iFinD，缺一合约即阻断该品种。目录来源和行情来源必须分别标记，不能把 OpenCTP 或 AKShare 的目录写成 iFinD 行情。

默认 `--minimum-product-coverage 0`：非空合格核心样本即可更新 `data/options/latest.json`，源日期错配和结构无效的记录单独记原因。IV、OI、到期日等分析字段缺失不阻断发布。核心样本标记为 `core_available`，不宣称完整链；ATM 和合格偏度按 series 独立判断，单月平值样本不支持全链持仓或期限结构结论。只有显式设置非零覆盖门槛才可能将有效结果留在 attempt 层。

GitHub Action 从仓库 Secret `IFIND_REFRESH_TOKEN` 注入凭据。换取的 access token 会先加入 GitHub 日志脱敏规则，再写入当次 Runner 的临时 `GITHUB_ENV`，供期货和期权共用；token 和原始 iFinD 响应都不会写入仓库。公开仓库发布前必须确认 iFinD 商业数据的再分发许可，未确认时不得把原始商业数据当作可公开分发资产。

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
python -m china_commodities.cli run --provider ifind --skip-options
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

GitHub Actions 当前为 **dispatch-only**，没有 GitHub cron；外部自动化通过桥接工作流触发，06:03/18:03是约定的北京时间窗口，不是本仓库保证的触发时间。晨间采集完成的夜盘，并按国内交易日历复核最近 EOD；普通周六晨间夜盘归属下周一。国内休市日不套用到海外市场，External 的工作日请求提示与源日期验证独立保留。当日 `full` 工作流在18:00—18:15到达等待步骤时，先有界等待至18:15，再规划当日采集，避免18:03误采前一日；历史请求、夜盘专用和其他时段不等待。外部调度本身未被此代码修改。

夜盘及核心期权均按有效记录可用即发布，缺字段或局部失败保留说明，不以整体覆盖率扣住有效数据；日期、来源、合约身份仍须正确。各模块日期和能力状态独立，不以单所权限失败概括全市场。

每次生产 Action 的 `full` 工作流模式仍检查期货、期权、Physical 和 External 四个模块。期货、Physical 和 External 保留原强制刷新行为；期权使用 `--option-profile core`，同日复用成功记录，只补缺失核心合约，显式 `--force-refresh` 才重新请求。工作流模式 `full` 不表示期权全链；全链需显式 `--option-profile full`。`night_session_only` 仍是只采夜盘的独立探针模式。非晚间等待窗口的18:15前当日请求，按最近已完成国内交易日恢复；尚未完成的当天 EOD 不冒充可用。

Physical 每次以一个 AKShare 批量请求获取 100ppi 现货/基差表；External 对每条已配置海外合约读取公开日线。`last_run_status.json` 的每个 series 状态会记录 `request_made`、`query_start_date`、`query_end_date`、源日期和陈旧状态，便于审计实际请求范围。生产工作流以 `--shadow-days 1` 在通过校验后立即提升 AKShare 快照；无数据或无法精确映射的目标会保留显式状态。

历史回填优先使用 iFinD 区间查询；若账户对多日合约区间返回参数规模错误，则自动改用共享 access token 的逐日查询。系统取五个交易所共同存在或逐日验证通过的最近20个交易日，在内存中执行与每日任务相同的校验。只有选中的全部日期均通过时才发布，节假日和空返回不会伪装成交易日。历史回填建议在本地一次性执行；GitHub Action 只运行正常的单日更新。

仅采集上期所、上期能源、郑商所和广期所：

```powershell
python -m china_commodities.cli run --provider akshare --date 2026-08-14 --exclude-exchange DCE
python -m china_commodities.cli validate --scope ex-dce
```

当前正式产物发布五所具体期货合约的日终价量仓、期限结构和由此计算的异常候选；未映射的辅助模块保持空值与显式状态。只有 `data/` 下通过相应校验且确有变化的文件才会提交和推送；任一核心市场失败时保留上一份已验证快照，并把本次失败写入 `data/last_run_status.json`。
