"""Config-driven iFinD Data Pool adapter for commodity-option EOD chains.

iFinD report identifiers and output column names are account-specific discovery
results from SuperCommand.  They are therefore configuration, not guessed
constants in source code.  The adapter fails closed when required mappings are
missing or the returned trade date is stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import re
from typing import Any, Mapping

from .akshare_adapter import collect_option_daily
from .ifind_http_adapter import IFindHTTPError, IFindHTTPClient
from ..normalize import normalize_options
from ..option_greeks import OptionValuationInput, calculate_greeks


CANONICAL_NUMERIC_FIELDS = (
    "pre_settle",
    "open",
    "high",
    "low",
    "close",
    "settle",
    "volume",
    "turnover",
    "open_interest",
    "open_interest_change",
    "underlying_settle",
    "multiplier",
    "iv_percent",
    "delta",
    "gamma",
    "vega",
    "theta",
    "rho",
)
REQUIRED_MAPPINGS = frozenset({"contract"})
EXCHANGE_SUFFIXES = {
    "SHF": "SHFE",
    "INE": "INE",
    "DCE": "DCE",
    "CZC": "CZCE",
    "GFE": "GFEX",
    "GFEX": "GFEX",
}
IFIND_EXCHANGE_SUFFIX = {
    "SHFE": "SHF",
    "INE": "INE",
    "DCE": "DCE",
    "CZCE": "CZC",
    "GFEX": "GFE",
}
IFIND_OPTION_EOD_INDICATORS = (
    "latest",
    "open",
    "high",
    "low",
    "settlement",
    "preSettlement",
    "volume",
    "amount",
    "openInterest",
    "impliedVolatility",
    "delta",
    "gamma",
    "vega",
    "theta",
    "rho",
)


class IFindOptionDataError(RuntimeError):
    """Raised when an iFinD option report cannot be safely normalized."""


@dataclass(frozen=True)
class IFindOptionReportConfig:
    report_name: str
    function_parameters: dict[str, Any]
    output_parameters: Any
    field_map: dict[str, str]
    exchange: str | None = None
    product: str | None = None
    exercise_style: str | None = None
    risk_free_rate: float | None = None
    risk_free_rate_source: str | None = None
    iv_input_unit: str | None = None
    quote_mode: str = "real_time_enrich"
    quote_batch_size: int = 100
    tree_steps: int = 100
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "IFindOptionReportConfig":
        missing = sorted(REQUIRED_MAPPINGS.difference(value.get("field_map") or {}))
        if missing:
            raise IFindOptionDataError(
                "iFinD option field_map missing: " + ", ".join(missing)
            )
        report_name = str(value.get("report_name") or "").strip()
        if not report_name:
            raise IFindOptionDataError("iFinD option report_name is required")
        style = value.get("exercise_style")
        if style is not None and str(style).lower() not in {"european", "american"}:
            raise IFindOptionDataError(
                "exercise_style must be european, american, or null"
            )
        iv_input_unit = value.get("iv_input_unit")
        if "iv_percent" in value["field_map"] and iv_input_unit not in {
            "percent",
            "decimal",
        }:
            raise IFindOptionDataError(
                "iv_input_unit must be percent or decimal when IV is mapped"
            )
        rate = value.get("risk_free_rate")
        quote_mode = str(value.get("quote_mode", "real_time_enrich"))
        if quote_mode not in {"data_pool_only", "real_time_enrich"}:
            raise IFindOptionDataError(
                "quote_mode must be data_pool_only or real_time_enrich"
            )
        quote_batch_size = int(value.get("quote_batch_size", 100))
        if quote_batch_size < 1:
            raise IFindOptionDataError("quote_batch_size must be positive")
        return cls(
            report_name=report_name,
            function_parameters=dict(value.get("function_parameters") or {}),
            output_parameters=value.get("output_parameters") or {},
            field_map={str(key): str(item) for key, item in value["field_map"].items()},
            exchange=(str(value["exchange"]).upper() if value.get("exchange") else None),
            product=(str(value["product"]).upper() if value.get("product") else None),
            exercise_style=(str(style).lower() if style is not None else None),
            risk_free_rate=float(rate) if rate is not None else None,
            risk_free_rate_source=(
                str(value["risk_free_rate_source"])
                if value.get("risk_free_rate_source")
                else None
            ),
            iv_input_unit=str(iv_input_unit) if iv_input_unit else None,
            quote_mode=quote_mode,
            quote_batch_size=quote_batch_size,
            tree_steps=int(value.get("tree_steps", 100)),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True)
class ExchangeOptionUniverseConfig:
    """One exchange-published option directory to enrich with iFinD quotes."""

    exchange: str
    product: str
    symbol: str
    exercise_style: str | None = None
    risk_free_rate: float | None = None
    risk_free_rate_source: str | None = None
    iv_input_unit: str = "decimal"
    quote_batch_size: int = 100
    tree_steps: int = 100

    def __post_init__(self) -> None:
        if self.exchange.upper() not in IFIND_EXCHANGE_SUFFIX:
            raise IFindOptionDataError(
                f"unsupported commodity-option exchange: {self.exchange}"
            )
        if not self.product.strip() or not self.symbol.strip():
            raise IFindOptionDataError(
                "exchange option universe requires product and symbol"
            )
        if self.exercise_style is not None and self.exercise_style.lower() not in {
            "european",
            "american",
        }:
            raise IFindOptionDataError(
                "exercise_style must be european, american, or null"
            )
        if self.iv_input_unit not in {"decimal", "percent"}:
            raise IFindOptionDataError("iv_input_unit must be decimal or percent")
        if self.quote_batch_size < 1:
            raise IFindOptionDataError("quote_batch_size must be positive")


def _format_trade_date(value: Any, trade_date: str) -> Any:
    if isinstance(value, str):
        return value.replace("{trade_date}", trade_date).replace(
            "{trade_date_compact}", trade_date.replace("-", "")
        )
    if isinstance(value, dict):
        return {key: _format_trade_date(item, trade_date) for key, item in value.items()}
    if isinstance(value, list):
        return [_format_trade_date(item, trade_date) for item in value]
    return value


def _response_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten common Quant API Data Pool response shapes without logging raw data."""
    tables = response.get("tables") or response.get("data") or []
    if isinstance(tables, dict):
        if all(isinstance(value, list) for value in tables.values()):
            tables = [{"table": tables}]
        else:
            tables = [tables]
    rows: list[dict[str, Any]] = []
    for table_wrapper in tables if isinstance(tables, list) else []:
        if not isinstance(table_wrapper, dict):
            continue
        table = table_wrapper.get("table") or table_wrapper.get("data")
        if isinstance(table, list):
            rows.extend(item for item in table if isinstance(item, dict))
            continue
        if not isinstance(table, dict):
            rows.append(table_wrapper)
            continue
        lengths = [len(value) for value in table.values() if isinstance(value, list)]
        row_count = max(lengths or [1])
        for index in range(row_count):
            row: dict[str, Any] = {}
            for key, value in table.items():
                row[key] = (
                    value[index] if isinstance(value, list) and index < len(value) else value
                )
            for key in ("thscode", "code", "time"):
                if key in table_wrapper and key not in row:
                    wrapper_value = table_wrapper[key]
                    row[key] = (
                        wrapper_value[index]
                        if isinstance(wrapper_value, list) and index < len(wrapper_value)
                        else wrapper_value
                    )
            rows.append(row)
    return rows


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[start : start + size] for start in range(0, len(values), size)]


