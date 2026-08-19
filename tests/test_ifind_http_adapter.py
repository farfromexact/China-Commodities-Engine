from __future__ import annotations

import unittest

from china_commodities.collectors.ifind_http_adapter import (
    IFindHTTPClient,
    IFindHTTPError,
    collect_futures_daily,
    generate_contract_candidates,
)


class FakeTransport:
    def __init__(self, *, history_error: int = 0):
        self.history_error = history_error
        self.calls: list[tuple[str, dict[str, str], dict | None, int]] = []

    def __call__(self, url: str, headers: dict[str, str], payload: dict | None, timeout: int) -> dict:
        self.calls.append((url, headers, payload, timeout))
        if url.endswith("get_access_token"):
            return {"errorcode": 0, "data": {"access_token": "temporary-access"}}
        if self.history_error:
            return {"errorcode": self.history_error, "errmsg": "permission denied"}
        return {
            "errorcode": 0,
            "tables": [
                {
                    "thscode": "I2609.DCE",
                    "time": ["2026-08-18"],
                    "table": {
                        "open": [780.0],
                        "high": [790.0],
                        "low": [775.0],
                        "close": [788.0],
                        "settlement": [785.0],
                        "preSettlement": [782.0],
                        "volume": [1000],
                        "amount": [123456.0],
                        "openInterest": [2000],
                    },
                }
            ],
        }


class IFindHTTPAdapterTests(unittest.TestCase):
    def test_candidate_generation_uses_exchange_contract_conventions(self) -> None:
        shfe = generate_contract_candidates(
            "2026-08-18", "SHFE", ["RB"], history_years=0, forward_years=0
        )
        czce = generate_contract_candidates(
            "2026-08-18", "CZCE", ["SR"], history_years=0, forward_years=0
        )
        self.assertEqual(shfe[0], "RB2601")
        self.assertEqual(shfe[-1], "RB2612")
        self.assertEqual(czce[0], "SR601")
        self.assertEqual(czce[-1], "SR612")

    def test_refreshes_in_memory_and_normalizes_history(self) -> None:
        transport = FakeTransport()
        client = IFindHTTPClient(refresh_token="refresh", transport=transport)
        frame = collect_futures_daily(
            "2026-08-18", "DCE", ["I2609"], client=client
        )
        self.assertEqual(client.access_token, "temporary-access")
        self.assertEqual(frame.loc[0, "symbol"], "I2609")
        self.assertEqual(frame.loc[0, "date"], "20260818")
        self.assertEqual(frame.loc[0, "settle"], 785.0)
        self.assertEqual(frame.loc[0, "source_provider"], "ifind_http")
        self.assertEqual(transport.calls[0][1]["refresh_token"], "refresh")
        self.assertEqual(transport.calls[1][1]["access_token"], "temporary-access")
        self.assertTrue(transport.calls[1][0].endswith("cmd_history_quotation"))
        self.assertEqual(transport.calls[1][2]["startdate"], "2026-08-18")
        self.assertEqual(transport.calls[1][2]["functionpara"], {"Fill": "Omit"})

    def test_range_query_uses_distinct_start_and_end_dates(self) -> None:
        transport = FakeTransport()
        client = IFindHTTPClient(refresh_token="refresh", transport=transport)
        frame = client.history_quotes_range(
            ["I2609.DCE"],
            ["close"],
            start_date="2026-08-01",
            end_date="2026-08-18",
        )
        self.assertEqual(len(frame), 1)
        self.assertEqual(transport.calls[1][2]["startdate"], "2026-08-01")
        self.assertEqual(transport.calls[1][2]["enddate"], "2026-08-18")

    def test_entitlement_error_is_explicit(self) -> None:
        client = IFindHTTPClient(
            refresh_token="refresh", transport=FakeTransport(history_error=-430)
        )
        with self.assertRaisesRegex(IFindHTTPError, "permission denied"):
            collect_futures_daily(
                "2026-08-18", "DCE", ["I2609"], client=client
            )

    def test_missing_refresh_token_is_explicit(self) -> None:
        client = IFindHTTPClient(refresh_token=None, transport=FakeTransport())
        with self.assertRaisesRegex(IFindHTTPError, "refresh token is required"):
            client.get_access_token()


if __name__ == "__main__":
    unittest.main()
