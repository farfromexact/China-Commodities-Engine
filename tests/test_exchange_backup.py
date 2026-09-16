from __future__ import annotations

import gzip
import json
from pathlib import Path
import tempfile
import unittest

from china_commodities.catalog import OptionProduct, ProductCatalog
from china_commodities.collectors.exchange_eod_adapter import (
    DailyDocument, ReplayExchangeEODClient, normalize_document, parse_document,
)
from china_commodities.exchange_backup import (
    basic_summaries, collect_backup, compare_ifind, enrich_options,
    publish_backup, select_option_products,
)
from china_commodities.option_surface import build_option_surface


DAY = "2026-09-15"


def option(side="C", **values):
    row = {"trade_date": DAY, "source_trade_date": DAY, "source_date_match": True,
           "source_date_basis": "report_date", "source_provider": "exchange_eod",
           "exchange": "SHFE", "product": "CU", "contract": f"CU2611{side}100000",
           "underlying_contract": "CU2611", "strike": 100000.0, "option_type": side,
           "settle": 5000.0, "volume": 10.0, "open_interest": 20.0,
           "underlying_settle": 100000.0, "expiry_date": "2026-10-26",
           "greeks": {"quality": "unavailable", "selected": None}}
    row.update(values)
    return row


def snapshot(rows=None):
    return {"trade_date": DAY, "records": [option(), option("P")] if rows is None else rows,
            "source_provider": "exchange_eod", "futures": [],
            "coverage": {"publish_eligible": True}, "capabilities": {}, "source_statuses": []}


class ExchangeDateAndSchemaTests(unittest.TestCase):
    def test_shfe_rejects_stale_or_missing_report_date(self):
        for day in ("20260914", None):
            with self.assertRaisesRegex(ValueError, "report_date"):
                parse_document("SHFE", "options", DAY, json.dumps({"report_date": day, "o_curinstrument": [{}]}).encode())

    def test_gfex_echo_is_explicit_and_missing_or_wrong_day_fails(self):
        value = {"code": "0", "param": {"trade_date": ["20260915"]}, "data": [{}]}
        doc = parse_document("GFEX", "options", DAY, json.dumps(value).encode())
        self.assertEqual(doc.evidence["source_date_basis"], "response_query_date_echo")
        for params in ({}, {"trade_date": ["20260914"]}):
            value["param"] = params
            with self.assertRaises(ValueError):
                parse_document("GFEX", "options", DAY, json.dumps(value).encode())

    def test_czce_header_date_and_column_shape(self):
        raw = ("郑州商品交易所期权每日行情表(2026-09-15)\n"
               "合约代码|今结算|成交量(手)|持仓量|DELTA|隐含波动率\n"
               "SR611P6000|33|0|2|-0.2|18\n合计|0|0|0|0|0\n").encode()
        rows = normalize_document(parse_document("CZCE", "options", DAY, raw))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delta"], -0.2)
        self.assertEqual(rows[0]["iv_percent"], 18)
        with self.assertRaises(ValueError):
            parse_document("CZCE", "options", DAY, raw.replace(b"2026-09-15", b"2026-09-14"))

    def test_shfe_ine_boundary_and_series_iv_is_not_contract_iv(self):
        raw = {"report_date": "20260915", "o_curinstrument": [
            {"INSTRUMENTID": "cu2611C100000", "SETTLEMENTPRICE": 12},
            {"INSTRUMENTID": "sc2611C500", "SETTLEMENTPRICE": 15}],
            "o_cursigma": [{"INSTRUMENTID": "cu2611", "SIGMA": 0.8}]}
        doc = parse_document("SHFE", "options", DAY, json.dumps(raw).encode())
        self.assertEqual(len(normalize_document(doc)), 1)
        self.assertNotIn("iv_percent", normalize_document(doc)[0])
        doc.exchange = "INE"
        self.assertEqual(normalize_document(doc)[0]["product"], "SC")

    def test_zero_no_trade_and_duplicate_contract(self):
        row = {"delivMonth": "lc2611-C-100000", "clearPrice": 100, "volumn": 0,
               "close": 0, "open": 0, "impliedVolatility": 0, "openInterest": 0}
        doc = DailyDocument("GFEX", "options", DAY, [row], {"source_trade_date": DAY})
        value = normalize_document(doc)[0]
        self.assertIsNone(value["close"])
        self.assertIsNone(value["iv_percent"])
        self.assertEqual(value["volume"], 0)
        doc.rows.append(dict(row))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            normalize_document(doc)

    def test_replay_verifies_checksum_and_preserves_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit = root / "20260915-SHFE-options.json"
            audit.write_text(json.dumps({"raw_path": "payload.gz", "sha256": "wrong"}))
            (root / "payload.gz").write_bytes(gzip.compress(b"{}"))
            with self.assertRaisesRegex(ValueError, "checksum"):
                ReplayExchangeEODClient(root).fetch("SHFE", "options", DAY)
            audit.write_text(json.dumps({"error": "HTTP 412"}))
            with self.assertRaisesRegex(ValueError, "412"):
                ReplayExchangeEODClient(root).fetch("SHFE", "options", DAY)


