# SA/JM 交易卡价格异常调查与修复（2026-10-26）

## 1. Root Cause Analysis

**结论：异常已定位到 Gemini Radar 报告输出及历史继承链，不能归因于 Engine 原始价格错误，也尚不能证明数字由模型凭空生成。** 1,488 与 1,222 的最早可见归档是 **2026-10-23 晚报**，并非交易卡 ID 所表示的 10 月 21 日。数字在检索输入、提示词、历史上下文还是生成阶段首次被引入，现有归档不能区分。

### 可复现证据

Gemini 归档固定版本：`farfromexact/google-archive@f5556e96934fea2f0a6fa24da50ce56183119857`。

| 报告/位置 | 已核实的事实 | 能证明什么 |
|---|---|---|
| `2026-10-21_morning.md:86,244,252` | 首次出现 `idea_20261021_01`；SA≥1470、JM≤1250/1240 的条件价；未出现 1488/1222 | ID 首次出现与具体价格首次出现是两个事件；晨报不能用当日稍后的收盘价作为当时已知信息 |
| `2026-10-21_evening.md:60,176` | 报出 SA=1476、JM=1234，并出现报告日期与“10-22 今日”不一致 | 早期已有其他价格与时点问题，不能把后来的精确数字倒填至 21 日 |
| `2026-10-22_evening.md:47,128` | 声称 22 日收盘 SA=1476、JM=1228 | 同日 Engine 为 935/1384.5，存在明确不一致 |
| `2026-10-23_morning.md:137` | 名义计算使用 SA=1475、JM=1225；3:1 手数即60吨:60吨，却又称可按1:1.1名义配比 | 估值假设、手数、名义金额混在一起；不是1488/1222首次出现 |
| `2026-10-23_evening.md:13,45,63,120` | 首次明确报出 1488/1222、价差266，标记23日15:00收盘；JSON的 `top_ideas` 同样含这些数字 | 错误价格至少已经进入报告输出，且已用于执行/盈利叙述 |
| `2026-10-24_evening.md:125` | 又称 SA2701 周五943 | 同一归档系列存在相互矛盾的周五报价，尚不能解释变更机制 |
| `2026-10-25_morning.md:46,82,197` | 重复1488/1222；5:2手数被写成约1.22:1名义配比 | 历史传播及独立算术错误 |
| `2026-10-26_morning.md:58,103,151–153` | 重复1488/1222，混合标签“现货/期货收盘价”，进入开仓/止盈/止损卡 | 研究来源与期货执行之间没有有效阻断 |

直接链接：

