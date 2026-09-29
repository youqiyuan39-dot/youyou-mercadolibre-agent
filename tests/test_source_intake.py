import json
import tempfile
import unittest
from pathlib import Path

from src.source_intake import (
    IntakeError,
    normalize_collection,
    save_collection,
    save_collection_and_enqueue,
)


class SourceIntakeTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "source_url": "https://detail.1688.com/offer/856658055965.html?x=1",
            "title": "C1Q-W40A 化油器",
            "selected_variant": ["化油器"],
            "pricing": {"amount": 23, "currency": "CNY", "evidence": "visible_selected_price"},
            "images": ["https://cbu01.alicdn.com/test.jpg"],
            "product_images": [
                "https://cbu01.alicdn.com/product-1.jpg",
                "https://cbu01.alicdn.com/product-2.jpg",
            ],
            "sku_images": [
                {"url": "https://cbu01.alicdn.com/sku-1.jpg", "variant": "化油器", "sku_id": "sku-1"}
            ],
            "detail_images": ["https://cbu01.alicdn.com/detail-1.jpg"],
            "package": {"weight_g": 60, "length_cm": 6, "width_cm": 4, "height_cm": 3, "evidence": "visible_package_table"},
            "domestic_shipping": {"amount": 7.5, "free": False, "evidence": "visible_shipping_text"},
            "attributes": {"材质": "金属"},
            "pricing_tiers": [{"min_quantity": 3, "price": 22}],
            "minimum_order_quantity": 3,
            "skus": [{
                "sku_id": "sku-1", "variant": "化油器", "price": 23, "inventory": 12322,
                "package": {"weight_g": 60, "length_cm": 6, "width_cm": 4, "height_cm": 3},
            }],
            "seller": {"company_name": "供应商公司"},
            "extraction_source": "1688_structured_page_state",
        }

    def test_normalizes_visible_supplier_evidence(self):
        result = normalize_collection(self.payload)
        self.assertEqual(result["offer_id"], "856658055965")
        self.assertEqual(result["pricing"]["amount"], 23)
        self.assertEqual(result["image_counts"], {"product": 2, "sku": 1, "detail": 1})
        self.assertEqual(result["sku_images"][0]["variant"], "化油器")
        self.assertEqual(result["skus"][0]["inventory"], 12322)
        self.assertEqual(result["pricing_tiers"][0]["min_quantity"], 3)
        self.assertEqual(result["extraction_source"], "1688_structured_page_state")
        self.assertEqual(result["status"], "COLLECTED_UNVERIFIED")
        self.assertFalse(result["privacy"]["cookies_collected"])

    def test_normalizes_extended_supplier_facts(self):
        payload = dict(self.payload)
        payload.update({
            "unit": "件",
            "category": {"top_category_id": "123", "post_category_id": "456"},
            "sales": {"total_sold": 88},
            "offer_flags": {"isBuyerProtection": True},
            "cross_border": {"isCrossBorder": True},
            "guarantees": ["七天无理由退货"],
            "buyer_protection": [{"name": "晚发必赔"}],
        })
        normalized = normalize_collection(payload)
        self.assertEqual(normalized["unit"], "件")
        self.assertEqual(normalized["category"]["post_category_id"], "456")
        self.assertEqual(normalized["sales"]["total_sold"], 88)
        self.assertTrue(normalized["offer_flags"]["isBuyerProtection"])
        self.assertEqual(normalized["guarantees"], ["七天无理由退货"])

    def test_product_gallery_urls_are_not_duplicated_as_sku_images(self):
        self.payload["sku_images"].append({
            "url": "https://cbu01.alicdn.com/product-1.jpg",
            "variant": "一段错误的长标签",
            "sku_id": "",
        })
        result = normalize_collection(self.payload)
        self.assertEqual([item["url"] for item in result["sku_images"]], ["https://cbu01.alicdn.com/sku-1.jpg"])

    def test_preserves_full_gallery_for_each_sku(self):
        self.payload["sku_image_sets"] = [
            {"sku_id": "sku-1", "variant": "短款", "images": [
                "https://cbu01.alicdn.com/short-main.jpg", "https://cbu01.alicdn.com/short-detail.jpg"
            ]},
            {"sku_id": "sku-2", "variant": "长款", "images": [
                "https://cbu01.alicdn.com/long-main.jpg", "https://cbu01.alicdn.com/long-detail.jpg"
            ]},
        ]
        normalized = normalize_collection(self.payload)
        self.assertEqual(normalized["image_counts"]["sku"], 4)
        self.assertEqual(normalized["sku_image_sets"][1]["images"][1], "https://cbu01.alicdn.com/long-detail.jpg")
        with tempfile.TemporaryDirectory() as directory:
            draft = json.loads(save_collection_and_enqueue(self.payload, Path(directory))["draft"].read_text(encoding="utf-8"))
        groups = draft["evidence"]["image_review"]["sku_source_galleries"]
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[0]["gallery"]), 2)

    def test_supplier_package_and_domestic_shipping_enter_new_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = save_collection_and_enqueue(self.payload, root)
            draft = json.loads(result["draft"].read_text(encoding="utf-8"))
        self.assertEqual(draft["evidence"]["packed_weight"]["value"], 60)
        self.assertEqual(draft["evidence"]["packed_weight"]["status"], "SUPPLIER_PACKAGING_INFO")
        self.assertEqual(draft["evidence"]["packed_dimensions"]["length"], 6)
        self.assertEqual(draft["pricing_plan"]["domestic_shipping_cny"], 7.5)
        self.assertEqual(draft["payload"]["available_quantity"], 12322)
        self.assertEqual(draft["evidence"]["supplier_page"]["inventory_source"], "1688_SKU_AVAILABLE_QUANTITY")

    def test_inventory_defaults_to_1000_when_1688_has_no_stock_value(self):
        self.payload["skus"] = []
        with tempfile.TemporaryDirectory() as directory:
            draft_path = save_collection_and_enqueue(self.payload, Path(directory))["draft"]
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
        self.assertEqual(draft["payload"]["available_quantity"], 1000)
        self.assertEqual(draft["evidence"]["supplier_page"]["inventory_source"], "DEFAULT_1000_WHEN_1688_UNAVAILABLE")

    def test_rejects_login_and_secret_fields(self):
        self.payload["cookies"] = "secret"
        with self.assertRaises(IntakeError):
            normalize_collection(self.payload)

    def test_rejects_non_product_url(self):
        self.payload["source_url"] = "https://www.1688.com/"
        with self.assertRaises(IntakeError):
            normalize_collection(self.payload)

    def test_collection_mode_and_manual_package_enter_draft(self):
        self.payload["collection_mode"] = "multi"
        self.payload["package"] = {
            "weight_g": 200, "length_cm": 10, "width_cm": 8, "height_cm": 6,
            "evidence": "USER_INPUT_UNVERIFIED",
        }
        normalized = normalize_collection(self.payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = save_collection_and_enqueue(self.payload, root)
            draft = json.loads(result["draft"].read_text(encoding="utf-8"))
        self.assertEqual(normalized["collection_mode"], "multi")
        self.assertEqual(draft["evidence"]["supplier_page"]["collection_mode"], "multi")
        self.assertEqual(draft["evidence"]["packed_weight"]["value"], 200)
        self.assertEqual(draft["evidence"]["packed_dimensions"]["length"], 10)
        self.assertEqual(draft["evidence"]["packed_weight"]["status"], "USER_INPUT_UNVERIFIED")

    def test_saves_utf8_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = save_collection(self.payload, Path(directory))
            text = path.read_text(encoding="utf-8")
        self.assertIn("化油器", text)

    def test_collection_enters_processing_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = save_collection_and_enqueue(self.payload, root)
            draft = result["draft"].read_text(encoding="utf-8")
            board = result["board"].read_text(encoding="utf-8")
            package_job_exists = (root / "jobs" / "1688-856658055965-draft-package-ai-request.json").is_file()
        self.assertTrue(result["draft_created"])
        self.assertEqual(result["queue_status"], "PENDING_PROCESSING")
        self.assertEqual(result["image_counts"]["product"], 2)
        self.assertEqual(result["image_counts"]["sku"], 1)
        self.assertEqual(result["image_counts"]["detail"], 1)
        self.assertEqual(result["validation_stage"], "READY_FOR_AI")
        self.assertIn("C1Q-W40A 化油器", draft)
        self.assertIn("source_gallery", draft)
        self.assertIn("sku_gallery", draft)
        self.assertIn("detail_gallery", draft)
        parsed_draft = json.loads(draft)
        self.assertEqual(parsed_draft["payload"]["buying_mode"], "buy_it_now")
        self.assertEqual(parsed_draft["payload"]["warranty_type_value_id"], "6150835")
        self.assertTrue(parsed_draft["payload"]["seller_sku"].startswith("AUTO-"))
        self.assertEqual(parsed_draft["evidence"]["supplier_page"]["skus"][0]["inventory"], 12322)
        self.assertEqual(result["package_ai_status"], "NOT_NEEDED_SUPPLIER_EVIDENCE")
        self.assertFalse(package_job_exists)
        self.assertIn("READY_FOR_AI", board)

    def test_repeat_collection_refreshes_source_facts_but_preserves_locked_title(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = save_collection_and_enqueue(self.payload, root)
            draft = json.loads(first["draft"].read_text(encoding="utf-8"))
            draft["payload"]["title"] = "人工英文标题"
            draft["payload"]["family_name"] = "人工英文标题"
            draft["edit_metadata"] = {"field_locks": {"title": True, "family_name": True}}
            first["draft"].write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
            self.payload["pricing"]["amount"] = 19
            self.payload["domestic_shipping"]["amount"] = 6
            second = save_collection_and_enqueue(self.payload, root)
            saved = json.loads(second["draft"].read_text(encoding="utf-8"))
        self.assertFalse(second["draft_created"])
        self.assertEqual(saved["payload"]["title"], "人工英文标题")
        self.assertEqual(saved["evidence"]["purchase_cost"]["amount"], 19)
        self.assertEqual(saved["pricing_plan"]["domestic_shipping_cny"], 6)

    def test_repeat_collection_repairs_failed_first_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = dict(self.payload)
            bad["title"] = "供应商公司"
            bad["package"] = {}
            bad["detail_images"] = []
            first = save_collection_and_enqueue(bad, root)
            repaired = save_collection_and_enqueue(self.payload, root)
            draft = json.loads(repaired["draft"].read_text(encoding="utf-8"))
        self.assertEqual(draft["payload"]["title"], "C1Q-W40A 化油器")
        self.assertEqual(draft["evidence"]["packed_weight"]["value"], 60)
        self.assertEqual(len(draft["evidence"]["image_review"]["detail_gallery"]), 1)


if __name__ == "__main__":
    unittest.main()
