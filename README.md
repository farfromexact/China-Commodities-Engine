# China Commodities Engine

China Commodities Engine 是一个面向中国商品期货的每日数据 bot 项目骨架。它的职责是采集、标准化、校验并保存可审计的数据，供后续雷达、研究和交易分析使用；数据引擎本身不把单日涨跌直接解释为交易结论。

## 数据层级

数据设计按以下层级组织：

- 合约层：具体合约的交易日、开高低收、结算价、成交量、持仓量和成交额；连续主力仅用于研究，交易应落到具体合约。
- 期限结构层：主力与次主力、跨期价差、曲线变化和换月标记。
- 交割与现货层：仓单、库存、现货价格和基差，并保留地区、品质、单位、税口径和价格时点。
- 期权层：当前正式产物只发布产品级成交量、持仓量、PCR和混合期限IV摘要；尚未形成可交易的ATM IV、偏度或期限结构曲面，无法确认做市商净头寸时也不推断 Gamma 方向。
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

- `data/latest.json`：各交易所和具体合约的最新标准化数据。
- `data/radar_latest.json`：期限结构、基差、仓单和异常候选的当前摘要。
- `data/radar_history.json`：面向历史比较的紧凑日级记录。
- `data/contract_meta.json`：合约乘数、最小变动价位、交易与到期属性等元数据。
- `data/last_run_status.json`：各数据模块的状态、错误和新鲜度。
- `data/snapshots/YYYY-MM-DD.json`：通过校验的日级快照。

## 数据源原则

本项目采用 **AKShare-first but not AKShare-only** 原则：AKShare 是默认采集入口，但不是唯一数据源。不同交易所或数据模块失败时，可以使用有明确来源、时间和口径标记的官方直连或其他适配器；回退数据不得静默覆盖主数据，也不得在来源不一致时伪装成同一口径。

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
python -m china_commodities.cli run
python -m china_commodities.cli validate
```

仅采集上期所、上期能源、郑商所和广期所：

```powershell
python -m china_commodities.cli run --date 2026-08-14 --exclude-exchange DCE
python -m china_commodities.cli validate --scope ex-dce
```

GitHub Actions 通过 `workflow_dispatch` 或工作日北京时间 18:15（UTC 10:15）运行测试，并默认执行排除 DCE 的四交易所范围化采集与校验。产物更新在 `data/scoped/ex-dce/`；只有 `data/` 下经过校验且确有变化的文件才会被提交和推送。DCE 恢复稳定后，可以移除工作流中的 `--exclude-exchange DCE` 并恢复全市场发布。