def option_contract_to_ifind_code(contract: str, exchange: str) -> str:
    """Convert a commodity-option exchange contract to its iFinD code."""

    normalized_exchange = exchange.upper()
    suffix = IFIND_EXCHANGE_SUFFIX.get(normalized_exchange)
    if suffix is None:
        raise IFindOptionDataError(
            f"unsupported commodity-option exchange: {exchange}"
        )
    symbol = contract.upper().split(".", 1)[0].replace(" ", "")
    match = re.fullmatch(
        r"(?P<underlying>[A-Z]+\d{3,4})(?P<series>-?MS)?[-]?(?P<type>[CP])[-]?(?P<strike>\d+(?:\.\d+)?)",
        symbol,
    )
    if match is None:
        raise IFindOptionDataError(
            f"exchange option contract could not be converted to iFinD: {contract}"
        )
    strike = match.group("strike")
    if "." in strike:
        strike = strike.rstrip("0").rstrip(".")
    if normalized_exchange in {"DCE", "GFEX"}:
        series = "-MS" if match.group("series") else ""
        return (
            f"{match.group('underlying')}{series}-"
            f"{match.group('type')}-{strike}.{suffix}"
        )
    series = "MS" if match.group("series") else ""
    return (
        f"{match.group('underlying')}{series}{match.group('type')}{strike}.{suffix}"
    )


