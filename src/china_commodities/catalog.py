"""Editable product taxonomy and commodity-option coverage catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


UNKNOWN_SECTOR = "未分类"


@dataclass(frozen=True)
class OptionProduct:
    exchange: str
    product: str
    symbol: str


@dataclass(frozen=True)
class ProductCatalog:
    sectors: dict[str, tuple[str, ...]]
    names: dict[str, str]
    exchanges: dict[str, tuple[str, ...]]
    options: tuple[OptionProduct, ...]

    @property
    def product_to_sector(self) -> dict[str, str]:
        return {
            product.upper(): sector
            for sector, products in self.sectors.items()
            for product in products
        }

    def sector_for(self, product: str) -> str:
        return self.product_to_sector.get(product.upper(), UNKNOWN_SECTOR)

    def name_for(self, product: str) -> str:
        return self.names.get(product.upper(), product.upper())

    def products_for_exchange(self, exchange: str) -> tuple[str, ...]:
        return self.exchanges.get(exchange.upper(), ())


def default_catalog_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "products.json"


def load_catalog(path: str | Path | None = None) -> ProductCatalog:
    catalog_path = Path(path) if path is not None else default_catalog_path()
    payload: dict[str, Any] = json.loads(catalog_path.read_text(encoding="utf-8"))
    sectors = {
        str(sector): tuple(str(product).upper() for product in products)
        for sector, products in payload["sectors"].items()
    }
    names = {str(key).upper(): str(value) for key, value in payload["names"].items()}
    exchanges = {
        str(exchange).upper(): tuple(str(product).upper() for product in products)
        for exchange, products in payload["exchanges"].items()
    }
    options = tuple(
        OptionProduct(
            exchange=str(item["exchange"]).upper(),
            product=str(item["product"]).upper(),
            symbol=str(item["symbol"]),
        )
        for item in payload["options"]
    )
    return ProductCatalog(
        sectors=sectors,
        names=names,
        exchanges=exchanges,
        options=options,
    )
