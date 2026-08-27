# China Commodities Engine

China Commodities Engine 是一个面向中国商品期货的每日数据 bot 项目骨架。它的职责是采集、标准化、校验并保存可审计的数据，供后续雷达、研究和交易分析使用；数据引擎本身不把单日涨跌直接解释为交易结论。

## 数据层级

数据设计按以下层级组织：

- 合约层：具体合约的交易日、开高低收、结算价、成交量、持仓量和成交额；连续主力仅用于研究，交易应落到具体合约。
- 期限结构层：主力与次主力、跨期价差、曲线变化和换月标记。
- 交割与现货层：仓单、库存、现货价格和基差，并保留地区、品质、单位、税口径和价格时点。
- 期权层：按目标目录逐品种尝试采集商品期权。目标目录为64个已上市品种；优先使用交易所 EOD 合约目录，经 AKShare 适配，网页受阻时才使用一次性 OpenCTP 当前合约字典作为显式后备；逐合约报价、源日期、IV 和 vendor Greeks 均由 iFinD 验证。单品种失败隔离并写入 `data/options/last_run_status.json`；成功品种覆盖率达到默认75%才允许更新 `data/options/latest.json`，低于门槛则保留上一份。部分覆盖只能标记为 `partial_chain`，不能声称全市场完整；到期日与标的键完整、源日期100%匹配且 IV 覆盖率至少80%时可以生成 EOD 曲面，OI 覆盖率至少90%才标记 `positioning_ready`，bid-ask 只阻断 `execution_ready`，不阻断曲面。
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

- `data/latest.json`：各交易所和具体合约的最新标准化日盘 EOD 数据；已验证夜盘以独立 `night_session` 覆盖层附入，不改写日盘字段。
- `data/radar_latest.json`：期限结构、基差、仓单和异常候选的当前摘要；同时附有可供雷达读取的独立 `night_session` 覆盖层。
- `data/radar_history.json`：面向历史比较的紧凑日级记录；日盘记录保留在 `records`，夜盘快照保留在独立、按当前交易日去重且滚动20日的 `night_session_records`。
- `data/market_state_latest.json`：基于最近20个交易日、按当前具体合约回溯的收益、波动、量仓冲击、换月和曲线状态；不拼接不同主力合约。夜盘数据只作为 `night_session` 上下文，不进入 EOD 历史收益计算。
- `data/report_input_latest.json`：下游晨报使用的只读汇总层；合并 Market、Physical、External、期权曲面、合约元数据和各模块独立时间戳，不发起新的供应商请求。
- `data/contract_meta.json`：合约乘数、最小变动价位、交易与到期属性等元数据，并附有不混入合约规则的 `night_session_snapshot` 来源摘要。
- `data/history/futures.parquet`：按交易日、交易所和具体合约去重的长期日线历史；不受20日 JSON 窗口限制，可回填252个交易日。
- `data/physical/latest.json`、`attempt_latest.json` 和 `history.parquet`：20个核心品种的固定指标矩阵、产业序列、官方仓单引用、`Spot - Futures` 基差和显式空值结论。
- `data/external/latest.json`、`attempt_latest.json` 和 `history.parquet`：固定海外标的日频序列；连续或远月上下文序列不得直接进入进口平价。
- `data/last_run_status.json`：各数据模块的状态、错误和新鲜度，并附有最新已验证夜盘的摘要状态。
- `data/snapshots/YYYY-MM-DD.json`：通过校验的日级快照。
- `data/options/latest.json`：最近一次达到发布门槛的商品期权小型索引；只保留一个交易日的元数据、质量字段、总记录数、整链压缩快照入口和品种分片清单，不再内嵌逐合约记录。
- `data/options/latest_shards/YYYY-MM-DD/EXCHANGE/PRODUCT.json.gz`：索引引用的当日逐品种压缩链；目录只保留 `latest` 对应的一个交易日，便于选择性读取并避免每日重写50MB级 JSON。
- `data/options/attempt_latest.json.gz`：最近一次通过逐合约校验、但可能未达到75%提升门槛的压缩部分链；必须同时读取其中的 `coverage`、`attempt_only` 和 `promotion_eligible`，不得当作全市场 `latest`。
- `data/options/quality_latest.json`：期权链、覆盖范围、曲面、模型 Greeks 和执行价格的独立就绪状态。
- `data/options/surface_latest.json`：严格按交易所、品种、标的期货合约和到期日分组的已提升 EOD 曲面。
- `data/options/surface_attempt_latest.json.gz`、`surface_last_run_status.json` 和 `surface_shadow_state.json`：当次曲面、质量门槛和可配置的影子运行状态；失败不会覆盖上一份有效曲面。生产工作流在完成历史初始化后使用 `--surface-shadow-days 1`，首个通过全部曲面质量门槛的EOD日期即可提升。
- `data/options/history/year=YYYY/month=MM/YYYY-MM-DD.parquet`：已提升 EOD 期权链的逐日 Parquet 分区；同日重跑按交易所和合约去重，滚动保留最近252个交易日。
- `data/options/last_run_status.json`：每个目标品种的尝试结果、错误、源日期、成功覆盖率和是否允许提升 `latest`。
- `data/options/snapshots/YYYY-MM-DD.json.gz`：达到发布门槛后保存的压缩逐合约期权快照。
- `data/options/history.json`：按交易日汇总的期权历史记录。