def _quote_code(row: dict[str, Any]) -> str:
    return str(row.get("thscode") or row.get("code") or "").strip().upper()


def _realtime_quote_map(
    codes: list[str],
    *,
    client: IFindHTTPClient,
    indicators: tuple[str, ...],
    batch_size: int,
) -> dict[str, dict[str, Any]]:
    def query(batch: list[str]) -> list[dict[str, Any]]:
        try:
            response = client.request(
                "real_time_quotation",
                {"codes": ",".join(batch), "indicators": ",".join(indicators)},
            )
            return _response_rows(response)
        except IFindHTTPError as exc:
            parameter_error = "code -4210" in str(exc)
            if not parameter_error:
                raise
            if len(batch) == 1:
                raise IFindOptionDataError(
                    f"iFinD rejected option quote code {batch[0]} with parameter error"
                ) from exc
            midpoint = len(batch) // 2
            return query(batch[:midpoint]) + query(batch[midpoint:])

    rows: list[dict[str, Any]] = []
    for batch in _chunks(codes, batch_size):
        rows.extend(query(batch))
    quotes: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = _quote_code(row)
        if code:
            quotes[code] = row
    return quotes


def _enrich_realtime_quotes(
    universe_rows: list[dict[str, Any]],
    *,
    client: IFindHTTPClient,
    config: IFindOptionReportConfig,
) -> list[dict[str, Any]]:
    contract_field = config.field_map["contract"]
    codes = sorted(
        {
            str(row.get(contract_field) or "").strip().upper()
            for row in universe_rows
            if row.get(contract_field)
        }
    )
    if not codes:
        raise IFindOptionDataError(
            f"iFinD option report {config.report_name} returned no contract codes"
        )
    quotes = _realtime_quote_map(
        codes,
        client=client,
        indicators=IFIND_OPTION_EOD_INDICATORS,
        batch_size=config.quote_batch_size,
    )
    output: list[dict[str, Any]] = []
    for universe in universe_rows:
        code = str(universe.get(contract_field) or "").strip().upper()
        quote = quotes.get(code)
        if quote is None:
            continue
        if not any(quote.get(field) is not None for field in IFIND_OPTION_EOD_INDICATORS):
            continue
        output.append(dict(universe, **quote, _source_endpoint="data_pool+real_time_quotation"))
    if not output:
        raise IFindOptionDataError(
            f"iFinD option quotes were empty for report {config.report_name}"
        )
    return output