class BackupContinuityTests(unittest.TestCase):
    def test_primary_outage_falls_back_and_recovery_prefers_primary(self):
        backup = snapshot()
        outage = select_option_products(None, backup, DAY)
        self.assertEqual(outage["decisions"][0]["selected"], "exchange_backup")
        primary = snapshot([option(source_provider="ifind_http"), option("P", source_provider="ifind_http")])
        recovery = select_option_products(primary, backup, DAY)
        self.assertEqual(recovery["decisions"][0]["selected"], "ifind")
        primary["records"][0]["source_trade_date"] = "2026-09-14"
        fallback = select_option_products(primary, backup, DAY)
        self.assertEqual(fallback["decisions"][0]["selected"], "exchange_backup")
        self.assertTrue(all(r["source_provider"] == "exchange_eod" for r in fallback["records"]))

    def test_stale_both_returns_no_fresh_records(self):
        self.assertFalse(select_option_products(snapshot(), snapshot(), "2026-09-16")["records"])

    def test_missing_oi_blocks_pcr_without_becoming_zero(self):
        rows = [option(), option("P", open_interest=None)]
        summary = basic_summaries(rows)[0]
        self.assertIsNone(summary["call_open_interest"])
        self.assertIsNone(summary["put_call_open_interest_ratio"])
        self.assertEqual(summary["put_call_volume_ratio"], 1.0)

    def test_benchmark_includes_missing_contracts_and_does_not_call_availability_accuracy(self):
        backup = snapshot([option(settle=9000)])
        baseline = snapshot()
        result = compare_ifind(backup, baseline)
        self.assertEqual(result["contract_coverage_vs_ifind"], 0.5)
        self.assertEqual(result["core_field_coverage_vs_ifind"], 0.5)
        self.assertEqual(result["fields"]["settle"]["agreement_rtol_1e_4_atol_1e_6"], 0)
        baseline["trade_date"] = "2026-09-14"
        self.assertEqual(compare_ifind(backup, baseline)["status"], "unavailable")

    def test_failed_later_attempt_preserves_last_valid_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publish_backup(snapshot(), root)
            good = (root / "latest.json.gz").read_bytes()
            failed = snapshot([])
            failed["trade_date"] = "2026-09-16"
            failed["coverage"]["publish_eligible"] = False
            report = publish_backup(failed, root)
            self.assertEqual((root / "latest.json.gz").read_bytes(), good)
            self.assertTrue(report["previous_valid_retained"])
            self.assertFalse(report["promoted"])

    def test_surface_never_claims_ifind_for_exchange_data(self):
        surface = build_option_surface(snapshot())
        self.assertEqual(surface["surfaces"][0]["vendor"], "exchange_eod")

    def test_enrichment_uses_independent_settlement_and_requires_named_rate(self):
        rows = [option()]
        futures = [{"exchange": "SHFE", "contract": "CU2611", "settle": 102000,
                    "source_provider": "exchange_eod"}]
        enrich_options(rows, futures, {})
        self.assertEqual(rows[0]["underlying_settle"], 102000)
        self.assertIsNone(rows[0]["expiry_date"])
        self.assertIsNone(rows[0]["greeks"]["model"])
        with self.assertRaises(ValueError):
            enrich_options(rows, futures, {}, risk_free_rate=0.02)

    def test_one_exchange_failure_keeps_others_and_denominator(self):
        catalog = ProductCatalog({}, {}, {"SHFE": ("CU",), "DCE": ("M",)},
                                 (OptionProduct("SHFE", "CU", "铜期权"), OptionProduct("DCE", "M", "豆粕期权")))
        class Client:
            def fetch(self, exchange, kind, trade_date):
                if exchange == "DCE":
                    raise ValueError("HTTP 412")
                row = ({"INSTRUMENTID": "cu2611C100000", "SETTLEMENTPRICE": 100,
                        "VOLUME": 2, "OPENINTEREST": 10} if kind == "options" else
                       {"PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2611", "SETTLEMENTPRICE": 100000})
                return DailyDocument(exchange, kind, DAY, [row], {"source_trade_date": DAY,
                                      "source_date_basis": "report_date", "source_provider": "exchange_eod"})
        result = collect_backup(DAY, Path("unused"), catalog=catalog, client=Client(),
                                metadata_loader=lambda *_: {}, progress=lambda _: None)
        self.assertEqual(result["coverage"]["product_coverage"], 0.5)
        self.assertFalse(result["coverage"]["publish_eligible"])
        self.assertEqual(result["coverage"]["failed_products"], ["DCE:M"])
        self.assertEqual(len(result["records"]), 1)


if __name__ == "__main__":
    unittest.main()