正式 JSON 历史统一滚动保留最近20个交易日：`radar_history.json` 的日盘 `records` 与夜盘 `night_session_records` 分别按交易日去重保存，`data/snapshots/` 保存同一窗口内包含具体合约的完整日盘快照。`latest.json` 始终指向最近一个已验证交易日；其 `night_session` 仅为最近一次通过源时间戳验证的夜盘覆盖层。晨间缓存命中时仍会同步这六个顶层读取产物，但若内容完全相同则不会制造无意义的 Git 提交。商品期权的整链压缩快照和紧凑摘要同样只滚动保留最近20个成功发布交易日，`latest_shards` 只保留当前一天；低于75%覆盖率的尝试不会覆盖上一份 `latest`，也不会缩短有效历史窗口。商品期权 Parquet 使用逐日分区并滚动保留最近252个交易日，旧分区删除前不会用新数据覆盖其他日期。

`market_state_latest.json` 的历史收益只复利同一个具体合约每天已发布的结算收益，不把换月前后的两个主力价格拼成连续涨跌。它同时给出 1/3/5/20 日收益、20日实现波动率、成交量与持仓量 z-score、持仓变化、`volume/OI`、价仓四象限线索、近次月价差 z-score，以及主力/曲线合约对换月标记。观察不足时字段保持 `null` 并披露实际样本数；价仓四象限只是归因线索，不是“新多”“新空”的事实。

当前状态向量仅覆盖 `Market` 层。基差、仓单、社会库存、产业利润和进口平价未验证前，`fundamental_score` 必须为空；期权曲面未达到门槛前，`convexity_score` 也必须为空。下游不能把单日横截面活跃度或 Market 状态向量直接当成交易建议。

## 数据源原则

核心期货的自动发布采用 **iFinD-primary** 原则：上期所、上期能源、大商所、郑商所和广期所的具体商品期货日终行情统一从 iFinD Quant API 提取，AKShare 不参与核心期货正式行情。商品期权是明确标记的例外：交易所 EOD 合约目录经 AKShare 适配，受阻时使用 OpenCTP 目录后备；逐合约报价、源日期、IV 和 vendor Greeks 仍由 iFinD 提供。任何来源切换都必须保留来源、时间和口径标记，不得静默混合。

本仓库不建设分钟、逐笔或逐笔盘口历史，但明确建设一个**夜盘收盘快照层**：晨间在日盘开盘前从 iFinD 读取具体期货合约的最新报价，并且只有供应商时间戳落在昨晚已结束的夜盘窗口时才写入 `data/night_session/`。该层保存夜盘 OHLC、最新价、成交量、持仓、源时间戳和相对前结算变动，不和 `data/latest.json` 的日盘 EOD 混合，也不把日盘报价伪装成夜盘。晚间任务更新当天国内日盘 EOD；晨间仍会复核最近一个已完成工作日的国内 EOD，并更新已经完成收盘的海外日频序列。Physical 与 External 已用5个真实历史交易日完成日期、字段、单位和覆盖初始化；期权曲面按用户批准的生产配置在首个通过全部质量门槛的EOD日期直接提升。失败尝试仍只更新 `attempt_latest`、`last_run_status` 与 shadow state，绝不覆盖上一份有效快照。

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

产业数据和海外日频序列只使用 `config/data_foundation.json` 中已固定、已验证的 iFinD 指标 ID；生产任务不依赖自然语言搜索。合约参数与仓单由交易所接口经 AKShare 适配，iFinD 基础数据和专题报表客户端只为后续已验证映射提供只读入口。会员排名和未固定的现货/海外指标保持 `skipped/unavailable`，不得猜测补值。商品期权按下方独立流程采集，目录优先由交易所 EOD 经 AKShare 适配、受阻时使用 OpenCTP 后备，逐合约字段由 iFinD 提供。

### 商品期权全市场采集（目标范围）

商品期权目标目录为64个已上市品种，其中包含焦煤期权，不包含尚无正式挂牌合约的低硫燃料油期权。每日 GitHub Action 按品种逐一尝试：目录和数据错误隔离到单品种；iFinD 明确返回某交易所行情权限不足时只停止该交易所，继续采集其他交易所；认证、额度、传输及无法识别的 HTTP 错误仍按全局失败停止，避免重复消耗额度。每个品种的目录、源日期、合约数、报价覆盖和错误写入 `data/options/last_run_status.json`。这64个品种是目标范围，不代表已经完成64个品种的实采；实际覆盖必须以 Action 实跑后的状态文件为准。