def _exchange_quote_config(
    universe: ExchangeOptionUniverseConfig,
) -> IFindOptionReportConfig:
    return IFindOptionReportConfig.from_dict(
        {
            "report_name": (
                f"exchange_directory:{universe.exchange.upper()}:{universe.product.upper()}"
            ),
            "field_map": {
                "contract": "thscode",
                "trade_date": "time",
                "close": "latest",
                "open": "open",
                "high": "high",
                "low": "low",
                "settle": "settlement",
                "pre_settle": "preSettlement",
                "volume": "volume",
                "turnover": "amount",
                "open_interest": "openInterest",
                "iv_percent": "impliedVolatility",
                "delta": "delta",
                "gamma": "gamma",
                "vega": "vega",
                "theta": "theta",
                "rho": "rho",
            },
            "exchange": universe.exchange.upper(),
            "product": universe.product.upper(),
            "exercise_style": universe.exercise_style,
            "risk_free_rate": universe.risk_free_rate,
            "risk_free_rate_source": universe.risk_free_rate_source,
            "iv_input_unit": universe.iv_input_unit,
            "quote_mode": "data_pool_only",
            "quote_batch_size": universe.quote_batch_size,
            "tree_steps": universe.tree_steps,
            "metadata": {
                "universe_source": "exchange_eod_via_akshare",
                "source_symbol": universe.symbol,
            },
        }
    )


def _value(row: dict[str, Any], field_map: dict[str, str], canonical: str) -> Any:
    source = field_map.get(canonical)
    return row.get(source) if source else None


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _iso_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()[:10].replace("/", "-")
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _contract_parts(contract: str) -> tuple[str | None, str | None, float | None]:
    symbol = contract.upper().split(".", 1)[0].replace(" ", "")
    match = re.match(
        r"^(?P<underlying>[A-Z]+\d{3,4})(?:-?MS)?[-]? (?P<type>[CP])[-]? (?P<strike>\d+(?:\.\d+)?)$".replace(
            " ", ""
        ),
        symbol,
    )
    if not match:
        return None, None, None
    return (
        match.group("underlying"),
        match.group("type"),
        float(match.group("strike")),
    )


def _exchange_from_contract(contract: str) -> str | None:
    if "." not in contract:
        return None
    return EXCHANGE_SUFFIXES.get(contract.rsplit(".", 1)[-1].upper())


def _vendor_greeks(record: dict[str, Any]) -> dict[str, float]:
    output: dict[str, float] = {}
    for key in ("iv_percent", "delta", "gamma", "vega", "theta", "rho"):
        value = record.get(key)
        if isinstance(value, (int, float)):
            output[key] = float(value)
    return output


def _model_greeks(
    record: dict[str, Any],
    config: IFindOptionReportConfig,
    trade_date: str,
) -> dict[str, Any] | None:
    if config.exercise_style not in {"european", "american"}:
        return None
    if config.risk_free_rate is None:
        return None
    forward = record.get("underlying_settle")
    strike = record.get("strike")
    expiry = record.get("expiry_date")
    if not isinstance(forward, (int, float)) or not isinstance(strike, (int, float)):
        return None
    if not expiry:
        return None
    days = (date.fromisoformat(expiry) - date.fromisoformat(trade_date)).days
    if days <= 0:
        return None
    market_price = record.get("settle") or record.get("close")
    valuation = OptionValuationInput(
        forward=float(forward),
        strike=float(strike),
        time_to_expiry_years=days / 365.0,
        rate=config.risk_free_rate,
        option_type=record["option_type"],
        exercise_style=config.exercise_style,
        market_price=float(market_price) if isinstance(market_price, (int, float)) else None,
        iv_percent=record.get("iv_percent"),
    )
    result = calculate_greeks(valuation, tree_steps=config.tree_steps)
    return result.to_dict() if result else None