- [最早含精确数字的23日晚报](https://github.com/farfromexact/google-archive/blob/f5556e96934fea2f0a6fa24da50ce56183119857/global-cross-asset-radar/reports/2026/10/2026-10-23_evening.md#L45)
- [21日首次建卡](https://github.com/farfromexact/google-archive/blob/f5556e96934fea2f0a6fa24da50ce56183119857/global-cross-asset-radar/reports/2026/10/2026-10-21_morning.md#L86)
- [26日详细交易卡](https://github.com/farfromexact/google-archive/blob/f5556e96934fea2f0a6fa24da50ce56183119857/global-cross-asset-radar/reports/2026/10/2026-10-26_morning.md#L144)

23日晚报该路径的 GitHub 提交历史返回一条创建记录：`34a5045868d80cb1dceb2be2579edbccf170b824`，提交时间 `2026-10-23T12:24:39Z`。报告自行标记生成时间20:15 CST。前者是归档提交时间，后者不是运行日志验证过的生成时间。

**来源引用不成立：**23日晚报第45行给这组价格引用的“新华财经商品”链接，实际内容是[《新一期贷款市场报价利率未作调整》](https://www.news.cn/fortune/20261020/c5ceb4f5be3b4924845a929da482a717/c.html)，日期10月20日，正文讲LPR，没有SA/JM报价。该引用既不支持合约价格，也不支持23日收盘时点。26日报告只标交易所名称不能修复这一证据缺口。

### Engine 链路核查

1. `collectors/ifind_http_adapter.py::collect_futures_history` 将 `close→close`、`settlement→settle`、`time→date`，保留具体合约，不在此用现货替换期货。
2. `normalize.py::normalize_futures` 检查API记录日期等于请求日期，标准化数字；本次补充保留 `source_provider/source_endpoint/source_code`，避免明细来源在标准化后丢失。
3. 本地10月21日快照：SA701 close949/settle952；JM2701 close1410/settle1412.5。close差为 **−461**，settle差为 **−460.5**。
4. 另从远端读取10月23日快照，Git blob为 `17e27c8304f8f4f9c5217a468e070c079b77c6e4`：SA701 close963/settle948；JM2701 close1427.5/settle1412。与报告声称日期相同的close差为 **−464.5**。此快照保存于本地调查目录，未替换生产快照。
5. `features.py::contract_month` 已按日期处理郑商所YMM。2026年时SA701与SA2701均指2027年1月；新增执行映射同时核验交易所和品种，拒绝连续、指数、期权和无效月份。
6. `historical_features.py::build_market_state` 提供研究状态；`reporting.py::_compact_product` 保留主力合约、曲线和收益指标。原接口没有完整逐合约执行报价表，也没有输出交易卡校验器。不能将收益/曲线字段当委托报价。
7. Gemini报告（例如21日晨报20行、23日晚报20行及对应JSON）自行披露 `China-Commodities-Engine: permission_error`，声称回退公开来源。**未获得实际读取Engine成功的日志，不能把这条报告链硬接到Engine快照。**
8. `google-archive/README.md:3–19` 明确这是Gemini Drive输出的归档目标；`apps-script/Code.gs:98–111,253,374–380` 读取Drive文本、比较hash并base64上传。所见同步代码不生成交易观点、不计算SA/JM价格、不校验行情。原始Gemini生成器/提示词/检索记录不在该仓库。未修改同步器或任何触发器。

10月21日原始HTTP响应未在本地留存；快照是标准化后的证据，不应称为逐字原始API响应。不能用现有实现证明当时某个未留存API响应的内容。

### 数字搜索与排除

扫描Engine `data` 下156个文本/Parquet文件（36个Parquet，共632,640行），并扫描`.cache`、配置、文档、Automation标记及两套Radar归档。区分字段名、合约、日期，未把相同数字判成来源。

- 1488确实出现在其他历史期货价格，如2026-09-18 **JM2610 close**、09-21 **JM2610 settle**、08-27 **JM2705 close**；都不是本卡SA2701同日价格。
- 1222见于07-30 **JM2609/JM2610 low**，以及其他品种的期权价格、持仓等字段；不是所称JM2701的10-23 close。
- 266也出现在成交/持仓/曲线/期权字段。没有任何证据证明报告引用了上述同值记录。
- 不同历史合约、现货或历史数值误用仍属待证假设，不能指定某条同值记录为根因。

**直接失效机制（已证实）**：未能读取Engine时，无法被引用来源支持的价格仍被标为具体期货收盘价，继而用于可执行卡并在后续报告重复；价格溯源与执行阻断失效。**数字产生机制（未知）**：缺少生成运行输入及检索响应，不能确定是源数据错误、解析错配、上下文污染还是生成错误。

## 2. Impact Assessment

- Gemini固定版本含19份历史Markdown及19份对应JSON（10月16–26日，归档存在日期/版次空缺）；全部扫描。20次涉及明确国内商品合约的JSON交易候选，涉及3个ID：`idea_20261017_01`（含FU/L）、`idea_20261021_01`（SA/JM）、`idea_20261024_01`（含CU）。均缺本次要求的逐腿快照溯源对象，不能认定为已验证可执行卡。
- 已确认SA/JM卡不止26日一份异常。23日晚报还宣称“已持仓盈利”，但无成交回报，不能据此确认真实持仓或损益。
- 25日晨报46行的行情表另写FU2612日盘5838、LC2701结算99080；同日23日Engine快照对应close5481、settle93800。属于其他商品的同口径数值冲突，应回查；不能把这一发现直接解释成FU/L组合已成交损失。CU78650标夜盘，不能与日盘107900直接做同会话判错。
- 本地GPT Radar独立扫描60份Markdown（另有JSON、latest副本），未找到该ID及本次精确数字。该checkout主要是旧档，**不覆盖用户此前检查的10月21日GPT报告**，不能据此宣称GPT全部无影响。
- Engine本地没有既有可执行交易卡生成器/历史卡库；不得报告“Engine全部历史卡校验通过”。Git可见代码/文档历史搜索未找到该ID对应生成逻辑。
- 机器可读清单：`docs/experiments/sa-jm-price-audit-20261026.json`。完整字段命中位于`tmp/sa-jm-audit-complete.json`；一个非UTF-8原始备份页面无法解析。二进制zip、未获取的Drive历史版本、不可达Git历史、外部运行上下文不在此次覆盖范围。

## 3. Fix Summary

### 执行边界

- 新增 `src/china_commodities/execution.py`：`quote_book` 从可信快照导出close/settle；必须有明确合约、交易所、正有限价格、日期、来源、带时区观察时间及快照hash。`observed_at_kind=snapshot_generated_at` 明确这是快照观察时间，不伪装成逐笔行情时间。
- `validate_quote` 从调用方提供的可信快照重新解析，逐字段比较，阻止把现货换成close标签、改价格、改日期、伪造hash或合约映射。候选卡自身的来源标签不构成证据。
- `gate_card` 检查两腿同日期、同价格类型；数量为正整数；乘数与同日非陈旧元数据一致；费用/滑点显式且非负；合约未过期；止盈止损为组合人民币净损益预算。未支持的任意开仓/止损/止盈字段拒绝透传。
- 失败返回 `status=research_only, execution=null`，原 `research` 原样保留。通过返回 `validated_reference_plan`，**仅为指定交易日的历史/EOD参考计划，不能代表当前可成交盘口**。本修复不提供自动下单。
- `reporting.py` schema升为3；保留原研究模块，增加 `execution_prices`、`execution_policy`、`trade_cards`。若提供`data/trade_cards/candidates.json`，必须经过gate才能进入执行对象。现有报告发布命令自然使用新逻辑，不改Actions时间或触发逻辑。
- `scripts/validate_trade_cards.py` 为独立下游输出出口：校验失败仍写出研究降级结果并返回exit1。旧式Radar自由文本必须由其生成方转换为候选结构并调用此出口；本地修复无法约束未接入的外部生成器。
- `scripts/audit_execution_prices.py` 支持JSON字段上下文、CSV、Parquet及文本行搜索，输出卡候选和不可读文件，不以数字相同推断来源。

### 组合计算

`portfolio_pnl` 对每条腿使用方向×手数×乘数×价格变化，并扣除显式假设的双边总费用及双边总滑点价格×数量。费用单位是每手往返人民币，滑点单位是往返合计元/吨（或该合约报价单位）；不是单边参数。货币为人民币。

以21日close为基准：

| 项目 | 数值 |
|---|---:|
| 报价差 S | 949−1410 = −461元/吨 |
| SA数量 | 5×20 = 100吨 |
| JM数量 | 2×60 = 120吨 |
| 多腿名义 / 空腿名义 | 94,900 / 169,200元 |
| 名义金额差 V | −74,300元 |
| 失衡程度：绝对净名义/总名义 | 28.13% |

毛损益 `100×ΔSA−120×ΔJM`。若SA+10、JM−20，毛利3400元；测试明确假设每手往返费用10元、每腿往返滑点价格1元，则费用70、滑点220、净利3110元。**这是假设计算，不是券商收费或预测。**

即使两个场景的报价差变化均为+30，SA+20/JM−10时毛利3200元，与上一场景不同。故单一230/305/340价差阈值不能唯一确定本组合止损止盈金额。

按报告自己的错误价格算，5:2名义为148800:146640，约1.0147:1，也不是1.22:1或1:1.1。新输出同时列报价差、数量、多空名义、失衡率、毛/净损益；净损益风险触发不是损失封顶承诺，跳空/成交失败仍可越界。不同商品的品质、地区、税费及交割条件不可互换，现货/库存/基差继续用于研究，不能直接转换为委托价。

## 4. Test Results

**提交前最终结果：全套回归237 passed、111 subtests passed（35.75秒）。** 本轮正常权限执行成功，此前临时目录导致的4项阻塞已全部解除。补充测试验证了CLI成功/失败退出码、失败仍保留研究，以及极大数值导致组合计算溢出时禁止输出执行计划。以下早期测试记录仅保留调查过程，不代表当前仍有阻塞。

- `tests/fixtures/sa_jm_20261021.json` 从真实21日快照逐字段摘录两腿，附完整原快照hash。生产逻辑没有硬编码1488/1222/−461。
- `tests/test_execution.py` 覆盖真实close差、郑商所跨十年映射、现货及伪装close、错误来源/hash/时点、异步日期/旧快照、close/settle混用、5:2损益与费用、非法数量、乘数错配、元数据缺失/过期、重复合约、未验证/非有限快照、未知止损字段、report_input集成。
- 正常沙箱可运行的执行、标准化、触发逻辑测试通过；最终数字见本文件末尾验收记录。
- 初始三文件运行21 passed、4 failed、19 subtests passed。4项失败全部是Windows `tempfile.TemporaryDirectory` 创建目录后不能读写/清理的PermissionError（1项新增集成、3项原reporting测试），不是断言差异。
- 两次正常权限回归申请均被自动审批超时拒绝。未宣称完整回归通过。
- 用PowerShell创建工作区目录，真实调用`build_report_input`补做文件集成：4条溯源报价导出、错误卡execution=null、研究保留，全部通过。另对完整本地data构建只读预览：schema3、1490条close/settle报价、61条拒绝记录，未改生产数据。

## 5. Remaining Risks / 下游接入

1. Gemini需要提供21日首次建卡、22日晚报及23日晚报的完整提示词、前序报告实际读取版本、检索请求/响应、工具访问失败信息、运行ID、原始生成输出和Drive版本时间。只有这些才能回答数字在模型输入之前还是输出时首次出现。
2. 外部必须执行“研究生成→结构化候选→本校验器→只渲染execution对象”流程。私有Engine不可读时，可使用独立核验并归档的期货快照，但不能用行业报道标签替代。未接入时，即使Engine新增约束也无法阻止Gemini继续输出自由文本交易建议。
3. `as_of_date`必须由可信发布方指定，不能从候选卡自报日期取值。EOD计划可指定最近已完成交易日；新会话下单前需刷新可交易报价，不能把旧日计划标为实时可执行。不同来源的盘中last目前保守不支持。
4. 当前本地JM2701合约元数据乘数缺失，按新规则会降级。没有为了放行卡而补写60；60仅用于用户指定组合的测试/示例。恢复执行需真实元数据证据。
5. 修复随本次代码提交发布；Google Drive以及Automation提示词未被修改。旧归档作为调查证据保留，未覆盖成新价格。没有更改任何触发器。仓库既有report-input步骤下一次运行时生成schema v3；此次不重写已发布的生产数据。

### 下游调用示例

```powershell
python scripts/validate_trade_cards.py --snapshot data/latest.json --metadata data/contract_meta.json --candidates data/trade_cards/candidates.json --as-of-date YYYY-MM-DD --output validated_cards.json
```

每张候选顶层只允许 `id/research/legs/risk`。每腿只允许 `quote/lots/side/multiplier/round_trip_fee_per_lot/round_trip_slippage_price`；quote必须完整复制对应`execution_prices.quotes`对象；side为long/short。risk只允许正数`max_loss_cny/take_profit_cny`。不要把失败候选原始文本继续放在“执行建议”栏目；研究可以保留并注明执行未核验。

审计复跑：

```powershell
python scripts/audit_execution_prices.py data tmp/sa-jm-source/global-cross-asset-radar --values 1488 1222 266 --output tmp/sa-jm-audit.json
```

早期分组验收记录（已由上述全套回归覆盖）：

- 执行/标准化/触发逻辑：`23 passed, 1 deselected, 19 subtests passed`；deselected是受临时目录权限影响的集成用例，已另用真实工作区文件验证。
- iFinD HTTP适配/期货特征/历史特征：`24 passed`。
- 合计47项测试通过，另有19个参数子用例；没有声称整个仓库测试套件通过。
- 完整原始21日快照重新读取计算：SA701→SA2701，close差−461，通过。
- `git diff --check`通过；没有修改`.github`、触发标记或生产`data`文件。

提交前已快进同步远端6次纯数据提交，并在最新数据上只读构建report_input验证：交易日2026-10-23、1490条溯源报价、61条拒绝记录；SA2701 close963、JM2701 close1427.5。未重新抓取供应商数据。
