"""Normalize heterogeneous AKShare frames into conservative canonical records."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable

import pandas as pd

from .catalog import ProductCatalog


FUTURES_NUMERIC_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "turnover",
    "settle",
    "pre_settle",
)

# AKShare's SHFE daily route currently returns both SHFE and INE rows. Keep the
# exchange boundary explicit so the same INE contract cannot be published twice.
INE_FUTURES_PRODUCTS = frozenset({"BC", "EC", "LU", "NR", "SC"})


def iso_date(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip().replace("/", "-")
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text).isoformat()


def _canonical_product(value: Any) -> str:
    match = re.search(r"[A-Za-z]+", str(value or ""))
    return match.group(0).upper() if match else ""


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", "", regex=False), errors="coerce")


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    clean = frame.astype(object).where(pd.notna(frame), None)
    return clean.to_dict(orient="records")


FUTURES_SOURCE_DATE_COLUMNS = (
    "trade_date",
    "date",
    "tradedate",
    "TRADEDATE",
    "交易日期",
    "交易日",
    "日期",
)


def _source_dates(raw: pd.DataFrame) -> tuple[str, list[str]]:
    """Return the published date column and its distinct valid ISO dates."""
    columns_by_text = {str(column): column for column in raw.columns}
    source_column = next(
        (
            columns_by_text[candidate]
            for candidate in FUTURES_SOURCE_DATE_COLUMNS
            if candidate in columns_by_text
        ),
        None,
    )
    if source_column is None:
        raise ValueError("futures frame missing source trade date column")

    dates: set[str] = set()
    invalid_values: list[str] = []
    for value in raw[source_column].dropna().tolist():
        text = str(value).strip()
        if not text or text.lower() in {"nan", "nat", "none"}:
            continue
        try:
            if re.fullmatch(r"\d{8}(?:\.0)?", text):
                parsed = iso_date(text[:8])
            else:
                timestamp = pd.to_datetime(value, errors="raise")
                parsed = timestamp.date().isoformat()
            dates.add(parsed)
        except (TypeError, ValueError, OverflowError):
            invalid_values.append(text)
    if invalid_values:
        examples = ", ".join(sorted(set(invalid_values))[:3])
        raise ValueError(f"unparseable futures source trade date: {examples}")
    if not dates:
        raise ValueError("futures source trade date column is empty")
    return str(source_column), sorted(dates)


def normalize_futures(
    raw: pd.DataFrame, exchange: str, trade_date: str
) -> list[dict[str, Any]]:
    """Normalize one exchange's concrete futures contracts."""
    required = {"symbol", "variety"}
    missing = sorted(required.difference(raw.columns))
    if missing:
        raise ValueError(f"futures frame missing columns: {', '.join(missing)}")
    if raw.empty:
        return []

    requested_date = iso_date(trade_date)
    source_date_column, source_dates = _source_dates(raw)
    if source_dates != [requested_date]:
        raise ValueError(
            "futures source trade date mismatch: "
            f"requested={requested_date}, observed={','.join(source_dates)}"
        )

    frame = pd.DataFrame(index=raw.index)
    frame["trade_date"] = requested_date
    frame["requested_trade_date"] = requested_date
    frame["source_trade_date"] = source_dates[0]
    frame["source_date_match"] = True
    frame["source_date_column"] = source_date_column
    frame["exchange"] = exchange.upper()
    frame["product"] = raw["variety"].map(_canonical_product)
    frame["contract"] = raw["symbol"].astype(str).str.strip().str.upper()
    for column in FUTURES_NUMERIC_COLUMNS:
        if column in raw.columns:
            frame[column] = _numeric(raw[column])
        else:
            frame[column] = None

    zero_ohlc_placeholder = (
        frame["open"].eq(0)
        & frame["high"].eq(0)
        & frame["low"].eq(0)
        & frame["close"].gt(0)
    )
    frame["ohlc_quality"] = "complete"
    frame.loc[zero_ohlc_placeholder, ["open", "high", "low"]] = None
    frame.loc[
        zero_ohlc_placeholder, "ohlc_quality"
    ] = "exchange_zero_placeholder_normalized_to_null"
    partial_ohlc = frame[["open", "high", "low", "close"]].isna().any(axis=1)
    frame.loc[
        partial_ohlc & ~zero_ohlc_placeholder, "ohlc_quality"
    ] = "partial_missing_fields"

    frame = frame[
        frame["product"].ne("")
        & frame["contract"].str.contains(r"\d", regex=True, na=False)
    ].copy()
    if exchange.upper() == "SHFE":
        frame = frame[~frame["product"].isin(INE_FUTURES_PRODUCTS)].copy()
    elif exchange.upper() == "INE":
        frame = frame[frame["product"].isin(INE_FUTURES_PRODUCTS)].copy()
    frame["close_return_pct"] = (
        frame["close"] / frame["pre_settle"] - 1.0
    ) * 100.0
    frame["settle_return_pct"] = (
        frame["settle"] / frame["pre_settle"] - 1.0
    ) * 100.0
    frame.sort_values(["exchange", "product", "contract"], inplace=True)
    frame.reset_index(drop=True, inplace=True)
    return _records(frame)


