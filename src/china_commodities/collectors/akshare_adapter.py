"""Thin, injectable routing helpers for AKShare commodity data."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import StringIO
import json
import re
import time
from typing import Any

import pandas as pd
import requests


COMMODITY_EXCHANGES: tuple[str, ...] = ("SHFE", "INE", "DCE", "CZCE", "GFEX")

_WAREHOUSE_FUNCTIONS: dict[str, str] = {
    "DCE": "futures_warehouse_receipt_dce",
    "CZCE": "futures_warehouse_receipt_czce",
    "SHFE": "futures_shfe_warehouse_receipt",
    "INE": "futures_shfe_warehouse_receipt",
    "GFEX": "futures_gfex_warehouse_receipt",
}

_OPTION_FUNCTIONS: dict[str, str] = {
    "DCE": "option_hist_dce",
    "CZCE": "option_hist_czce",
    "SHFE": "option_hist_shfe",
    "INE": "option_hist_shfe",
    "GFEX": "option_hist_gfex",
}

_DCE_OPTION_PRODUCT_CODES: dict[str, str] = {
    # AKShare 1.18.64 predates the July 2025 pure-benzene option mapping even
    # though the exchange endpoint already accepts the product code.
    "纯苯期权": "bz",
}

_MEMBER_RANKING_FUNCTIONS: dict[str, str] = {
    "SHFE": "get_shfe_rank_table",
    "INE": "get_shfe_rank_table",
    "DCE": "get_dce_rank_table",
    "CZCE": "get_rank_table_czce",
    "GFEX": "futures_gfex_position_rank",
}

_CONTRACT_INFO_FUNCTIONS: dict[str, str] = {
    "SHFE": "futures_contract_info_shfe",
    "INE": "futures_contract_info_ine",
    "DCE": "futures_contract_info_dce",
    "CZCE": "futures_contract_info_czce",
    "GFEX": "futures_contract_info_gfex",
}

_DCE_CONTRACT_INFO_URL = (
    "http://www.dce.com.cn/dcereport/publicweb/tradepara/contractInfo"
)
_DCE_CONTRACT_INFO_PAYLOAD = {
    "lang": "zh",
    "tradeType": "1",
    "varietyId": "all",
}
_DCE_CONTRACT_INFO_LEGACY_URL = (
    "http://portal.dce.com.cn/publicweb/businessguidelines/"
    "queryContractInfo.html"
)
_DCE_CONTRACT_INFO_LEGACY_PARAMS = {
    "contractInformation.variety": "all",
    "contractInformation.trade_type": "0",
}
_DCE_CONTRACT_INFO_ATTEMPTS = 2
_DCE_CONTRACT_INFO_RETRY_DELAY_SECONDS = 1.0


def _prepare_dce_contract_info_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize columns and dates shared by the DCE official routes."""

    frame = frame.copy()
    frame.rename(
        columns={
            "contractId": "合约",
            "variety": "品种名称",
            "varietyOrder": "品种代码",
            "unit": "交易单位",
            "tick": "最小变动价位",
            "startTradeDate": "开始交易日",
            "endTradeDate": "最后交易日",
            "endDeliveryDate": "最后交割日",
        },
        inplace=True,
    )
    for column in ("交易单位", "最小变动价位"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("开始交易日", "最后交易日", "最后交割日"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(
                frame[column], format="mixed", errors="coerce"
            ).dt.date
    return frame


def _load_akshare(ak_module: Any | None) -> Any:
    """Return an injected AKShare-compatible module or import AKShare."""

    if ak_module is not None:
        return ak_module

    import akshare

    return akshare


def _normalize_trade_date(trade_date: str) -> str:
    """Normalize an ISO or compact date to the ``YYYYMMDD`` form."""

    if not isinstance(trade_date, str):
        raise TypeError("trade_date must be a string")

    if len(trade_date) == 10 and trade_date[4] == "-" and trade_date[7] == "-":
        date_format = "%Y-%m-%d"
    elif len(trade_date) == 8 and trade_date.isdigit():
        date_format = "%Y%m%d"
    else:
        raise ValueError("trade_date must use YYYY-MM-DD or YYYYMMDD")

    try:
        parsed = datetime.strptime(trade_date, date_format)
    except ValueError as exc:
        raise ValueError("trade_date must be a valid calendar date") from exc

    return parsed.strftime("%Y%m%d")


def _validate_exchange(exchange: str) -> str:
    """Validate and return a supported commodity exchange code."""

    if exchange not in COMMODITY_EXCHANGES:
        supported = ", ".join(COMMODITY_EXCHANGES)
        raise ValueError(f"unsupported exchange {exchange!r}; expected one of {supported}")
    return exchange


def collect_futures_daily(
    trade_date: str,
    exchange: str,
    ak_module: Any | None = None,
) -> pd.DataFrame:
    """Collect one exchange's futures daily data for a single trading date."""

    date = _normalize_trade_date(trade_date)
    exchange = _validate_exchange(exchange)
    ak = _load_akshare(ak_module)
    return ak.get_futures_daily(start_date=date, end_date=date, market=exchange)


def collect_dce_realtime_fallback(
    trade_date: str,
    ak_module: Any | None = None,
    max_workers: int = 6,
) -> pd.DataFrame:
    """Collect current DCE concrete contracts from Sina when official EOD is blocked.

    The fallback is accepted only when every returned row reports the requested
    trade date. It intentionally leaves settlement and turnover unavailable
    rather than inventing official EOD values.
    """

    requested = _normalize_trade_date(trade_date)
    ak = _load_akshare(ak_module)
    marks = ak.futures_symbol_mark()
    required_columns = {"exchange", "symbol"}
    if not required_columns.issubset(marks.columns):
        raise ValueError("futures_symbol_mark response is missing exchange/symbol")
    names = (
        marks.loc[marks["exchange"].eq("大连商品交易所"), "symbol"]
        .dropna()
        .astype(str)
        .drop_duplicates()
        .tolist()
    )
    if not names:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(ak.futures_zh_realtime, symbol=name): name for name in names}
        for future in as_completed(futures):
            name = futures[future]
            try:
                frame = future.result()
                if isinstance(frame, pd.DataFrame) and not frame.empty:
                    frames.append(frame)
            except Exception as exc:  # keep one product failure visible in attrs
                failures.append(f"{name}:{type(exc).__name__}")
    if not frames:
        return pd.DataFrame()

    raw = pd.concat(frames, ignore_index=True)
    needed = {"symbol", "exchange", "trade", "open", "high", "low", "volume", "position", "tradedate"}
    missing = sorted(needed.difference(raw.columns))
    if missing:
        raise ValueError(f"DCE fallback response missing columns: {', '.join(missing)}")
    raw["tradedate"] = pd.to_datetime(raw["tradedate"], errors="coerce").dt.strftime("%Y%m%d")
    symbols = raw["symbol"].astype(str)
    raw = raw[
        raw["exchange"].astype(str).str.lower().eq("dce")
        & raw["tradedate"].eq(requested)
        & symbols.str.fullmatch(r"[A-Za-z]+\d{3,4}")
    ].copy()
    if raw.empty:
        return pd.DataFrame()

    pre_settle = raw.get("prevsettlement", raw.get("presettlement"))
    settle = pd.to_numeric(raw.get("settlement"), errors="coerce")
    settle = settle.where(settle.gt(0))
    output = pd.DataFrame(
        {
            "symbol": raw["symbol"].astype(str).str.upper(),
            "date": requested,
            "open": raw["open"],
            "high": raw["high"],
            "low": raw["low"],
            "close": raw["trade"],
            "volume": raw["volume"],
            "open_interest": raw["position"],
            "turnover": None,
            "settle": settle,
            "pre_settle": pre_settle,
            "variety": raw["symbol"].astype(str).map(
                lambda value: re.match(r"[A-Za-z]+", value).group(0).upper()
            ),
        }
    )
    output.drop_duplicates(subset=["symbol"], keep="first", inplace=True)
    output.attrs["requested_products"] = names
    output.attrs["observed_products"] = sorted(output["variety"].dropna().unique().tolist())
    output.attrs["product_coverage"] = len(output.attrs["observed_products"]) / len(names)
    output.attrs["failed_products"] = sorted(failures)
    output.attrs["source"] = "Sina futures_zh_realtime via AKShare"
    return output.reset_index(drop=True)


def collect_warehouse_receipt(
    trade_date: str,
    exchange: str,
    ak_module: Any | None = None,
) -> Any:
    """Collect a warehouse-receipt report using the exchange-specific route."""

    date = _normalize_trade_date(trade_date)
    exchange = _validate_exchange(exchange)
    function_name = _WAREHOUSE_FUNCTIONS[exchange]
    function = getattr(_load_akshare(ak_module), function_name)
    return function(date=date)


def collect_basis_daily(
    trade_date: str,
    products: Sequence[str] | None = None,
    ak_module: Any | None = None,
) -> pd.DataFrame:
    """Collect daily spot/basis data, preserving AKShare's default product scope."""

    date = _normalize_trade_date(trade_date)
    function = _load_akshare(ak_module).futures_spot_price
    if products is None:
        return function(date=date)
    return function(date=date, vars_list=list(products))


def collect_foreign_futures_history(
    symbol: str,
    ak_module: Any | None = None,
) -> pd.DataFrame:
    """Collect a public foreign-futures daily history from AKShare.

    ``futures_foreign_hist`` is backed by Sina Finance in AKShare.  Keep the
    source-specific symbol explicit at the caller so it cannot be confused
    with a domestic exchange code or a synthetic import-parity leg.
    """

    normalized_symbol = str(symbol or "").strip()
    if not normalized_symbol:
        raise ValueError("foreign futures symbol must not be empty")
    return _load_akshare(ak_module).futures_foreign_hist(symbol=normalized_symbol)


def collect_option_daily(
    trade_date: str,
    exchange: str,
    symbol: str,
    ak_module: Any | None = None,
) -> pd.DataFrame:
    """Collect one option symbol using the exchange-specific history route."""

    date = _normalize_trade_date(trade_date)
    exchange = _validate_exchange(exchange)
    if exchange == "DCE" and symbol in _DCE_OPTION_PRODUCT_CODES:
        response = requests.post(
            "https://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes",
            json={
                "contractId": "",
                "lang": "zh",
                "optionSeries": "",
                "statisticsType": 0,
                "tradeDate": date,
                "tradeType": "2",
                "varietyId": _DCE_OPTION_PRODUCT_CODES[symbol],
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        frame = pd.DataFrame(payload.get("data") or [])
        if frame.empty:
            return frame
        frame.rename(
            columns={
                "variety": "品种名称",
                "contractId": "合约",
                "open": "开盘价",
                "high": "最高价",
                "low": "最低价",
                "close": "收盘价",
                "lastClear": "前结算价",
                "clearPrice": "结算价",
                "diff": "涨跌",
                "diff1": "涨跌1",
                "delta": "Delta",
                "impliedVolatility": "隐含波动率(%)",
                "volumn": "成交量",
                "openInterest": "持仓量",
                "diffI": "持仓量变化",
                "turnover": "成交额",
                "matchQtySum": "行权量",
            },
            inplace=True,
        )
        columns = [
            "品种名称", "合约", "开盘价", "最高价", "最低价", "收盘价",
            "前结算价", "结算价", "涨跌", "涨跌1", "Delta",
            "隐含波动率(%)", "成交量", "持仓量", "持仓量变化", "成交额", "行权量",
        ]
        frame = frame.reindex(columns=columns)
        for column in columns[2:]:
            frame[column] = pd.to_numeric(
                frame[column].astype(str).str.replace(",", "", regex=False),
                errors="coerce",
            )
        return frame
    function_name = _OPTION_FUNCTIONS[exchange]
    function = getattr(_load_akshare(ak_module), function_name)
    return function(symbol=symbol, trade_date=date)


def collect_option_volatility_daily(
    trade_date: str,
    exchange: str,
    symbol: str,
    ak_module: Any | None = None,
) -> pd.DataFrame:
    """Collect exchange-published option-series IV for SHFE/INE products."""

    date = _normalize_trade_date(trade_date)
    exchange = _validate_exchange(exchange)
    if exchange not in {"SHFE", "INE"}:
        raise ValueError("option series volatility is currently routed only for SHFE/INE")
    return _load_akshare(ak_module).option_vol_shfe(symbol=symbol, trade_date=date)


def collect_member_rankings(
    trade_date: str,
    exchange: str,
    products: Sequence[str] | None = None,
    ak_module: Any | None = None,
) -> Any:
    """Collect published member ranking tables without inferring client direction."""

    date = _normalize_trade_date(trade_date)
    exchange = _validate_exchange(exchange)
    function = getattr(_load_akshare(ak_module), _MEMBER_RANKING_FUNCTIONS[exchange])
    if exchange == "CZCE":
        return function(date=date)
    if products is None:
        return function(date=date)
    return function(date=date, vars_list=list(products))


def _collect_dce_contract_info_direct() -> pd.DataFrame:
    """Read DCE's current contract table from its current official route.

    The current DCE route returns JSON.  A legacy portal HTML route is kept as
    a narrow fallback because DCE has changed this endpoint more than once.
    The AKShare function remains a final compatibility fallback in the public
    adapter below.
    """

    last_error: Exception | None = None
    for attempt in range(_DCE_CONTRACT_INFO_ATTEMPTS):
        try:
            response = requests.post(
                _DCE_CONTRACT_INFO_URL,
                json=_DCE_CONTRACT_INFO_PAYLOAD,
                timeout=30,
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                rows = payload.get("data")
                if isinstance(rows, dict):
                    rows = rows.get("entityList")
                if not isinstance(rows, list) or not rows:
                    raise ValueError(
                        "DCE contract info endpoint returned no JSON records"
                    )
                return _prepare_dce_contract_info_frame(pd.DataFrame(rows))

            body = str(getattr(response, "text", "") or "")
            if not body.strip():
                raise ValueError("DCE contract info endpoint returned an empty body")
            tables = pd.read_html(StringIO(body))
            if not tables:
                raise ValueError(
                    "DCE contract info endpoint returned no HTML table"
                )
            return _prepare_dce_contract_info_frame(tables[0])
        except (json.JSONDecodeError, requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt + 1 < _DCE_CONTRACT_INFO_ATTEMPTS:
                time.sleep(_DCE_CONTRACT_INFO_RETRY_DELAY_SECONDS)

    legacy_error: Exception | None = None
    try:
        response = requests.post(
            _DCE_CONTRACT_INFO_LEGACY_URL,
            params=_DCE_CONTRACT_INFO_LEGACY_PARAMS,
            timeout=30,
        )
        response.raise_for_status()
        body = str(getattr(response, "text", "") or "")
        if not body.strip():
            raise ValueError("DCE legacy contract info endpoint returned an empty body")
        tables = pd.read_html(StringIO(body))
        if not tables:
            raise ValueError("DCE legacy contract info endpoint returned no HTML table")
        return _prepare_dce_contract_info_frame(tables[0])
    except (json.JSONDecodeError, requests.RequestException, ValueError) as exc:
        legacy_error = exc

    assert last_error is not None
    raise RuntimeError(
        "DCE contract info official routes failed; current route after "
        f"{_DCE_CONTRACT_INFO_ATTEMPTS} attempts: "
        f"{type(last_error).__name__}: {last_error}; legacy route: "
        f"{type(legacy_error).__name__}: {legacy_error}"
    ) from legacy_error


def collect_contract_info(
    trade_date: str,
    exchange: str,
    ak_module: Any | None = None,
) -> pd.DataFrame:
    """Collect exchange contract metadata with the appropriate date semantics."""

    date = _normalize_trade_date(trade_date)
    exchange = _validate_exchange(exchange)
    if exchange == "DCE" and ak_module is None:
        direct_error: Exception | None = None
        try:
            return _collect_dce_contract_info_direct()
        except Exception as exc:
            direct_error = exc
        try:
            function = getattr(_load_akshare(ak_module), _CONTRACT_INFO_FUNCTIONS[exchange])
            return function()
        except Exception as fallback_error:
            raise RuntimeError(
                "DCE contract info failed through both the current official "
                f"portal and the AKShare compatibility route; portal: "
                f"{type(direct_error).__name__}: {direct_error}; AKShare: "
                f"{type(fallback_error).__name__}: {fallback_error}"
            ) from fallback_error
    function = getattr(_load_akshare(ak_module), _CONTRACT_INFO_FUNCTIONS[exchange])
    if exchange in {"DCE", "GFEX"}:
        return function()
    return function(date=date)


def akshare_version(ak_module: Any | None = None) -> str:
    """Return the injected or installed AKShare version string."""

    version = getattr(_load_akshare(ak_module), "__version__", None)
    return "unknown" if version is None else str(version)


__all__ = [
    "COMMODITY_EXCHANGES",
    "akshare_version",
    "collect_basis_daily",
    "collect_contract_info",
    "collect_dce_realtime_fallback",
    "collect_foreign_futures_history",
    "collect_futures_daily",
    "collect_member_rankings",
    "collect_option_daily",
    "collect_option_volatility_daily",
    "collect_warehouse_receipt",
]
