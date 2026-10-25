import unittest

from src.validate_listing import validate


class ValidateListingTests(unittest.TestCase):
    def test_v02_nested_evidence_blocks_unverified_packaging_and_gtin(self):
        product = {
            "evidence": {
                "supplier_url": "https://example.invalid/item",
                "purchase_cost": {"amount": 28, "currency": "CNY"},
                "packed_weight": None,
                "packed_dimensions": None,
                "gtin_or_exemption": "",
                "compatibility_evidence": "",
                "images_human_reviewed": False,
            },
            "payload": {"attributes": [{"id": "GTIN", "value_name": ""}]},
        }

        errors, warnings = validate(product)

        self.assertIn("缺少打包重量", errors)
        self.assertIn("缺少打包尺寸", errors)
        self.assertFalse(any("条码" in issue for issue in errors))
        self.assertIn("缺少型号兼容证据，不能让 AI 自行补写", warnings)
        self.assertIn("图片尚未人工核对", warnings)

    def test_no_gtin_marker_is_quiet_even_after_approval(self):
        product = {
            "evidence": {
                "supplier_url": "https://example.invalid/item",
                "purchase_cost": {"amount": 28},
                "packed_weight": {"value": 150},
                "packed_dimensions": {"length": 9.5, "width": 6.5, "height": 5},
                "gtin_or_exemption": "NO_GTIN_PENDING_CATEGORY_CHECK",
                "images_human_reviewed": True,
            },
            "payload": {"barcode_type": "NO_GTIN", "catalog_listing": False, "attributes": []},
            "human_approved": False,
        }
        errors, warnings = validate(product)
        self.assertNotIn("缺少真实条码或平台无条码依据", errors)
        self.assertFalse(any("条码" in issue for issue in warnings))
        product["human_approved"] = True
        errors, _ = validate(product)
        self.assertFalse(any("条码" in issue for issue in errors))


if __name__ == "__main__":
    unittest.main()