def normalize_ifind_option_rows(
    rows: list[dict[str, Any]],
    *,
    trade_date: str,
    config: IFindOptionReportConfig,
) -> list[dict[str, Any]]:
    requested_date = date.fromisoformat(trade_date).isoformat()
    output: list[dict[str, Any]] = []
    for row in rows:
        raw_contract = _value(row, config.field_map, "contract")
        contract = str(raw_contract or "").strip().upper()
        if not contract:
            continue
        underlying, option_type, strike = _contract_parts(contract)
        underlying = str(
            _value(row, config.field_map, "underlying_contract") or underlying or ""
        ).strip().upper()
        option_type = str(
            _value(row, config.field_map, "option_type") or option_type or ""
        ).strip().upper()
        strike = _number(_value(row, config.field_map, "strike")) or strike
        source_date = _iso_date(_value(row, config.field_map, "trade_date"))
        if source_date is not None and source_date != requested_date:
            raise IFindOptionDataError(
                f"iFinD option report returned stale date {source_date}; requested {requested_date}"
            )
        exchange = str(
            _value(row, config.field_map, "exchange")
            or config.exchange
            or _exchange_from_contract(contract)
            or ""
        ).upper()
        product = str(
            _value(row, config.field_map, "product")
            or config.product
            or re.match(r"[A-Z]+", underlying or contract).group(0)  # type: ignore[union-attr]
        ).upper()
        if not underlying or option_type not in {"C", "P"} or not strike:
            raise IFindOptionDataError(
                f"iFinD option contract could not be parsed safely: {contract}"
            )
        record: dict[str, Any] = {
            "trade_date": requested_date,
            "exchange": exchange,
            "product": product,
            "contract": contract.split(".", 1)[0],
            "ifind_code": contract,
            "underlying_contract": underlying.split(".", 1)[0],
            "expiry_date": _iso_date(_value(row, config.field_map, "expiry_date")),
            "option_type": option_type,
            "strike": float(strike),
            "exercise_style": config.exercise_style or "unknown",
            "source_provider": "ifind_http",
            "source_endpoint": row.get("_source_endpoint", "data_pool"),
            "source_report": config.report_name,
            "source_trade_date": source_date,
            "source_date_match": source_date == requested_date if source_date else None,
            "source_date_status": (
                "source_date_verified"
                if source_date
                else "requested_date_stamped_after_reference_futures_gate"
            ),
        }
        for canonical in CANONICAL_NUMERIC_FIELDS:
            record[canonical] = _number(_value(row, config.field_map, canonical))
        if record.get("iv_percent") is not None and config.iv_input_unit == "decimal":
            record["iv_percent"] = float(record["iv_percent"]) * 100.0
        vendor = _vendor_greeks(record)
        model = _model_greeks(record, config, requested_date)
        if model is not None:
            selected = model
            selected_source = "model"
            quality = "vendor_and_model" if vendor else "model_derived"
        elif vendor:
            selected = vendor
            selected_source = "vendor"
            quality = "vendor_reported"
        else:
            selected = None
            selected_source = None
            quality = "unavailable"
        record["greeks"] = {
            "quality": quality,
            "selected_source": selected_source,
            "selected": selected,
            "vendor": vendor or None,
            "model": model,
            "model_assumptions": {
                "exercise_style": config.exercise_style,
                "risk_free_rate": config.risk_free_rate,
                "risk_free_rate_source": config.risk_free_rate_source,
                "day_count": "actual_365",
            },
            "vendor_units": "as_reported_by_ifind_except_iv_normalized_to_percent",
            "dealer_position_direction_known": False,
        }
        output.append(record)
    output.sort(key=lambda item: (item["exchange"], item["product"], item["contract"]))
    return output


