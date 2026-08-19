# Commodity option EOD data

This module collects end-of-day commodity-option chains after the Chinese
market close. The target catalog contains 64 commodity-option products. The
catalog is a target scope, not a claim that all 64 products have already been
collected successfully. The daily GitHub Action attempts products one by one,
with product-level failure isolation; the actual coverage is reported by
`data/options/last_run_status.json` after each run. Intraday option ticks are
not collected or retained.

## Sources, coverage and promotion

The concrete option-contract directory comes first from exchange end-of-day
data adapted through AKShare. If an exchange website is blocked on a GitHub
runner, the batch downloads the OpenCTP active-instrument dictionary once and
uses it only as an explicitly labelled fallback for contract discovery and
expiry metadata. Per-contract close/settlement, source date, volume, open
interest, underlying settlement, IV and vendor-reported Greeks still come from
iFinD; one missing iFinD quote blocks that product.

Each product gets its own status, source date, contract count, quote coverage
and error record. A product failure does not discard successful products or
abort the status report for the whole batch. An explicit iFinD market-security
denial stops only the named exchange; authentication, quota, transport and
unknown HTTP failures remain global to avoid repeating a known-bad request.
The default promotion rule is:

- if successful-product coverage is at least 75% of the 64-product target,
  update `data/options/latest.json` with the validated batch;
- otherwise, keep the previous `latest` and retain the failed attempt only in
  `data/options/last_run_status.json`; if the partial chain itself passes
  per-contract validation, also store it as the explicitly non-promoted
  `data/options/attempt_latest.json.gz`;
- if the published batch does not cover every target product, its quality
  status is `partial_chain`, and it must not be described as full-market
  complete.

The repository must not claim that all 64 products were successfully sampled
until a real GitHub Action run records that result. `latest.json` is the last
batch that met the promotion threshold, not proof of complete target-scope
coverage.

## Retention

- `data/options/latest.json`: latest batch that met the promotion threshold;
  it may be a `partial_chain` batch and is not automatically full-market
  complete.
- `data/options/attempt_latest.json.gz`: latest validated attempt, including a
  below-threshold partial chain; consumers must inspect `coverage`,
  `attempt_only` and `promotion_eligible` and must not treat it as global
  `latest`.
- `data/options/snapshots/YYYY-MM-DD.json.gz`: compressed full chains for the
  latest 20 successfully published trading days.
- `data/options/history.json`: compact series summaries for the latest 20
  successfully published trading days.
- `data/options/quality_latest.json`: machine-readable readiness gates for the
  raw chain, product scope, volatility surface, model Greeks and executable
  bid/ask data.
- `data/options/last_run_status.json`: per-product attempt status, source dates,
  coverage counts, errors and the promotion decision for the latest run.

Retention is a rolling window of 20 successfully published trading days, not
20 calendar days. A failed, empty or below-threshold collection does not
replace the last valid chain or shorten that window.

Passing raw-chain validation is intentionally weaker than being surface-ready.
A product chain can be complete and same-date while the batch remains
`partial_chain` when target products are missing. Even with complete product
coverage, the repository must not generate a volatility surface, an executable
expression or an execution recommendation until expiry date, exercise style
and bid-ask coverage are all verified. When those fields are missing, it may
retain vendor-reported IV and Greeks as reported, but the quality state is at
most `chain_only` for the available chain.

## Full-chain record

Each option record preserves:

- exchange, product, concrete option contract and underlying futures contract;
- call/put, strike, expiry date and explicitly verified exercise style;
- close/settlement, volume, open interest and underlying settlement;
- vendor-reported IV and Greeks when the configured iFinD report supplies them;
- a model-consistent Greek set calculated from the same end-of-day assumptions;
- source report, data provider, Greek quality and model assumptions.

European futures options use Black-76. American futures options use a CRR tree.
If the exercise style, IV unit, expiry, underlying settlement or risk-free-rate
assumption is not verified, the model layer remains unavailable instead of
silently choosing a default.

Vendor Greeks and model Greeks are stored separately. The model fields use:

- IV in annualized percent;
- Delta per one underlying-price unit;
- Gamma per squared underlying-price unit;
- Vega per one volatility percentage point;
- Theta per calendar day;
- Rho per one interest-rate percentage point.

Option open interest cannot reveal dealers' net position direction. The output
therefore never labels an OI-derived exposure as positive or negative dealer
gamma.

## Contract universe and iFinD quotes

The default route does not require finding option contract codes in
SuperCommand. For each target product, it reads the exchange-published EOD
product directory through AKShare and falls back to the single OpenCTP batch
directory only when the primary directory fails. It converts every concrete
contract to iFinD syntax and requests the standard iFinD end-of-day quote, IV
and vendor Greek fields in batches. An empty directory, missing iFinD contract
or a quote without the requested trade date marks that product as failed;
other products continue to be attempted.

Commodity-option iFinD codes preserve the exchange contract without the
CFFEX-style separators (for example, `CU2609C110000.SHF`).

Run a no-write all-products canary with:

```powershell
python scripts/collect_ifind_options.py --all-products --date YYYY-MM-DD --dry-run
```

Run the daily all-products collection with the default 75% promotion gate:

```powershell
python scripts/collect_ifind_options.py --all-products --date YYYY-MM-DD
```

Other products can still be selected explicitly with `--exchange`, `--product`,
and `--symbol` for a focused canary. A focused canary must not be interpreted
as evidence of full 64-product coverage.

## Optional iFinD Data Pool universe

An account-specific Quant API Data Pool report can still be used when needed.
Its report identifier and field names must first be verified in iFinD
SuperCommand. Copy `config/ifind_options.example.json` to
`config/ifind_options.json` only after that verification and fill in:

1. report identifier and report parameters;
2. exact output field mapping;
3. whether IV is returned as a decimal or percent;
4. contract exercise style from the exchange specification;
5. end-of-day risk-free rate and its named source.

The config contains no token or password. Credentials remain in
`IFIND_REFRESH_TOKEN`.

Run its no-write canary with the explicit mode:

```powershell
python scripts/collect_ifind_options.py --universe-source data-pool --config config/ifind_options.json --date 2026-08-19 --dry-run
```

After the canary returns a non-empty, same-date chain, publish with:

```powershell
python scripts/collect_ifind_options.py --universe-source data-pool --config config/ifind_options.json --date 2026-08-19
```

The collector stops without writing if the report is empty, stale, duplicated,
uses a non-iFinD source, or still contains placeholder configuration.

## Redistribution

Before publishing this repository or its generated option data publicly, the
operator must confirm that the iFinD commercial-data agreement permits
redistribution of the per-contract quotes, IV and vendor Greeks. Credentials
and raw authentication responses are never committed; licensing approval is a
separate requirement from having a valid API token.
