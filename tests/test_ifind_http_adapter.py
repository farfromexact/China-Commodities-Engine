from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from china_commodities.collectors.ifind_http_adapter import (
    IFindHTTPClient,
    IFindHTTPError,
    collect_futures_daily,
    collect_futures_history,
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


class SplitOnLargeRangeTransport(FakeTransport):
    def __call__(self, url, headers, payload, timeout):
        if url.endswith("get_access_token"):
            return super().__call__(url, headers, payload, timeout)
        self.calls.append((url, headers, payload, timeout))
        codes = payload["codes"].split(",")
        if len(codes) > 1:
            return {
                "errorcode": -4210,
                "errmsg": "error happen with input parameters",
            }
        return {
            "errorcode": 0,
            "tables": [
                {
                    "thscode": codes[0],
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


class FlakyTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.remaining_failures = 1

    def __call__(self, url, headers, payload, timeout):
        if not url.endswith("get_access_token") and self.remaining_failures:
            self.calls.append((url, headers, payload, timeout))
            self.remaining_failures -= 1
            raise IFindHTTPError(
                "iFinD transport failed: URLError: SSL handshake timed out"
            )
        return super().__call__(url, headers, payload, timeout)


class IFindHTTPAdapterTests(unittest.TestCase):
    def test_client_retries_transient_transport_failure(self) -> None:
        transport = FlakyTransport()
        client = IFindHTTPClient(
            refresh_token="refresh",
            transport=transport,
            retry_backoff_seconds=0.01,
        )
        with patch(
            "china_commodities.collectors.ifind_http_adapter.time.sleep"
        ) as sleep:
            response = client.request(
                "real_time_quotation", {"codes": "CU2609.SHF"}
            )
        self.assertEqual(response["errorcode"], 0)
        self.assertEqual(len(transport.calls), 3)
        sleep.assert_called_once_with(0.01)

    def test_environment_access_token_avoids_refresh_device_session(self) -> None:
        transport = FakeTransport()
        client = IFindHTTPClient(transport=transport)
        with patch.dict(os.environ, {"IFIND_ACCESS_TOKEN": "shared-access"}):
            token = client.get_access_token()
        self.assertEqual(token, "shared-access")
        self.assertEqual(transport.calls, [])

    def test_client_applies_configured_request_interval(self) -> None:
        client = IFindHTTPClient(
            refresh_token="refresh",
            transport=FakeTransport(),
            minimum_request_interval_seconds=0.55,
        )
        with patch("china_commodities.collectors.ifind_http_adapter.time.monotonic") as clock, patch(
            "china_commodities.collectors.ifind_http_adapter.time.sleep"
        ) as sleep:
            clock.side_effect = [1.0, 1.0, 1.1, 1.55]
            client.request("real_time_quotation", {"codes": "CU2609.SHF"})
            client.request("real_time_quotation", {"codes": "AL2609.SHF"})
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.45)

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

    def test_range_collection_splits_only_parameter_size_errors(self) -> None:
        transport = SplitOnLargeRangeTransport()
        client = IFindHTTPClient(refresh_token="refresh", transport=transport)
        frame = collect_futures_history(
            "2026-08-01",
            "2026-08-18",
            "DCE",
            ["I2609", "I2610"],
            client=client,
            batch_size=2,
        )
        self.assertEqual(sorted(frame["symbol"].tolist()), ["I2609", "I2610"])
        history_calls = [call for call in transport.calls if not call[0].endswith("get_access_token")]
        self.assertEqual(len(history_calls), 3)

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
