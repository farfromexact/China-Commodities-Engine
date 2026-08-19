from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import pandas as pd

from china_commodities.collectors.ifind_adapter import (
    IFindSDKError,
    IFindSDKSession,
    collect_futures_daily,
    contract_to_ifind_code,
)


class FakeResponse:
    def __init__(self, data: pd.DataFrame, errorcode: int = 0, errmsg: str = ""):
        self.data = data
        self.errorcode = errorcode
        self.errmsg = errmsg


class FakeIFind:
    def __init__(self, *, login_code: int = 0, query_error: int = 0):
        self.login_code = login_code
        self.query_error = query_error
        self.login_calls = 0
        self.logout_calls = 0
        self.hq_calls: list[tuple[str, str, str, str, str]] = []

    def THS_iFinDLogin(self, username: str, password: str) -> int:
        self.login_calls += 1
        return self.login_code

    def THS_iFinDLogout(self) -> int:
        self.logout_calls += 1
        return 0

    def THS_GetErrorInfo(self, code: int) -> dict[str, object]:
        return {"errorcode": code, "errmsg": "test error"}

    def THS_HQ(
        self,
        codes: str,
        fields: str,
        params: str,
        begin: str,
        end: str,
    ) -> FakeResponse:
        self.hq_calls.append((codes, fields, params, begin, end))
        frame = pd.DataFrame(
            {
                "time": ["2026-08-18"],
                "thscode": [codes.split(",")[0]],
                "open": [780.0],
                "high": [790.0],
                "low": [775.0],
                "close": [788.0],
                "settlement": [785.0],
                "preSettlement": [782.0],
                "volume": [1000],
                "amount": [123456.0],
                "openInterest": [2000],
            }
        )
        return FakeResponse(frame, self.query_error, "query denied")


class IFindAdapterTests(unittest.TestCase):
    def test_contract_code_mapping(self) -> None:
        self.assertEqual(contract_to_ifind_code("i2609", "DCE"), "I2609.DCE")
        self.assertEqual(contract_to_ifind_code("rb2610", "SHFE"), "RB2610.SHF")
        self.assertEqual(contract_to_ifind_code("sr609", "CZCE"), "SR609.CZC")
        self.assertEqual(contract_to_ifind_code("lc2609", "GFEX"), "LC2609.GFE")
        with self.assertRaises(ValueError):
            contract_to_ifind_code("I0", "DCE")

    def test_session_uses_environment_and_always_logs_out(self) -> None:
        fake = FakeIFind()
        with patch.dict(
            os.environ,
            {"IFIND_USERNAME": "test-user", "IFIND_PASSWORD": "test-password"},
            clear=False,
        ):
            with IFindSDKSession(ifind_module=fake) as session:
                frame = collect_futures_daily(
                    "2026-08-18", "DCE", ["I2609"], session=session
                )
        self.assertEqual(fake.login_calls, 1)
        self.assertEqual(fake.logout_calls, 1)
        self.assertEqual(frame.loc[0, "symbol"], "I2609")
        self.assertEqual(frame.loc[0, "variety"], "I")
        self.assertEqual(frame.loc[0, "date"], "20260818")
        self.assertEqual(frame.loc[0, "settle"], 785.0)
        self.assertEqual(frame.loc[0, "open_interest"], 2000)
        self.assertEqual(frame.loc[0, "source_provider"], "ifind_sdk")

    def test_login_failure_does_not_query_or_logout(self) -> None:
        fake = FakeIFind(login_code=-9)
        with self.assertRaisesRegex(IFindSDKError, "code -9"):
            with IFindSDKSession("user", "password", ifind_module=fake):
                self.fail("session should not be entered")
        self.assertEqual(fake.logout_calls, 0)
        self.assertEqual(fake.hq_calls, [])

    def test_query_entitlement_error_is_explicit_and_logs_out(self) -> None:
        fake = FakeIFind(query_error=-430)
        with self.assertRaisesRegex(IFindSDKError, "THS_HQ failed"):
            with IFindSDKSession("user", "password", ifind_module=fake) as session:
                collect_futures_daily(
                    "2026-08-18", "DCE", ["I2609"], session=session
                )
        self.assertEqual(fake.logout_calls, 1)


if __name__ == "__main__":
    unittest.main()
