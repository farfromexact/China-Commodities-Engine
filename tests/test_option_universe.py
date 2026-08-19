from __future__ import annotations

import unittest
from unittest.mock import Mock

from china_commodities.catalog import OptionProduct
from china_commodities.option_universe import (
    collect_openctp_option_directories,
    normalize_openctp_option_directory,
)


PRODUCTS = (
    OptionProduct("DCE", "JM", "焦煤期权"),
    OptionProduct("INE", "BC", "国际铜期权"),
)


def _row(
    exchange: str,
    contract: str,
    underlying: str,
    option_type: str,
    *,
    open_date: str = "2026-01-01",
    expiry_date: str = "2026-09-16",
) -> dict:
    return {
        "ExchangeID": exchange,
        "InstrumentID": contract,
        "OpenDate": open_date,
        "ExpireDate": expiry_date,
        "UnderlyingInstrID": underlying,
        "OptionsType": option_type,
        "StrikePrice": 1000.0,
    }


class OptionUniverseTests(unittest.TestCase):
    def test_normalizes_only_active_catalog_contracts(self) -> None:
        rows = [
            _row("DCE", "jm2610-C-1000", "jm2610", "1"),
            _row("DCE", "jm2610-P-1000", "jm2610", "2"),
            _row("INE", "bc2609C100000", "bc2609", "1"),
            _row(
                "DCE",
                "jm2608-C-1000",
                "jm2608",
                "1",
                expiry_date="2026-08-10",
            ),
            _row("DCE", "i2609-C-700", "i2609", "1"),
        ]

        result = normalize_openctp_option_directory(
            rows,
            trade_date="2026-08-19",
            option_products=PRODUCTS,
        )

        self.assertEqual(len(result[("DCE", "JM")]), 2)
        self.assertEqual(result[("DCE", "JM")][0]["option_type"], "C")
        self.assertEqual(result[("DCE", "JM")][1]["option_type"], "P")
        self.assertEqual(
            result[("INE", "BC")][0]["expiry_date"],
            "2026-09-16",
        )
        self.assertEqual(
            result[("INE", "BC")][0]["universe_source"],
            "openctp_contract_directory",
        )

    def test_download_validates_and_normalizes_response(self) -> None:
        response = Mock()
        response.json.return_value = {
            "data": [_row("DCE", "jm2610-C-1000", "jm2610", "1")]
        }
        transport = Mock(return_value=response)

        result = collect_openctp_option_directories(
            "2026-08-19",
            PRODUCTS,
            transport=transport,
        )

        response.raise_for_status.assert_called_once_with()
        self.assertEqual(transport.call_args.kwargs["timeout"], 60)
        self.assertIn(("DCE", "JM"), result)


if __name__ == "__main__":
    unittest.main()
