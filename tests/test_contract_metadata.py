from __future__ import annotations

import unittest

from china_commodities.models import PipelineResult
from china_commodities.storage import _contract_meta


def result_with_metadata(night_session: str | None = "21:00-23:00") -> PipelineResult:
    return PipelineResult(
        trade_date="2026-08-19",
        generated_at="2026-08-19T18:30:00+08:00",
        akshare_version="test",
        scope_verified=True,
        included_exchanges=["SHFE"],
        futures_records=[
            {
                "exchange": "SHFE",
                "product": "CU",
                "contract": "CU2610",
            }
        ],
        contract_metadata=[
            {
                "exchange": "SHFE",
                "product": "CU",
                "contract": "CU2610",
                "multiplier": 5.0,
                "tick_size": 10.0,
                "tick_value": 50.0,
                "night_session": night_session,
                "delivery_unit": "25吨",
                "margin_rate_percent": 10.0,
                "price_limit_percent": 8.0,
                "last_trading_day": "2026-10-15",
                "metadata_status": "official_partial",
                "metadata_vendor": "official_exchange_via_akshare",
                "original_source": "SHFE",
                "source_date": "2026-08-19",
            }
        ],
    )


class ContractMetadataTests(unittest.TestCase):
    def test_complete_requires_static_ninety_nine_and_dynamic_ninety_five(self) -> None:
        payload = _contract_meta(result_with_metadata())
        self.assertEqual(payload["quality_state"], "complete")
        self.assertEqual(payload["static_fields_min_coverage"], 1.0)
        self.assertEqual(payload["dynamic_fields_min_coverage"], 1.0)
        self.assertEqual(payload["contracts"][0]["night_session"], "21:00-23:00")

    def test_missing_static_rule_remains_partial(self) -> None:
        payload = _contract_meta(result_with_metadata(night_session=None))
        self.assertEqual(payload["quality_state"], "partial")
        self.assertEqual(payload["night_session_coverage"], 0.0)


if __name__ == "__main__":
    unittest.main()
