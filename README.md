# China Commodities Engine

China Commodities Engine 是一个面向中国商品期货的每日数据 bot 项目骨架。它的职责是采集、标准化、校验并保存可审计的数据，供后续雷达、研究和交易分析使用；数据引擎本身不把单日涨跌直接解释为交易结论。

## 数据层级

数据设计按以下层级组织：

- 合约层：具体合约的交易日、开高低收、结算价、成交量、持仓量和成交额；连续主力仅用于研究，交易应落到具体合约。
- 期限结构层：主力与次主力、跨期价差、曲线变化和换月标记。
- 交割与现货层：仓单、库存、现货价格和基差，并保留地区、品质、单位、税口径和价格时点。
- 期权层：期权合约、隐含波动率、偏度和期限结构；无法确认做市商净头寸时，不把 Gamma 方向写成事实。
- 数据质量层：来源、抓取时间、交易日、状态、新鲜度、回退标记和错误信息。

数据新鲜度按模块独立判断。失败或陈旧数据必须显式标记，不能伪造交易日，也不能让未经验证的陈旧数据进入当日异常扫描；快照在校验通过后才进入历史产物。

其中 `verified` 表示交易日、具体合约、完整交易所覆盖和结构校验通过；`official_complete` 进一步表示五个市场均来自交易所日终接口。若大商所官方接口被拦截，系统只会在新浪返回同一交易日的全部可用具体合约时启用 AKShare 实时行情降级，并明确保留 `is_fallback=true`；该降级不会伪造官方结算价或成交额。

`validate` 会校验已提升的 `latest.json`；首次运行若因单一交易所失败而没有可提升快照，只要 `last_run_status.json` 已完整记录非新鲜状态和阻断原因，结构校验仍通过，便于 Actions 提交可观察的失败状态。它不会把部分运行改写成成功数据。

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

GitHub Actions 通过 `workflow_dispatch` 或工作日北京时间 18:15（UTC 10:15）运行同一组测试、采集和校验命令；只有 `data/` 下经过校验且确有变化的文件才会被提交和推送。