def collect_option_eod_from_exchange_universe(
    trade_date: str,
    *,
    client: IFindHTTPClient,
    universes: list[ExchangeOptionUniverseConfig],
    ak_module: Any | None = None,
    directory_records_by_product: Mapping[
        tuple[str, str], list[dict[str, Any]]
    ] | None = None,
) -> dict[str, Any]:
    """Use exchange EOD directories for codes and iFinD for quotes and Greeks."""

    requested_date = date.fromisoformat(trade_date).isoformat()
    if not universes:
        raise IFindOptionDataError(
            "at least one exchange option universe is required"
        )
    records: list[dict[str, Any]] = []
    universe_contract_count = 0
    universe_sources: set[str] = set()
    for universe in universes:
        exchange = universe.exchange.upper()
        product = universe.product.upper()
        directory_key = (exchange, product)
        if directory_records_by_product is not None and directory_key in directory_records_by_product:
            directory_records = [
                dict(record) for record in directory_records_by_product[directory_key]
            ]
            source_values = {
                str(record.get("universe_source") or "").strip()
                for record in directory_records
            }
            if len(source_values) != 1 or not next(iter(source_values), ""):
                raise IFindOptionDataError(
                    f"provided option directory has ambiguous source for {exchange}:{product}"
                )
            universe_source = next(iter(source_values))
        else:
            raw = collect_option_daily(
                requested_date,
                exchange,
                universe.symbol,
                ak_module=ak_module,
            )
            directory_records = normalize_options(
                raw,
                exchange,
                product,
                universe.symbol,
                requested_date,
            )
            universe_source = "exchange_eod_via_akshare"
        if not directory_records:
            raise IFindOptionDataError(
                f"exchange option directory was empty for {exchange}:{product}"
            )
        directory_by_code: dict[str, dict[str, Any]] = {}
        for record in directory_records:
            code = option_contract_to_ifind_code(record["contract"], exchange)
            if code in directory_by_code:
                raise IFindOptionDataError(
                    f"exchange option directory returned duplicate contract: {code}"
                )
            directory_by_code[code] = record
        codes = sorted(directory_by_code)
        quotes = _realtime_quote_map(
            codes,
            client=client,
            indicators=IFIND_OPTION_EOD_INDICATORS,
            batch_size=universe.quote_batch_size,
        )
        missing = [
            code
            for code in codes
            if code not in quotes
            or not any(
                quotes[code].get(field) is not None
                for field in IFIND_OPTION_EOD_INDICATORS
            )
        ]
        if missing:
            sample = ", ".join(missing[:5])
            raise IFindOptionDataError(
                f"iFinD option quote coverage incomplete for {exchange}:{product}; "
                f"missing {len(missing)} of {len(codes)} contracts; sample: {sample}"
            )
        quote_rows: list[dict[str, Any]] = []
        for code in codes:
            quote = quotes[code]
            source_date = _iso_date(quote.get("time"))
            if source_date != requested_date:
                raise IFindOptionDataError(
                    f"iFinD option quote {code} has source date "
                    f"{source_date or 'missing'}; requested {requested_date}"
                )
            quote_rows.append(
                dict(
                    quote,
                    _source_endpoint="exchange_directory+real_time_quotation",
                    _universe_source="exchange_eod_via_akshare",
                )
            )
        config = _exchange_quote_config(universe)
        normalized = normalize_ifind_option_rows(
            quote_rows,
            trade_date=requested_date,
            config=config,
        )
        if len(normalized) != len(directory_records):
            raise IFindOptionDataError(
                f"normalized iFinD option coverage changed for {exchange}:{product}; "
                f"expected {len(directory_records)}, got {len(normalized)}"
            )

        suffix = IFIND_EXCHANGE_SUFFIX[exchange]
        underlying_codes = sorted(
            {
                f"{record['underlying_contract']}.{suffix}"
                for record in normalized
            }
        )
        underlying_quotes = _realtime_quote_map(
            underlying_codes,
            client=client,
            indicators=("settlement", "latest"),
            batch_size=universe.quote_batch_size,
        )
        missing_underlyings = [
            code for code in underlying_codes if code not in underlying_quotes
        ]
        if missing_underlyings:
            raise IFindOptionDataError(
                f"iFinD underlying settlement coverage incomplete for "
                f"{exchange}:{product}; missing {', '.join(missing_underlyings[:5])}"
            )
        underlying_settles: dict[str, float] = {}
        for code in underlying_codes:
            quote = underlying_quotes[code]
            source_date = _iso_date(quote.get("time"))
            if source_date != requested_date:
                raise IFindOptionDataError(
                    f"iFinD underlying quote {code} has source date "
                    f"{source_date or 'missing'}; requested {requested_date}"
                )
            settlement = _number(quote.get("settlement"))
            if settlement is None:
                settlement = _number(quote.get("latest"))
            if settlement is None or settlement <= 0:
                raise IFindOptionDataError(
                    f"iFinD underlying quote {code} has no positive settlement"
                )
            underlying_settles[code.split(".", 1)[0]] = settlement
        for record in normalized:
            directory_record = directory_by_code[record["ifind_code"]]
            record["underlying_settle"] = underlying_settles[
                record["underlying_contract"]
            ]
            record["expiry_date"] = directory_record.get("expiry_date")
            directory_style = str(
                directory_record.get("exercise_style") or ""
            ).lower()
            if directory_style in {"american", "european"}:
                record["exercise_style"] = directory_style
                record["exercise_style_source"] = "exchange_directory"
            else:
                record["exercise_style_source"] = "official_rule_registry"
            record["universe_source"] = universe_source
            record["universe_source_provider"] = directory_record.get(
                "universe_source_provider",
                "exchange_via_akshare",
            )
            record["universe_source_date"] = directory_record.get(
                "universe_source_date",
                requested_date,
            )
            record["universe_symbol"] = universe.symbol
            record["universe_trade_date"] = requested_date
        universe_contract_count += len(directory_records)
        records.extend(normalized)
        universe_sources.add(universe_source)

    contracts = [record["contract"] for record in records]
    if len(contracts) != len(set(contracts)):
        raise IFindOptionDataError(
            "exchange option universes returned duplicate normalized contracts"
        )
    return {
        "schema_version": 1,
        "trade_date": requested_date,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_provider": "ifind_http",
        "universe_source": (
            next(iter(universe_sources))
            if len(universe_sources) == 1
            else "mixed_contract_directories"
        ),
        "universe_contract_count": universe_contract_count,
        "quote_contract_count": len(records),
        "quote_coverage_complete": len(records) == universe_contract_count,
        "collection_mode": "end_of_day",
        "intraday": False,
        "records": records,
    }


