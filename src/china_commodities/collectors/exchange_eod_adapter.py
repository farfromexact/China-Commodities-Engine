"""Whole-exchange EOD downloads with explicit date evidence and raw archives.

These public routes are also used by AKShare. Fetch each daily file once so
new products are not lost to an SDK's hard-coded product-name dictionary.
The GFEX date is an echoed query parameter, not a per-contract timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import requests

from ..storage import write_json_atomic


SHFE_FIELDS = {
    "open": "OPENPRICE", "high": "HIGHESTPRICE", "low": "LOWESTPRICE",
    "close": "CLOSEPRICE", "settle": "SETTLEMENTPRICE",
    "pre_settle": "PRESETTLEMENTPRICE", "volume": "VOLUME",
    "open_interest": "OPENINTEREST", "open_interest_change": "OPENINTERESTCHG",
    "turnover": "TURNOVER", "delta": "DELTA",
}
CZCE_FIELDS = {
    "open": "今开盘", "high": "最高价", "low": "最低价", "close": "今收盘",
    "settle": "今结算", "pre_settle": "昨结算", "volume": "成交量(手)",
    "open_interest": "持仓量", "open_interest_change": "增减量",
    "turnover": "成交额(万元)", "delta": "DELTA", "iv_percent": "隐含波动率",
}
PORTAL_FIELDS = {
    "open": "open", "high": "high", "low": "low", "close": "close",
    "settle": "clearPrice", "pre_settle": "lastClear", "volume": "volumn",
    "open_interest": "openInterest", "open_interest_change": "diffI",
    "turnover": "turnover", "delta": "delta", "iv_percent": "impliedVolatility",
}
INE_PRODUCTS = frozenset({"BC", "EC", "LU", "NR", "SC"})


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def contract_key(value: Any) -> str:
    """Match exchange/iFinD punctuation while retaining exercise-series MS."""
    return str(value or "").upper().split(".", 1)[0].replace("-", "").replace(" ", "")


@dataclass
class DailyDocument:
    exchange: str
    kind: str
    trade_date: str
    rows: list[dict[str, Any]]
    evidence: dict[str, Any]


class ExchangeEODClient:
    def __init__(self, raw_dir: Path, *, timeout: float = 20, transport=None):
        self.raw_dir = Path(raw_dir)
        self.timeout = timeout
        self.transport = transport or requests.request
        self.documents: dict[tuple[str, str, str], DailyDocument] = {}

    def fetch(self, exchange: str, kind: str, trade_date: str) -> DailyDocument:
        day = date.fromisoformat(trade_date)
        if kind not in {"options", "futures"}:
            raise ValueError("kind must be options or futures")
        # The SHFE options file includes the INE options, with product IDs.
        source_exchange = "SHFE" if exchange == "INE" and kind == "options" else exchange
        key = (source_exchange, kind, day.isoformat())
        if key in self.documents:
            document = self.documents[key]
            return DailyDocument(exchange, kind, trade_date, document.rows, document.evidence)
        compact = day.strftime("%Y%m%d")
        kwargs: dict[str, Any] = {}
        method = "GET"
        if source_exchange in {"SHFE", "INE"}:
            host = "www.shfe.com.cn" if source_exchange == "SHFE" else "www.ine.cn"
            segment = "option" if kind == "options" else "future"
            url = f"https://{host}/data/tradedata/{segment}/dailydata/kx{compact}.dat"
        elif source_exchange == "CZCE":
            segment = "Option" if kind == "options" else "Future"
            url = (f"http://www.czce.com.cn/cn/DFSStaticFiles/{segment}/"
                   f"{day.year}/{compact}/{segment}DataDaily.txt")
        elif source_exchange == "GFEX":
            method = "POST"
            url = "http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList"
            kwargs["data"] = {"trade_date": compact, "trade_type": "1" if kind == "options" else "0"}
        elif source_exchange == "DCE":
            method = "POST"
            url = "http://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes"
            kwargs["json"] = {
                "tradeDate": compact, "tradeType": "2" if kind == "options" else "1",
                "varietyId": "all", "contractId": "", "lang": "zh",
                "optionSeries": "", "statisticsType": 0,
            }
        else:
            raise ValueError(f"unsupported exchange: {exchange}")
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        audit_path = self.raw_dir / f"{compact}-{source_exchange}-{kind}.json"
        audit = {"url": url, "request": kwargs, "requested_trade_date": trade_date,
                 "fetched_at": datetime.now(timezone.utc).isoformat()}
        try:
            response = self.transport(method, url, timeout=self.timeout,
                                      headers={"User-Agent": "Mozilla/5.0"}, **kwargs)
            raw = response.content
            digest = hashlib.sha256(raw).hexdigest()
            raw_path = self.raw_dir / f"{compact}-{source_exchange}-{kind}-{digest[:16]}.raw.gz"
            raw_path.write_bytes(gzip.compress(raw, mtime=0))
            audit.update(http_status=response.status_code, sha256=digest,
                         raw_path=raw_path.name, final_url=response.url)
            response.raise_for_status()
            document = parse_document(source_exchange, kind, trade_date, raw, audit)
            audit.update(document.evidence)
            self.documents[key] = document
        except Exception as exc:
            audit["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            write_json_atomic(audit_path, audit)
        return DailyDocument(exchange, kind, trade_date, document.rows, document.evidence)


class ReplayExchangeEODClient:
    """Rebuild from archived bytes, including failures, without network access."""
    def __init__(self, raw_dir: Path):
        self.raw_dir = Path(raw_dir)

    def fetch(self, exchange: str, kind: str, trade_date: str) -> DailyDocument:
        source_exchange = "SHFE" if exchange == "INE" and kind == "options" else exchange
        compact = date.fromisoformat(trade_date).strftime("%Y%m%d")
        audit_path = self.raw_dir / f"{compact}-{source_exchange}-{kind}.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if audit.get("error"):
            raise ValueError(f"archived failure: {audit['error']}")
        name = audit["raw_path"]
        if Path(name).name != name:
            raise ValueError("raw archive path must be a basename")
        raw = gzip.decompress((self.raw_dir / name).read_bytes())
        if hashlib.sha256(raw).hexdigest() != audit["sha256"]:
            raise ValueError("raw archive checksum mismatch")
        document = parse_document(source_exchange, kind, trade_date, raw, audit)
        return DailyDocument(exchange, kind, trade_date, document.rows, document.evidence)


def parse_document(exchange: str, kind: str, trade_date: str,
                   raw: bytes, evidence: dict | None = None) -> DailyDocument:
    """Never infer a source date solely from the date the caller requested."""
    compact = date.fromisoformat(trade_date).strftime("%Y%m%d")
    proof = dict(evidence or {})
    if exchange == "CZCE":
        text = raw.decode("utf-8-sig")
        lines = text.splitlines()
        match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", lines[0] if lines else "")
        if not match or match.group(1) != trade_date:
            raise ValueError("CZCE report title date is absent or stale")
        headers = [item.strip().replace("（", "(").replace("）", ")") for item in lines[1].split("|")]
        rows = []
        for line in lines[2:]:
            cells = [cell.strip() for cell in line.split("|")]
            if not cells or not re.match(r"^[A-Za-z]+\d", cells[0]):
                continue
            if len(cells) != len(headers):
                raise ValueError("CZCE report column count changed")
            rows.append(dict(zip(headers, cells)))
        proof["source_date_basis"] = "report_title"
    else:
        payload = json.loads(raw)
        if exchange in {"SHFE", "INE"}:
            if str(payload.get("report_date", "")) != compact:
                raise ValueError("SHFE/INE report_date is absent or stale")
            rows = payload.get("o_curinstrument")
            proof["source_date_basis"] = "report_date"
        else:
            if str(payload.get("code")) != "0":
                raise ValueError(f"{exchange} returned failure code")
            params = payload.get("param") or {}
            dates = params.get("trade_date", params.get("tradeDate"))
            dates = dates if isinstance(dates, list) else [dates]
            if dates != [compact]:
                raise ValueError(f"{exchange} response has no matching query-date evidence")
            rows = payload.get("data")
            proof["source_date_basis"] = "response_query_date_echo"
        if not isinstance(rows, list):
            raise ValueError(f"{exchange} response has no row list")
    if not rows:
        raise ValueError(f"{exchange} {kind} report is empty")
    proof.update(source_trade_date=trade_date, source_provider="exchange_eod")
    return DailyDocument(exchange, kind, trade_date, rows, proof)


def normalize_document(document: DailyDocument) -> list[dict[str, Any]]:
    exchange, kind = document.exchange, document.kind
    mapping = SHFE_FIELDS if exchange in {"SHFE", "INE"} else CZCE_FIELDS if exchange == "CZCE" else PORTAL_FIELDS
    pattern = (r"^([A-Z]+)(\d{3,4})(?:MS)?([CP])(\d+(?:\.\d+)?)$"
               if kind == "options" else r"^([A-Z]+)(\d{3,4})$")
    output, seen = [], set()
    for row in document.rows:
        if exchange in {"SHFE", "INE"}:
            contract = (row.get("INSTRUMENTID") if kind == "options" else
                        str(row.get("PRODUCTGROUPID") or str(row.get("PRODUCTID", "")).split("_")[0]).strip() + str(row.get("DELIVERYMONTH", "")))
        elif exchange == "CZCE":
            contract = row.get("合约代码", row.get("品种月份", row.get("品种代码")))
        else:
            contract = row.get("contractId") or row.get("delivMonth")
            if kind == "futures" and str(contract).isdigit():
                contract = str(row.get("varietyOrder", "")) + str(contract)
        contract = contract_key(contract)
        match = re.fullmatch(pattern, contract)
        if not match:
            # A malformed concrete contract is not an exchange summary row.
            if re.match(r"^[A-Z]+\d", contract):
                raise ValueError(f"unrecognized {kind} contract: {contract}")
            continue
        product = match.group(1)
        if exchange == "SHFE" and product in INE_PRODUCTS:
            continue
        if exchange == "INE" and product not in INE_PRODUCTS:
            continue
        if contract in seen:
            raise ValueError(f"duplicate exchange contract: {exchange}:{contract}")
        seen.add(contract)
        record = {"trade_date": document.trade_date, "exchange": exchange,
                  "product": product, "contract": contract, **document.evidence}
        record.update({field: number(row.get(source)) for field, source in mapping.items()})
        # The exchange reports turnover in ten-thousand CNY. Preserve original.
        record["source_turnover"] = record.get("turnover")
        record["turnover"] = record["turnover"] * 10000 if record.get("turnover") is not None else None
        record["source_turnover_unit"] = "CNY_10000"
        record["source_date_match"] = True
        # A zero OHLC from a no-trade row is not an observed transaction price.
        if record.get("volume") == 0:
            for field in ("open", "high", "low", "close"):
                if record.get(field) == 0:
                    record[field] = None
        for field in ("volume", "open_interest", "settle", "pre_settle", "iv_percent"):
            if record.get(field) is not None and record[field] < 0:
                raise ValueError(f"negative {field} for {contract}")
        if record.get("iv_percent") == 0:
            record["iv_percent"] = None
        if kind == "options":
            record.update(underlying_contract=match.group(1) + match.group(2),
                          option_type=match.group(3), strike=float(match.group(4)),
                          expiry_date=None, exercise_style="unknown")
        output.append(record)
    if not output:
        raise ValueError(f"no concrete {exchange} {kind} contracts")
    return sorted(output, key=lambda row: row["contract"])
