# Independent exchange EOD backup experiment

This branch adds an independently collected commodity futures/options backup.
It uses no iFinD credentials and does not modify the production iFinD snapshots.
The existing daily workflow has an explicit `exchange_backup` dispatch mode;
that mode skips the production job, requests no secrets, and uploads results
as an artifact instead of committing market data. Public artifacts contain
aggregate reports and source audit manifests; full raw quotes remain local.

## Acceptance contract

The user's 80% target is evaluated on separate denominators, not a single
invented equivalence score:

1. **Catalog products:** at least 80% of the existing 64 option products have
   a nonempty dated report and at least 80% completeness across settlement,
   volume, open interest, and same-date underlying settlement.
2. **Same-date iFinD contracts:** intersection of normalized exchange/contract
   keys divided by the complete archived iFinD chain for that date. Missing
   exchanges and contracts remain in the denominator.
3. **Field availability and agreement:** field-by-field nonnull coverage,
   relative to iFinD's nonnull observations, and agreement on paired values.
   A returned value is not proof of agreement. Model Greeks are reported
   separately and never counted as observed vendor fields.
4. **Analysis capabilities:** PCR without missing-as-zero aggregation,
   expiry and IV coverage, individual usable surfaces, and model coverage.
   Bid/ask execution data, intraday paths, and dealer position direction are
   outside this EOD backup's capabilities.

This single-run gate only controls `data/backup/latest.json.gz`. It does not
authorize promotion into the main pipeline. Ten distinct successful trading
dates plus fault/recovery exercises remain the proposed production gate.

## Sources and evidence

- SHFE/INE: whole-exchange daily JSON. Validate `report_date`; filter the
  combined SHFE option report into the correct exchange. Do not apply
  series-level `SIGMA` to individual option contracts.
- CZCE: whole-exchange pipe-separated daily report. Validate the date in its
  title, header mapping, and row shape. Do not depend on an SDK's fixed list
  of product display names.
- GFEX: whole-exchange portal JSON. Its returned `param.trade_date` is a query
  echo, **not** an independently observed quote timestamp. Retain this weaker
  evidence classification in every record; reject absent/conflicting dates.
- DCE: official daily portal; HTTP errors or missing date evidence fail the
  exchange explicitly. No attempt is made to bypass access-control challenges.
- OpenCTP: current contract metadata only (expiry/open date), archived once.
  It supplies no backup price, settlement, IV or Greeks. Current listings are
  not a historical point-in-time universe; expired missing metadata stays null.
- Exercise styles: the existing versioned exchange-rule registry.

Each download saves request parameters, URL, timestamp, status, raw response
bytes, SHA-256 and date evidence under `raw/<trade_date>/`. Failed HTTP
responses are preserved too. Quotes and benchmark baselines are separate;
iFinD data never fills missing backup observations.

Turnover is converted from the documented ten-thousand-CNY reporting unit
while its original value is retained. Volume/OI retain reported counts;
single/double-sided conventions and price-time bases must be reconciled before
interpreting disagreement with iFinD. No-trade zero OHLC placeholders become
null; a settlement remains distinct from the transaction close.

## Run and replay

```powershell
python -X utf8 -u scripts/collect_exchange_backup.py --date 2026-09-15

# Strict acceptance: return exit code 2 when catalog coverage is below 80%.
python -X utf8 scripts/collect_exchange_backup.py --date 2026-09-15 --require-80

# Deterministic source replay, no network access; raw and metadata hashes checked.
python -X utf8 scripts/collect_exchange_backup.py --date 2026-09-15 --replay-raw data/backup/raw/2026-09-15 --output data/backup/replay

# Optional model scenario. 2% is an explicit experiment assumption, not a live rate.
python -X utf8 scripts/collect_exchange_backup.py --date 2026-09-15 --replay-raw data/backup/raw/2026-09-15 --output data/backup/model-scenario --risk-free-rate 0.02 --rate-source experiment_assumption_annual_2pct_not_observed --model-workers 4
```

The optional calculation reuses Black-76 for European exercise and a CRR tree
for American exercise. Unknown inputs or failed inversion leave model output
unavailable. A model result is not a validation of iFinD's proprietary Greeks;
tree convergence and pricing-assumption sensitivity need further assessment.
The default run makes no interest-rate assumption and calculates no model Greeks.

## Outputs and selection

- `last_run_status.json`, `reports/YYYY-MM-DD.json`: coverage, errors, comparison,
  capability counts and publication decision, including below-threshold runs.
- `snapshots/YYYY-MM-DD.json.gz`: independently collected options and futures.
- `attempt_latest.json.gz`: latest attempt, explicitly not a fresh full-market guarantee.
- `latest.json.gz`: last backup that passed the 80% catalog gate; a failed
  newer attempt or an older replay cannot replace it.
- `effective_latest.json.gz`: experimental whole-product selection, preferring
  same-date iFinD where the same core gate passes, otherwise the dated exchange
  chain. No individual-field splicing; every decision/rejection is recorded.
- `surface_attempt_latest.json.gz`: per-series capability assessment with
  correct exchange provenance. No iFinD source label is fabricated.

`effective_latest` is an **attempt for its explicitly stated date**, and can
be empty. Consumers must read the date/decisions. It is not the production
`data/options/latest.json`. The production iFinD-only validator remains intact
until the independent sources have passed the proposed shadow qualification.

The experimental output tree is ignored by Git. Curated experiment summaries
are in `docs/experiments/`; full raw archives remain local. GitHub artifacts
retain only aggregate reports and request/hash audit manifests.
Local experiment files currently have no automatic retention cleanup.

## Initial experiment, 2026-09-16

Branch base: `97eeb9d` (remote main, fetched before starting). Frozen comparison
date: 2026-09-15; an independent second date, 2026-09-14, checks reproducibility.

The local 2026-09-15 run produced 15,788 target option contracts across 45/64
products (70.3125%), plus 587 concrete futures. All collected options had the
four core fields. Expiry metadata coverage was 100%; observed per-contract IV
coverage was 54.4844%. There were 132 usable surfaces out of 214 observed series
under the existing per-series gate, and no executable bid/ask surfaces.

The same-date iFinD baseline contained 18,936 contracts over 52/64 products.
10,672 contracts matched (56.3583%); backup core-field availability relative to
iFinD was 54.9223%. The backup also covered 12 products missing from the iFinD
baseline, so raw record-count ratios are not a valid replacement-coverage metric.
Paired settlement agreement at rtol=1e-4 / atol=1e-6 was 73.6101%; IV values
often differ. These are not yet interchangeable vendor outputs.

The 2026-09-14 run also returned 45/64 products, with 15,642 option contracts.
Both dates failed all 19 DCE option products with HTTP 412. Thus the independent
backup **has not met the 80% target**. Production automatic takeover remains off.

Validation: 192 unit tests passed after initial implementation, including
stale-source rejection, exchange isolation, no-trade normalization, raw replay
integrity, missing-data PCR behavior, source labeling and failure/recovery.
Subsequent model/GitHub trials and final validation are recorded separately.

References:
- https://akshare.akfamily.xyz/data/option/option.html
- Existing SDK adapters and the archived official responses (see hashes).