具体期权合约目录优先来自交易所 EOD，经 AKShare 适配；若交易所网页在 GitHub Runner 被阻断，则整批只下载一次 OpenCTP 当前有效合约字典，并仅把它用于合约发现和到期日元数据。逐合约的收盘/结算、成交量、持仓量、标的结算价、源日期、IV 和 vendor Greeks 仍来自 iFinD，缺一合约即阻断该品种。目录来源和行情来源必须分别标记，不能把 OpenCTP 或 AKShare 的目录写成 iFinD 行情。

默认只有成功品种覆盖率 `>=75%` 才允许更新 `data/options/latest.json`；低于门槛时保留上一份有效 `latest`，但把已经通过逐合约校验的当次部分链压缩保存为 `data/options/attempt_latest.json.gz`，并记录本次尝试和失败原因。达到门槛但未覆盖全部目标品种时，质量状态必须为 `partial_chain`，不能声称全市场完整。曲面按每个到期日独立校验：源日期匹配100%、分组键完整且 IV 覆盖率至少80%；OI 覆盖率至少90%才可用于持仓分析。缺少 bid-ask 时曲面仍可发布，但 `execution_ready=false`，不得给出执行建议。

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
python -m china_commodities.cli foundation --audit-only
python -m china_commodities.cli foundation --scope physical --date YYYY-MM-DD
python -m china_commodities.cli foundation --scope external --date YYYY-MM-DD
python -m china_commodities.cli backfill --end-date YYYY-MM-DD --days 252 --history-limit 20 --snapshot-limit 20
python -m china_commodities.cli history-rebuild --retention-days 252
python -m china_commodities.cli report-input --repair-futures-history
python -m china_commodities.cli validate
```

商品期权全品种先做无写入探针：

```powershell
python scripts/collect_ifind_options.py --all-products --date YYYY-MM-DD --dry-run
```

正式执行全品种逐品种采集，默认成功品种覆盖率门槛为75%；是否更新 `latest` 由该门槛和质量校验共同决定：

```powershell
python scripts/collect_ifind_options.py --all-products --date YYYY-MM-DD
```

GitHub Actions 通过 `workflow_dispatch`，或在北京时间**周二至周六 06:13**（UTC 周一至周五 22:13，抓取前一晚夜盘）和**周一至周五 18:15**（UTC 10:15）运行。**06:13 的主任务是提取昨夜夜盘**：夜盘按当前交易日键存储，并以供应商源时间戳严格验证；它还会复核前一个工作日的国内 EOD 和已完成海外日频。**18:15 的主任务是提取当日日盘 EOD**，并对海外日频进行缓存式补采。External 在晨间只以最近完成交易日为请求日，晚间才尝试当日可用序列。手工 `full` 在北京时间 18:15 前会自动采用与晨间相同的“已完成 EOD”日期策略，避免把未收盘的当天日盘误记为失败；对历史日期或 18:15 后的当日手工运行才请求该日期的完整 EOD。夜盘按“可用即发布”处理：无夜盘或整晚无成交的具体合约会保留显式状态，不阻断已有有效夜盘记录，也不会造成重复请求。期权模块单品种失败写入 `data/options/last_run_status.json`；日盘期货、夜盘快照、Physical、External、期权链和期权曲面的提升规则彼此隔离。只有达到各自发布门槛且确有变化的文件才会提交和推送。

每次 Action 会先只读检查仓库中各模块的日期和验证状态。同一请求日期已经存在已验证的期货、期权、Physical 或 External 数据时，对应模块直接跳过；若所有计划模块都已存在，连 access token 也不会换取。只有新日期、缺失数据或上次失败的模块才会请求 iFinD。CLI 和期权脚本还有第二层同日检查；确需重取时必须显式传入 `--force-refresh`。

Physical/External 还按指标做第二层缓存：同日已有可用 EDB 观测时不会重复请求；跨日只查询本地最后观测日之后的增量区间。`last_run_status.json` 的每个 series 状态会记录 `cache_hit`、`request_made`、`query_start_date` 和 `query_end_date`，便于审计实际请求范围。

历史回填优先使用 iFinD 区间查询；若账户对多日合约区间返回参数规模错误，则自动改用共享 access token 的逐日查询。系统取五个交易所共同存在或逐日验证通过的最近20个交易日，在内存中执行与每日任务相同的校验。只有选中的全部日期均通过时才发布，节假日和空返回不会伪装成交易日。历史回填建议在本地一次性执行；GitHub Action 只运行正常的单日更新。

仅采集上期所、上期能源、郑商所和广期所：

```powershell
python -m china_commodities.cli run --provider akshare --date 2026-08-14 --exclude-exchange DCE
python -m china_commodities.cli validate --scope ex-dce
```

当前正式产物发布五所具体期货合约的日终价量仓、期限结构和由此计算的异常候选；未映射的辅助模块保持空值与显式状态。只有 `data/` 下通过相应校验且确有变化的文件才会提交和推送；任一核心市场失败时保留上一份已验证快照，并把本次失败写入 `data/last_run_status.json`。