def _find_column(columns: Iterable[Any], candidates: tuple[str, ...]) -> str | None:
    names = {str(column): column for column in columns}
    for candidate in candidates:
        if candidate in names:
            return str(names[candidate])
    return None


def _total_row(frame: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    text = frame.astype(str)
    total_mask = text.apply(lambda column: column.str.contains("总计", na=False)).any(axis=1)
    if total_mask.any():
        return frame.loc[total_mask].tail(1), "source_total"
    if "ROWSTATUS" in frame.columns:
        status = pd.to_numeric(frame["ROWSTATUS"], errors="coerce")
        if status.eq(1).any():
            return frame.loc[status.eq(1)].tail(1), "source_status_total"
    return frame, "sum_rows"


def _product_from_table(
    key: str | None, frame: pd.DataFrame, catalog: ProductCatalog
) -> str:
    if key:
        code = _canonical_product(key)
        if code and code in catalog.product_to_sector:
            return code
        reverse_names = {name: product for product, name in catalog.names.items()}
        if str(key).strip() in reverse_names:
            return reverse_names[str(key).strip()]
    for column in ("品种代码", "品种", "PRODUCTGROUPID", "PRODUCTID"):
        if column in frame.columns and not frame.empty:
            code = _canonical_product(frame[column].iloc[0])
            if code:
                return code
    return _canonical_product(key)


def normalize_warehouse(
    raw: Any,
    exchange: str,
    trade_date: str,
    catalog: ProductCatalog,
) -> list[dict[str, Any]]:
    """Create one conservative warehouse total per source product table."""
    if isinstance(raw, dict):
        tables = [(str(key), value) for key, value in raw.items()]
    elif isinstance(raw, pd.DataFrame):
        if raw.empty:
            return []
        split_column = _find_column(raw.columns, ("品种代码", "品种"))
        if split_column:
            tables = [(str(key), value) for key, value in raw.groupby(split_column)]
        else:
            tables = [(None, raw)]
    else:
        raise TypeError(f"unsupported warehouse payload: {type(raw).__name__}")

    quantity_candidates = (
        "今日仓单量（手）",
        "今日仓单量",
        "仓单数量",
        "仓单量",
        "WRTWGHTS",
        "仓单数量(手)",
        "数量",
    )
    change_candidates = (
        "增减（手）",
        "增减",
        "当日增减",
        "WRTCHANGE",
        "仓单变化",
    )
    output: list[dict[str, Any]] = []
    for key, value in tables:
        frame = value if isinstance(value, pd.DataFrame) else pd.DataFrame(value)
        if frame.empty:
            continue
        quantity_column = _find_column(frame.columns, quantity_candidates)
        if not quantity_column:
            continue
        change_column = _find_column(frame.columns, change_candidates)
        selected, method = _total_row(frame)
        quantity_values = _numeric(selected[quantity_column]).dropna()
        if quantity_values.empty:
            continue
        change_values = (
            _numeric(selected[change_column]).dropna()
            if change_column is not None
            else pd.Series(dtype=float)
        )
        quantity = (
            float(quantity_values.iloc[-1])
            if method != "sum_rows"
            else float(quantity_values.sum())
        )
        change = None
        if not change_values.empty:
            change = (
                float(change_values.iloc[-1])
                if method != "sum_rows"
                else float(change_values.sum())
            )
        product = _product_from_table(key, frame, catalog)
        output.append(
            {
                "trade_date": iso_date(trade_date),
                "exchange": exchange.upper(),
                "product": product,
                "warehouse_quantity": quantity,
                "warehouse_change": change,
                "source_unit": "as_published",
                "quantity_column": quantity_column,
                "aggregation_method": method,
            }
        )
    return sorted(output, key=lambda item: (item["exchange"], item["product"]))


def normalize_basis(raw: pd.DataFrame, trade_date: str) -> list[dict[str, Any]]:
    """Normalize 生意社 data and explicitly label it as a proxy basis."""
    if raw.empty:
        return []
    required = {"symbol", "spot_price"}
    missing = sorted(required.difference(raw.columns))
    if missing:
        raise ValueError(f"basis frame missing columns: {', '.join(missing)}")
    output: list[dict[str, Any]] = []
    numeric_columns = (
        "spot_price",
        "near_contract_price",
        "dominant_contract_price",
        "near_basis",
        "dom_basis",
        "near_basis_rate",
        "dom_basis_rate",
    )
    for _, row in raw.iterrows():
        item: dict[str, Any] = {
            "trade_date": iso_date(trade_date),
            "product": _canonical_product(row.get("symbol")),
            "basis_kind": "proxy_basis",
            "basis_definition": "futures_minus_spot",
            "spot_source": "100ppi",
            "spot_region": None,
            "spot_grade": None,
            "tax_included": None,
            "price_time": None,
            "near_contract": str(row.get("near_contract", "")).upper() or None,
            "dominant_contract": str(row.get("dominant_contract", "")).upper() or None,
        }
        for column in numeric_columns:
            value = pd.to_numeric(row.get(column), errors="coerce")
            item[column] = float(value) if pd.notna(value) else None
        published_rates = [
            abs(float(item[column]))
            for column in ("near_basis_rate", "dom_basis_rate")
            if item.get(column) is not None
        ]
        item["basis_quality"] = (
            "D_display_only_not_comparable"
            if published_rates and max(published_rates) > 20.0
            else "C_proxy_partial_definition"
        )
        item["directional_scoring_allowed"] = False
        output.append(item)
    return sorted(output, key=lambda item: item["product"])


OPTION_COLUMN_MAP = {
    "合约": "contract",
    "合约代码": "contract",
    "合约名称": "contract",
    "昨结算": "pre_settle",
    "前结算价": "pre_settle",
    "今开盘": "open",
    "开盘价": "open",
    "最高价": "high",
    "最低价": "low",
    "今收盘": "close",
    "收盘价": "close",
    "今结算": "settle",
    "结算价": "settle",
    "成交量(手)": "volume",
    "成交量": "volume",
    "持仓量": "open_interest",
    "增减量": "open_interest_change",
    "持仓量变化": "open_interest_change",
    "成交额(万元)": "turnover",
    "成交额": "turnover",
    "DELTA": "delta",
    "Delta": "delta",
    "德尔塔": "delta",
    "隐含波动率": "iv_percent",
    "隐含波动率(%)": "iv_percent",
    "行权量": "exercise_volume",
}


def _option_parts(contract: str) -> tuple[str | None, str | None, float | None]:
    upper = contract.upper().replace(" ", "")
    underlying_match = re.match(r"([A-Z]+)(\d{3,4})", upper)
    underlying = (
        f"{underlying_match.group(1)}{underlying_match.group(2)}"
        if underlying_match
        else None
    )
    option_match = re.search(r"[-]?([CP])[-]?(\d+(?:\.\d+)?)$", upper)
    if not option_match:
        return underlying, None, None
    return underlying, option_match.group(1), float(option_match.group(2))


def normalize_options(
    raw: pd.DataFrame,
    exchange: str,
    product: str,
    source_symbol: str,
    trade_date: str,
) -> list[dict[str, Any]]:
    """Normalize a single commodity-option product response."""
    if raw.empty:
        return []
    renamed = raw.rename(columns=OPTION_COLUMN_MAP).copy()
    if "contract" not in renamed.columns:
        raise ValueError("option frame missing contract column")
    output: list[dict[str, Any]] = []
    numeric_columns = (
        "pre_settle",
        "open",
        "high",
        "low",
        "close",
        "settle",
        "volume",
        "open_interest",
        "open_interest_change",
        "turnover",
        "delta",
        "iv_percent",
        "exercise_volume",
    )
    for _, row in renamed.iterrows():
        contract = str(row.get("contract", "")).strip().upper()
        if not contract or not re.search(r"\d", contract):
            continue
        underlying, option_type, strike = _option_parts(contract)
        item: dict[str, Any] = {
            "trade_date": iso_date(trade_date),
            "exchange": exchange.upper(),
            "product": product.upper(),
            "source_symbol": source_symbol,
            "contract": contract,
            "underlying_contract": underlying,
            "option_type": option_type,
            "strike": strike,
        }
        for column in numeric_columns:
            value = pd.to_numeric(row.get(column), errors="coerce")
            item[column] = float(value) if pd.notna(value) else None
        item["iv_source"] = (
            "exchange_contract" if item.get("iv_percent") is not None else None
        )
        output.append(item)
    return sorted(output, key=lambda item: item["contract"])


def normalize_option_series_volatility(raw: pd.DataFrame) -> dict[str, float]:
    """Return underlying-series IV in percent, preserving exchange semantics."""
    if raw.empty:
        return {}
    if "合约系列" not in raw.columns or "隐含波动率" not in raw.columns:
        raise ValueError("option volatility frame missing contract series or IV")
    output: dict[str, float] = {}
    for _, row in raw.iterrows():
        series = str(row["合约系列"]).strip().upper()
        value = pd.to_numeric(row["隐含波动率"], errors="coerce")
        if not series or pd.isna(value) or float(value) <= 0:
            continue
        iv = float(value)
        output[series] = iv * 100.0 if iv <= 2.0 else iv
    return output


def normalize_member_rankings(
    raw: Any, exchange: str, trade_date: str
) -> list[dict[str, Any]]:
    """Summarize reconciled top-20 rows without treating totals as members."""
    if not isinstance(raw, dict):
        raise TypeError(f"unsupported member ranking payload: {type(raw).__name__}")
    output: list[dict[str, Any]] = []
    numeric_columns = (
        "vol",
        "vol_chg",
        "long_open_interest",
        "long_open_interest_chg",
        "short_open_interest",
        "short_open_interest_chg",
    )
    for key, value in raw.items():
        frame = value if isinstance(value, pd.DataFrame) else pd.DataFrame(value)
        if frame.empty:
            continue
        contract = str(
            frame["symbol"].dropna().iloc[0]
            if "symbol" in frame.columns and not frame["symbol"].dropna().empty
            else key
        ).strip().upper()
        product = (
            _canonical_product(frame["variety"].dropna().iloc[0])
            if "variety" in frame.columns and not frame["variety"].dropna().empty
            else _canonical_product(contract)
        )
        source_row_count = int(len(frame))
        text = frame.astype(str)
        total_mask = text.apply(
            lambda column: column.str.contains(
                r"总计|合计|小计|\bTOTAL\b",
                case=False,
                regex=True,
                na=False,
            )
        ).any(axis=1)
        ranks = (
            _numeric(frame["rank"])
            if "rank" in frame.columns
            else pd.Series(float("nan"), index=frame.index)
        )
        invalid_rank_mask = ranks.isna() | ranks.lt(1) | ranks.gt(20)
        removed_mask = total_mask | invalid_rank_mask
        member_rows = frame.loc[~removed_mask].copy()
        member_ranks = ranks.loc[member_rows.index].astype(int)
        published_top_n = int(member_ranks.max()) if not member_ranks.empty else 0
        expected_ranks = set(range(1, published_top_n + 1))
        observed_ranks = set(member_ranks.tolist())
        ranking_reconciled = bool(
            published_top_n > 0
            and published_top_n <= 20
            and observed_ranks == expected_ranks
            and not member_ranks.duplicated().any()
            and len(member_rows) == published_top_n
        )

        sums: dict[str, float | None] = {}
        for column in numeric_columns:
            sums[column] = (
                float(_numeric(member_rows[column]).fillna(0).sum())
                if column in member_rows.columns and not member_rows.empty
                else None
            )
        long_oi = sums["long_open_interest"]
        short_oi = sums["short_open_interest"]
        long_change = sums["long_open_interest_chg"]
        short_change = sums["short_open_interest_chg"]
        output.append(
            {
                "trade_date": iso_date(trade_date),
                "exchange": exchange.upper(),
                "product": product,
                "ranking_scope": "contract" if re.search(r"\d", contract) else "product",
                "contract": contract if re.search(r"\d", contract) else None,
                "source_row_count": source_row_count,
                "published_top_n": published_top_n,
                "member_row_count": int(len(member_rows)),
                "total_row_removed": int(removed_mask.sum()),
                "ranking_reconciled": ranking_reconciled,
                "reported_rows": int(len(member_rows)),
                "reported_top_n": published_top_n,
                "reported_volume": sums["vol"],
                "reported_volume_change": sums["vol_chg"],
                "reported_long_open_interest": long_oi,
                "reported_long_open_interest_change": long_change,
                "reported_short_open_interest": short_oi,
                "reported_short_open_interest_change": short_change,
                "reported_net_long_minus_short": (
                    long_oi - short_oi
                    if long_oi is not None and short_oi is not None
                    else None
                ),
                "reported_net_change": (
                    long_change - short_change
                    if long_change is not None and short_change is not None
                    else None
                ),
                "participant_direction_inferred": False,
                "ranking_use": (
                    "published_top_n_distribution_only"
                    if ranking_reconciled
                    else "invalid_do_not_use"
                ),
            }
        )
    return sorted(
        output,
        key=lambda item: (
            item["exchange"],
            item["product"],
            item["contract"] or "",
        ),
    )


def _first_number(value: Any) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    return float(match.group(0)) if match else None


def _first_date(value: Any) -> str | None:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else None


def normalize_contract_info(
    raw: pd.DataFrame, exchange: str, trade_date: str
) -> list[dict[str, Any]]:
    """Normalize official effective-dated contract metadata where published."""
    if raw.empty:
        return []
    contract_column = _find_column(raw.columns, ("合约代码", "合约"))
    if not contract_column:
        raise ValueError("contract info frame missing contract code")
    output: list[dict[str, Any]] = []
    for _, row in raw.iterrows():
        contract = str(row.get(contract_column, "")).strip().upper()
        if not re.search(r"\d", contract):
            continue
        product = _canonical_product(row.get("产品代码", contract))
        multiplier = _first_number(row.get("交易单位"))
        tick_size = _first_number(
            row.get("最小变动价位", row.get("最小变动单位"))
        )
        tick_value = _first_number(row.get("最小变动价值"))
        if tick_value is None and multiplier is not None and tick_size is not None:
            tick_value = multiplier * tick_size
        margin_percent = _first_number(row.get("交易保证金率"))
        price_limit_percent = _first_number(row.get("涨跌停板"))
        output.append(
            {
                "as_of_date": iso_date(trade_date),
                "exchange": exchange.upper(),
                "product": product,
                "product_name": row.get("产品名称", row.get("品种")),
                "contract": contract,
                "multiplier": multiplier,
                "tick_size": tick_size,
                "tick_value": tick_value,
                "list_date": _first_date(
                    row.get("上市日", row.get("开始交易日", row.get("第一交易日")))
                ),
                "last_trading_day": _first_date(
                    row.get(
                        "最后交易日",
                        row.get(
                            "到期日",
                            row.get("最后交易日待国家公布2025年节假日安排后进行调整"),
                        ),
                    )
                ),
                "last_delivery_day": _first_date(row.get("最后交割日")),
                "margin_rate_percent": margin_percent,
                "price_limit_percent": price_limit_percent,
                "metadata_status": "official_partial",
            }
        )
    return sorted(output, key=lambda item: (item["exchange"], item["contract"]))