def collect_option_eod_snapshot(
    trade_date: str,
    *,
    client: IFindHTTPClient,
    reports: list[IFindOptionReportConfig],
) -> dict[str, Any]:
    """Collect one full EOD chain from explicitly verified iFinD report configs."""
    requested_date = date.fromisoformat(trade_date).isoformat()
    if not reports:
        raise IFindOptionDataError("at least one verified iFinD option report is required")
    records: list[dict[str, Any]] = []
    for config in reports:
        payload = {
            "reportname": config.report_name,
            "functionpara": _format_trade_date(
                config.function_parameters, requested_date
            ),
            "outputpara": _format_trade_date(config.output_parameters, requested_date),
        }
        response = client.request("data_pool", payload)
        rows = _response_rows(response)
        if config.quote_mode == "real_time_enrich":
            rows = _enrich_realtime_quotes(rows, client=client, config=config)
        records.extend(
            normalize_ifind_option_rows(
                rows,
                trade_date=requested_date,
                config=config,
            )
        )
    if not records:
        raise IFindOptionDataError(
            "iFinD option reports returned no contracts; nothing was published"
        )
    contracts = [record["contract"] for record in records]
    if len(contracts) != len(set(contracts)):
        raise IFindOptionDataError("iFinD option reports returned duplicate contracts")
    return {
        "schema_version": 1,
        "trade_date": requested_date,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_provider": "ifind_http",
        "collection_mode": "end_of_day",
        "intraday": False,
        "records": records,
    }
