from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from china_commodities.source_registry import (
    CORE_PHYSICAL_PRODUCTS,
    EXTERNAL_TARGETS,
    SourceRegistryError,
    load_source_registry,
)


class SourceRegistryTests(unittest.TestCase):
    def test_approved_scope_has_only_pinned_or_explicit_unavailable_targets(self) -> None:
        registry = load_source_registry()

        self.assertEqual(
            tuple(target.key for target in registry.physical_products),
            CORE_PHYSICAL_PRODUCTS,
        )
        self.assertEqual(
            tuple(target.key for target in registry.external_targets),
            EXTERNAL_TARGETS,
        )
        self.assertTrue(
            all(
                target.series or target.missing_reason
                for target in (*registry.physical_products, *registry.external_targets)
            )
        )
        self.assertFalse(registry.audit()["production_uses_natural_language_search"])
        self.assertEqual(
            {series.indicator_id for series in registry.series("physical")},
            {"S011038838", "S005948590", "S005696248", "S011318489"},
        )

    def test_rejects_approved_product_scope_drift(self) -> None:
        source = Path("config/data_foundation.json")
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["physical_products"] = payload["physical_products"][:-1]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "registry.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(SourceRegistryError, "approved 20-product"):
                load_source_registry(path)


if __name__ == "__main__":
    unittest.main()
