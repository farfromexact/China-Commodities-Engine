from __future__ import annotations

import tempfile
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from china_commodities.catalog import OptionProduct
from china_commodities.collectors.ifind_option_adapter import ExchangeOptionUniverseConfig
from china_commodities.option_core import CORE_PROFILE, CoreOptionCollector, select_core_contracts
from china_commodities.option_batch import collect_option_market_snapshot
from china_commodities.option_storage import publish_option_eod, read_option_latest, validate_option_snapshot, OptionSnapshotValidationError
from china_commodities.option_surface import build_option_surface
from china_commodities.reporting import build_report_input, reconcile_main_status
from china_commodities.storage import read_json
from china_commodities.storage import write_json_if_changed

DAY = "2026-10-14"
UNIVERSE = ExchangeOptionUniverseConfig("SHFE", "CU", "copper", exercise_style="american")


def directory():
    return [{"contract": f"{underlying}{side}{strike}", "exchange": "SHFE", "product": "CU",
             "underlying_contract": underlying, "option_type": side, "strike": strike,
             "open_interest": activity, "expiry_date": "2026-11-25",
             "universe_source": "test_directory"}
            for underlying, activity in [("CU2612", 100), ("CU2701", 20)]
            for strike in range(50000, 100000, 500) for side in ["C", "P"]]


def futures():
    return {"trade_date": DAY, "futures_contracts": [
        {"exchange": "SHFE", "contract": u, "settle": 80000,
         "source_trade_date": DAY, "source_date_match": True, "open_interest": 100}
        for u in ["CU2612", "CU2701"]]}


class Client:
    def __init__(self, stale=False, missing=False):
        self.calls = []
        self.stale = stale
        self.missing = missing

    def request(self, endpoint, payload):
        self.calls.append(payload)
        codes = payload["codes"].split(",")
        if self.missing:
            codes = codes[:1]
        return {"tables": [
            {"thscode": code, "time": "2026-10-15 01:00:00" if self.stale and i == 0 else DAY,
             "table": {"settlement": [100], "volume": [10]}}
            for i, code in enumerate(codes)]}


def collect(client, previous=None):
    collector = CoreOptionCollector(futures_snapshot=futures(), previous_snapshot=previous)
    result = collector(DAY, client=client, universes=[UNIVERSE],
                       directory_records_by_product={("SHFE", "CU"): directory()})
    result["collection_plans"] = list(collector.plans.values())
    return result


