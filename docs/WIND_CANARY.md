# Wind 本机小样本验证

## 生产补数路径

用户已决定采用Wind供应商数据，不再要求与外部源数值一致才能发布。
日期、字段、交易所覆盖和重复合约检查仍生效。
本次补11月10/11日期货、核心期权及对应已完成夜盘，全部就绪后一起推送。
不改变云端调度。期权沿用core_v1，一品种选一个活跃标的的配对行权价；
本次Wind目录范围是选中标的，不宣称全月份全行权价链。

```sh
PYTHONPATH=src .venv/bin/python scripts/backfill_wind_futures.py --start 2026-11-10 --end 2026-11-11 --directory tmp/wind-canary/20261111-210959-audit.json
# 串行执行，避免多个SDK采集进程争用：
PYTHONPATH=src .venv/bin/python scripts/backfill_wind_derivatives.py --start 2026-11-10 --end 2026-11-11 --mode options
PYTHONPATH=src .venv/bin/python scripts/backfill_wind_derivatives.py --start 2026-11-10 --end 2026-11-11 --mode night
# 三层全范围通过检查后才写目标目录；可先以tmp目录做发布演练：
PYTHONPATH=src .venv/bin/python scripts/publish_wind_backfill.py --start 2026-11-10 --end 2026-11-11 --output data
PYTHONPATH=src .venv/bin/python -m china_commodities.cli report-input --repair-futures-history
PYTHONPATH=src .venv/bin/python -m china_commodities.cli validate
```

目录只适用于此次已核对的稳定合约窗口，不是任意日期的通用目录。
采集逐合约缓存、串行、每次请求后0.5秒间隔、无重试，无注销或实时订阅。
遇SDK错误或缺失结算值停止；恢复时复用缓存。全范围校验通过后才发布。
Wind成交额保留原始人民币元，同时以万元兼容原iFinD主快照turnover。
原始商业数据缓存不提交；凭据不进入仓库。旧合约元数据按原日期保留。

期权价格及Greeks均来自带历史日期的WSD；IV小数乘100转换为百分数，
Delta/Gamma保留Wind口径，缺失值保留为空。Greeks有值不等于可执行交易。
夜盘采用WSI五分钟历史线按自然日时间窗筛选并聚合，排除零成交量填充值，
使用前一交易日EOD作为收益参考，不将日盘结算或日盘持仓量充当夜盘值。
以下章节为各验证阶段当时的记录，后续结论见 WIND_VALIDATION_20261111.md。

## 2026-11-11 第二阶段核对

`scripts/audit_wind.py` 是本次日期固定的研究脚本，不是生产调度入口。
使用 sample / directory / followup 三种模式，每次串行执行；每次调用后间隔一秒，
父进程240秒超时，逐请求保存结果。禁止同时启动多个验证进程。
`scripts/summarize_wind_directory.py AUDIT --date 2026-11-11` 离线检查目录。

- WSET futurecc 遍历仓库78个品种，77个非空，共806个合约：
  SHFE 238、INE 63、DCE 214、CZCE 243、GFEX 48。
  LR.CZC 返回空目录；旧快照也没有LR，不据此推断退市或权限原因。
  无重复代码、合约日期均覆盖查询日；代码集合与11月9日有效快照完全一致。
  这是配置范围内的覆盖，不是对交易所新增品种的独立全量发现。
- 单位字段为 WSS `punit`，不是 `priceunit`；乘数为 `contractmultiplier`。
  AU=元/克×1000，SC=元/桶×1000，M=元/吨×10，SA=元/吨×20，SI=元/吨×5。
- 上述五合约11月9日 Wind 与已存 iFinD 数据：结算、成交量、持仓量一致，
  Wind `amt` / 旧 `turnover` 均约10000。新适配器保留原始金额，并明确用万元
  兼容旧 iFinD turnover；不能把此规则自动套用到所有历史 AKShare 数据。
  此对照为供应商间验证，尚未完成同日交易所原始成交额的独立复核。
- AU2612 optionchain 返回176个合约，包含行权价、认购认沽、乘数、行权方式、
  first_tradedate/last_tradedate。`expiredate` 是剩余天数，不能当到期日期。
  两只深实值样本EOD可读但IV为空；平值C872/P872的IV为0.2089/0.2071，
  Delta为0.5225/-0.477，Gamma为0.0113/0.0114。
  IV看起来是小数口径，但尚未以官方字段单位/估值回算确认；WSS返回的是查询时刻，
  不能将传入tradeDate视为已经证明该Greeks的源日期，生产发布仍需进一步验证。
- AU2612 WSI：11月10日21:00–21:15返回21:05/10/15三根5分钟线；
  跨午夜查询返回23:55及11月11日00:00/05/10。时间为自然日，夜盘应整体映射
  到11月11日交易日，不能截取时间戳日期。未验证整夜完整性及节假日跨日行为。
- 验证前、中、后看板保持Wind已连接，last_error=null、restarts=0。
  未修改、注销或重启看板；此结果不代表大批量并发的稳定性已通过验证。

本地原始证据（Git忽略）：
`tmp/wind-canary/20261111-210848-audit.json`（初测）、
`20261111-210959-audit.json`（全配置目录）、
`20261111-211147-audit.json`（单位/同日对照/平值Greeks/跨午夜）。

新增 `collectors/wind_adapter.py` 只实现严格EOD字段归一化，尚未挂接生产pipeline。
后续需要跨交易所期权验证、Greeks口径确认、独立目录来源与官方单位复核后再切换。

当前只增加独立诊断入口，尚未替换生产 iFinD 采集。需要官方 WindPy SDK
及已授权的本机登录。本项目使用自己的虚拟环境，不修改 trading desk。

```sh
python3.12 -m venv .venv
.venv/bin/python scripts/probe_wind.py --date 2026-11-11 --codes AU2612.SHF,SC2612.INE,M2701.DCE,SA701.CZC,SI2701.GFE
```

Mac 默认 SDK 路径为 `/Applications/Wind API.app/Contents/python`。
Windows 或非默认安装目录通过 `--sdk-dir` 指定包含 WindPy.py 的目录；
本次未验证 Windows。SDK 搜索路径仅在子进程中生效。

每次最多五个明确合约、一天历史数据、七个字段；顺序调用，间隔一秒，
遇到错误停止，无重试、无订阅、不调用 stop/cancelRequest。
父进程设 90 秒超时，只终止本次子进程。
结果写入 Git 忽略的 `tmp/wind-canary/`，不会写生产 data 或推送。

2026-11-11 Mac 实测：先验证 AU2612.SHF 的 11 月 10 日 EOD；
再验证上述五个交易所样本的 11 月 11 日 EOD，全部返回
open/high/low/close/settle/volume/oi，源日期一致、字段非空且有限。
每次 WSD 调用约 0.07–0.10 秒，不含 SDK 初始化。

这只证明小样本连接和字段可用，未核验成交量单位、官方结算对照、
全市场目录、期权链、分钟夜盘、额度与无人值守运行。
输出明确保留 vendor_native 单位，不能直接当作已验证生产快照。
后续应抽出生产适配器，完成上述核对再接入发布流程。

离线验证：
```sh
.venv/bin/python -m unittest discover -s tests -p test_wind_probe.py -v
```