class CoreTests(unittest.TestCase):
    def test_selection_caps_before_request_and_keeps_atm_pairs(self):
        rows = directory()
        selected, plan = select_core_contracts(rows, {r["contract"]: r for r in futures()["futures_contracts"]}, [], DAY)
        self.assertEqual(len(selected), 30)
        self.assertLessEqual(len(selected), len(rows)*.1)
        self.assertEqual({r["underlying_contract"] for r in selected}, {"CU2612"})
        self.assertIn(80000, {r["strike"] for r in selected})
        self.assertEqual(plan["selection_forward_date"], DAY)
        client = Client()
        collect(client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(client.calls[0]["codes"].split(",")), 30)
        self.assertEqual(len(client.calls[0]["indicators"].split(",")), 10)
        self.assertNotIn("rho", client.calls[0]["indicators"])

    def test_missing_or_wrong_date_leg_does_not_discard_valid_quotes(self):
        result = collect(Client(stale=True))
        self.assertEqual(len(result["records"]), 29)
        self.assertFalse(result["quote_coverage_complete"])
        self.assertEqual(result["collection_plans"][0]["rejected_contracts"][0]["reason"], "source_date_mismatch")
        validate_option_snapshot(result)
        result = collect(Client(missing=True))
        self.assertEqual(len(result["records"]), 1)
        validate_option_snapshot(result)

    def test_same_day_retry_requests_only_missing_codes(self):
        previous = collect(Client(missing=True))
        client = Client()
        current = collect(client, previous)
        self.assertEqual(len(client.calls[0]["codes"].split(",")), 29)
        self.assertEqual(len(current["records"]), 30)
        next_client = Client()
        collect(next_client, current)
        self.assertEqual(next_client.calls, [])

    def test_one_product_below_old_threshold_publishes_without_metrics(self):
        partial = collect(Client(missing=True))
        def one(trade_date, *, universes, **kwargs):
            if universes[0].product != "CU":
                raise ValueError("unavailable directory")
            return partial
        products = [OptionProduct("SHFE", p, p) for p in ["CU", "AU", "AG"]]
        snapshot, status = collect_option_market_snapshot(
            DAY, client=Client(), option_products=products, collect_one=one,
            fallback_directory_loader=None, collection_scope=CORE_PROFILE,
        )
        self.assertTrue(status["coverage"]["publish_eligible"])
        self.assertAlmostEqual(status["coverage"]["product_coverage"], 1/3)
        self.assertFalse(snapshot["quote_coverage_complete"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            publish_option_eod(snapshot, root, surface_shadow_days=5)
            latest = read_option_latest(root)
            self.assertEqual(len(latest["records"]), 1)
            self.assertEqual(latest["quality"]["status"], "core_available")
            self.assertFalse(latest["quality"]["full_chain_verified"])
            surface = read_json(root / "options/surface_latest.json")
            self.assertEqual(surface["trade_date"], DAY)
            report = build_report_input(root)
            self.assertEqual(report["options"]["collection_scope"], CORE_PROFILE)
            self.assertEqual(report["options"]["record_count"], 1)
            self.assertFalse(report["options"]["surface_ready"])
            self.assertEqual(report["options"]["series"][0]["analytics_scope"], "sampled_strikes")

    def test_invalid_dates_and_empty_snapshots_still_cannot_publish(self):
        result = collect(Client())
        result["records"][0]["source_trade_date"] = "2026-10-13"
        with self.assertRaises(OptionSnapshotValidationError):
            validate_option_snapshot(result)
        result["records"] = []
        with self.assertRaises(OptionSnapshotValidationError):
            validate_option_snapshot(result)

    def test_cli_publishes_core_and_report_reads_it_without_live_requests(self):
        from types import SimpleNamespace
        from functools import partial
        from scripts.collect_ifind_options import main
        import pandas as pd
        client = Client(missing=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json_if_changed(root / "latest.json", futures())
            write_json_if_changed(root / "last_run_status.json", {"trade_date": DAY})
            with patch("sys.argv", ["collect", "--all-products", "--date", DAY, "--data-dir", str(root)]), \
                 patch("scripts.collect_ifind_options.IFindHTTPClient", return_value=client), \
                 patch("scripts.collect_ifind_options.load_catalog", return_value=SimpleNamespace(options=[OptionProduct("SHFE", "CU", "copper")])), \
                 patch("china_commodities.option_core.collect_option_daily", return_value=pd.DataFrame(directory())), \
                 patch("china_commodities.option_core.collect_openctp_option_directories", return_value={("SHFE", "CU"): directory()}), \
                 patch("scripts.collect_ifind_options.collect_option_market_snapshot", partial(collect_option_market_snapshot, fallback_directory_loader=lambda *a: {("SHFE", "CU"): directory()})), \
                 patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(main(), 0)
            status = read_json(root / "options/last_run_status.json")
            self.assertTrue(status["published"])
            self.assertTrue(status["data_fresh"])
            report = build_report_input(root)
            self.assertEqual(report["options"]["record_count"], 1)
            self.assertEqual(report["options"]["collection_plans"][0]["selected_contract_count"], 30)
            self.assertNotIn("selected_directory", report["options"]["collection_plans"][0])
            self.assertEqual(len(client.calls), 1)
            reconcile_main_status(root)
            reconcile_main_status(root)
            report = build_report_input(root)
            modules = report["quality"]["futures_status"]["modules"]
            self.assertEqual(len([m for m in modules if m["dataset"] == "options"]), 1)
            self.assertEqual(modules[0]["scope"], "core_sample")
            self.assertEqual(report["quality"]["futures_status"]["module_quality"]["options_chain"], "core_available")
    def test_sparse_atm_sample_does_not_fabricate_25delta_skew(self):
        result = collect(Client())
        for r in result["records"]:
            r["iv_percent"] = 20
            r["greeks"]["selected"] = {"iv_percent": 20, "delta": .5 if r["option_type"] == "C" else -.5}
        surface = build_option_surface(result)["surfaces"][0]
        self.assertTrue(surface["core_atm_ready"])
        self.assertFalse(surface["surface_ready"])
        self.assertFalse(surface["skew_ready"])
        self.assertIsNone(surface["risk_reversal_25d_iv_points"])
        for r in result["records"]:
            r["settle"] = None
        surface = build_option_surface(result)["surfaces"][0]
        self.assertFalse(surface["core_atm_ready"])

    def test_standard_and_monthly_average_never_pair_even_without_expiry(self):
        rows = []
        for strike in range(100, 200):
            for marker in ("", "MS"):
                for side in ("C", "P"):
                    rows.append(dict(contract=f"CF701{marker}{side}{strike}", underlying_contract="CF701",
                                     strike=strike, option_type=side, expiry_date=None))
        selected, plan = select_core_contracts(rows, {"CF701": {"settle": 150}}, [], DAY)
        self.assertEqual(len(selected), 30)
        self.assertEqual(plan["series_type"], "standard")
        self.assertTrue(all("MS" not in r["contract"] for r in selected))
        self.assertEqual({r["option_type"] for r in selected}, {"C", "P"})

    def test_metadata_is_loaded_once_before_selection_and_old_plan_invalidated(self):
        rows = directory()
        missing = [dict(r, expiry_date=None) for r in rows]
        from unittest.mock import Mock
        loader = Mock(return_value={("SHFE", "CU"): rows})
        previous = collect(Client())
        previous["collection_plans"][0]["selector_version"] = 1
        previous["collection_plans"][0]["selected_directory"] = []
        collector = CoreOptionCollector(futures_snapshot=futures(), previous_snapshot=previous, metadata_loader=loader)
        for _ in range(2):
            result = collector(DAY, client=Client(), universes=[UNIVERSE],
                               directory_records_by_product={("SHFE", "CU"): missing})
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(result["selection"]["expiry_date"], "2026-11-25")
        self.assertEqual(result["selection"]["unresolved_expiry_count"], 0)

    def test_delta_wings_use_existing_evidence_inside_unchanged_budget(self):
        rows = directory()
        previous = [dict(r, delta=(-.25 if r["option_type"] == "P" else .75))
                    for r in rows if r["strike"] == 70000]
        previous += [dict(r, delta=(.25 if r["option_type"] == "C" else -.75))
                     for r in rows if r["strike"] == 90000]
        selected, _ = select_core_contracts(rows, {"CU2612": {"settle": 80000}}, previous, DAY)
        self.assertEqual(len(selected), 30)
        self.assertTrue({70000, 80000, 90000} <= {r["strike"] for r in selected})

    def test_price_only_can_publish_but_one_leg_cannot_claim_paired_atm(self):
        result = collect(Client(missing=True))
        row = result["records"][0]
        row["greeks"]["selected"] = {"iv_percent": 20, "delta": .5}
        surface = build_option_surface(result)["surfaces"][0]
        self.assertFalse(surface["core_atm_ready"])
        self.assertIsNone(surface["atm_iv_percent"])

    def test_surface_does_not_mix_monthly_average_and_standard_even_same_expiry(self):
        result = collect(Client())
        first = result["records"][0]
        result["records"].append(dict(first, contract=f"CU2612MSC{int(first['strike'])}"))
        surface = build_option_surface(result)
        self.assertEqual(surface["series_count"], 2)
        self.assertEqual({s["series_type"] for s in surface["surfaces"]}, {"standard", "monthly_average"})

    def test_invalid_field_is_removed_without_discarding_valid_volume(self):
        class BadPrice(Client):
            def request(self, endpoint, payload):
                result = super().request(endpoint, payload)
                for table in result["tables"]:
                    table["table"].update(settlement=[-1], delta=[5])
                return result
        result = collect(BadPrice())
        self.assertEqual(len(result["records"]), 30)
        for row in result["records"]:
            self.assertIsNone(row["settle"])
            self.assertIsNone(row["delta"])
            self.assertEqual(row["volume"], 10)
            self.assertIn("settle", row["invalid_fields"])
        validate_option_snapshot(result)


if __name__ == "__main__":
    unittest.main()
