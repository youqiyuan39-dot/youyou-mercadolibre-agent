import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.draft_editor import EditorError, approve_local_review, browse_official_categories, create_edited_version, discover_official_category, get_draft, list_drafts, publish_preflight_issues, query_official_category, save_edited_draft, save_uploaded_image, update_review_gallery, update_review_sku_galleries


class DraftEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.drafts = self.root / "drafts"
        self.versions = self.root / "draft_versions"
        self.drafts.mkdir()
        self.source = self.drafts / "sample-draft.json"
        self.source.write_text(
            json.dumps(
                {
                    "status": "DRAFT_LOCAL_ONLY",
                    "requested_action": "draft",
                    "payload": {
                        "title": "旧标题",
                        "family_name": "旧标题",
                        "description": "",
                        "category_id": None,
                        "currency_id": "USD",
                        "available_quantity": None,
                        "sites_to_sell": ["MLB"],
                        "attributes": [],
                    },
                    "evidence": {
                        "supplier_url": "https://detail.1688.com/offer/1.html",
                        "purchase_cost": {"amount": 23, "currency": "CNY"},
                        "packed_weight": None,
                        "packed_dimensions": None,
                        "image_review": {
                            "source_gallery": [
                                {"role": "main", "local_file": "assets/x.png"},
                                {"role": "source_2", "local_file": "assets/y.png"},
                            ],
                            "gallery": [{"role": "main", "local_file": "assets/x.png"}],
                        },
                        "images_human_reviewed": False,
                        "gtin_or_exemption": "",
                        "compatibility_evidence": "",
                    },
                    "human_approved": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_lists_and_reads_drafts(self):
        rows = list_drafts(self.drafts, self.root)
        detail = get_draft("sample-draft.json", self.drafts, self.root)
        self.assertEqual(rows[0]["title"], "旧标题")
        self.assertEqual(detail["mode"], "LOCAL_DRAFT_ONLY")

    def test_browse_category_tree_returns_official_children(self):
        class Stub:
            def site_categories(self, site_id):
                self.site_id = site_id
                return [{"id": "CBT1", "name": "Tools"}]
        client = Stub()
        result = browse_official_categories("CBT", client=client)
        self.assertEqual(client.site_id, "CBT")
        self.assertEqual(result["children"][0]["id"], "CBT1")

    def test_reads_latest_capture_as_candidate_without_overwriting_draft(self):
        intake = self.root / "data" / "intake"
        intake.mkdir(parents=True)
        draft = json.loads(self.source.read_text(encoding="utf-8"))
        draft["evidence"]["source_product_id"] = "1"
        self.source.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        (intake / "1688-1-20260915-010101-000001.json").write_text(
            json.dumps({"title": "正确的新采集标题", "captured_at": "2026-09-15T01:01:01Z", "collection_mode": "single"}, ensure_ascii=False),
            encoding="utf-8",
        )
        detail = get_draft("sample-draft.json", self.drafts, self.root)
        original = json.loads(self.source.read_text(encoding="utf-8"))
        self.assertEqual(detail["latest_capture"]["title"], "正确的新采集标题")
        self.assertEqual(detail["latest_capture"]["collection_mode"], "single")
        self.assertEqual(original["payload"]["title"], "旧标题")

    def test_list_uses_latest_capture_title_images_and_package_facts(self):
        intake = self.root / "data" / "intake"
        intake.mkdir(parents=True)
        draft = json.loads(self.source.read_text(encoding="utf-8"))
        draft["evidence"]["source_product_id"] = "1"
        self.source.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        (intake / "1688-1-20260915-020202-000001.json").write_text(json.dumps({
            "title": "最新商品标题",
            "product_images": ["https://cbu01.alicdn.com/latest.jpg"],
            "package": {"weight_g": 150, "length_cm": 9.5, "width_cm": 6.5, "height_cm": 5},
        }, ensure_ascii=False), encoding="utf-8")
        row = list_drafts(self.drafts, self.root)[0]
        self.assertEqual(row["title"], "最新商品标题")
        self.assertEqual(row["thumbnail"], "https://cbu01.alicdn.com/latest.jpg")
        self.assertFalse(any("打包重量" in value or "打包尺寸" in value for value in row["errors"]))

    def test_save_creates_version_and_keeps_source(self):
        destination = create_edited_version(
            "sample-draft.json",
            {
                "payload": {
                    "title": "新标题",
                    "family_name": "新标题",
                    "description": "新描述",
                    "category_id": "CBT123",
                    "available_quantity": 5,
                    "seller_sku": "AUTO-12345678",
                    "barcode_type": "GTIN",
                    "buying_mode": "buy_it_now",
                    "condition": "new",
                    "catalog_listing": False,
                    "warranty_type": "No warranty",
                    "warranty_type_value_id": "6150835",
                    "warranty_time": "",
                    "sites_to_sell": ["MLB", "BAD"],
                    "attributes": [{"id": "BRAND", "value_name": "Generic"}],
                },
                "physical": {"weight_g": 200, "length_cm": 6, "width_cm": 4, "height_cm": 3, "status": "USER_ESTIMATED", "confidence": "MEDIUM"},
                "pricing": {"purchase_cost_cny": 23, "domestic_shipping_cny": 7.5, "exchange_rate_cny_per_usd": 7, "target_net_proceeds_usd": 8},
                "gtin_or_exemption": "平台免码依据待核",
                "compatibility_evidence": "供应商型号表",
                "images_human_reviewed": True,
                "gallery": [
                    {"role": "source_2", "local_file": "assets/y.png"},
                    {"role": "main", "local_file": "assets/x.png"},
                ],
                "field_locks": {"weight": True},
            },
            self.drafts,
            self.versions,
        )
        original = json.loads(self.source.read_text(encoding="utf-8"))
        saved = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(original["payload"]["title"], "旧标题")
        self.assertEqual(saved["payload"]["title"], "新标题")
        self.assertEqual(saved["payload"]["sites_to_sell"][0]["site_id"], "MLB")
        self.assertEqual(saved["evidence"]["packed_weight"]["value"], 200)
        self.assertEqual(saved["payload"]["seller_sku"], "AUTO-12345678")
        self.assertEqual(saved["evidence"]["shipping_weight_calculation"]["billable_weight_kg"], 0.2)
        self.assertEqual(saved["pricing_plan"]["status"], "REAL_PROFIT_UNVERIFIED")
        self.assertTrue(saved["edit_metadata"]["field_locks"]["weight"])
        self.assertEqual(saved["evidence"]["image_review"]["gallery"][0]["role"], "source_2")
        self.assertEqual(len(saved["evidence"]["image_review"]["source_gallery"]), 2)
        self.assertFalse(saved["human_approved"])
        self.assertEqual(saved["requested_action"], "draft")

    def test_save_preserves_each_sku_record_and_current_pricing_sku(self):
        destination = create_edited_version(
            "sample-draft.json",
            {
                "payload": {"available_quantity": 2, "seller_sku": "AUTO-1-001"},
                "physical": {}, "pricing": {}, "current_pricing_sku_id": "sku-1",
                "sku_details": [{
                    "sku_id": "sku-1", "variant": "14件套", "seller_sku": "AUTO-1-001",
                    "purchase_cost_cny": 10.3, "listing_price_usd": 18.99, "target_net_proceeds_usd": 8.4, "available_quantity": 2,
                    "package": {"weight_g": 80, "length_cm": 20, "width_cm": 23, "height_cm": 2},
                    "barcode_type": "NO_GTIN", "gtin": "", "variation_attributes": [{"name": "套装", "value": "14件套"}],
                    "source_image_count": 3,
                    "site_pricing": [{"site_id": "MLB", "net_proceeds": 8.4, "logistic_type": "remote"}],
                }],
            }, self.drafts, self.versions,
        )
        saved = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(saved["evidence"]["current_pricing_sku_id"], "sku-1")
        self.assertEqual(saved["evidence"]["sku_details"][0]["seller_sku"], "AUTO-1-001")
        self.assertEqual(saved["evidence"]["sku_details"][0]["package"]["height_cm"], 2)
        self.assertEqual(saved["evidence"]["sku_details"][0]["listing_price_usd"], 18.99)
        self.assertEqual(saved["evidence"]["sku_details"][0]["target_net_proceeds_usd"], 8.4)
        self.assertEqual(saved["pricing_plan"]["target_net_proceeds_usd"], 8.4)
        self.assertEqual(saved["payload"]["sites_to_sell"][0]["net_proceeds"], 8.4)

    def test_rejects_secret_fields_and_bad_name(self):
        with self.assertRaises(EditorError):
            create_edited_version("sample-draft.json", {"api_key": "secret"}, self.drafts, self.versions)
        with self.assertRaises(EditorError):
            get_draft("../sample-draft.json", self.drafts, self.root)

    def test_save_updates_working_draft_and_keeps_backup(self):
        source, snapshot, backup = save_edited_draft(
            "sample-draft.json",
            {
                "payload": {
                    "title": "已保存标题", "family_name": "已保存标题", "description": "描述",
                    "category_id": "CBT123", "available_quantity": 5, "seller_sku": "AUTO-12345678",
                    "barcode_type": "GTIN", "buying_mode": "buy_it_now", "condition": "new",
                    "catalog_listing": False, "sites_to_sell": [{"site_id": "MLB", "net_proceeds": 6.99}],
                    "attributes": [],
                },
                "physical": {}, "pricing": {"target_net_proceeds_usd": 6.99},
            },
            self.drafts,
            self.versions,
        )
        current = json.loads(source.read_text(encoding="utf-8"))
        previous = json.loads(backup.read_text(encoding="utf-8"))
        self.assertEqual(current["payload"]["sites_to_sell"][0]["site_id"], "MLB")
        self.assertEqual(current["pricing_plan"]["target_net_proceeds_usd"], 6.99)
        self.assertEqual(previous["payload"]["title"], "旧标题")
        self.assertTrue(snapshot.is_file())

    def test_profit_estimate_status_does_not_block_publish_preflight(self):
        draft = json.loads(self.source.read_text(encoding="utf-8"))
        draft["payload"].update({
            "category_id": "CBT123",
            "available_quantity": 1000,
            "sites_to_sell": [{"site_id": "MLB", "net_proceeds": 6.99}],
        })
        draft["pricing_plan"] = {"status": "REAL_PROFIT_UNVERIFIED"}
        issues = publish_preflight_issues(draft)
        self.assertNotIn("真实利润未核实", issues)

    def test_save_keeps_selected_source_and_generated_images(self):
        generated = self.root / "assets" / "ai-generated" / "sample.png"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b"image")
        _, _, _ = save_edited_draft(
            "sample-draft.json",
            {
                "payload": {"sites_to_sell": [{"site_id": "MLB", "net_proceeds": 6.99}]},
                "physical": {},
                "pricing": {},
                "gallery": [
                    {"role": "listing_2", "local_file": "assets/y.png", "source": "source_gallery"},
                    {"role": "main", "local_file": "ai-generated/sample.png", "source": "ai_generated"},
                ],
            },
            self.drafts,
            self.versions,
        )
        current = json.loads(self.source.read_text(encoding="utf-8"))
        self.assertEqual(len(current["evidence"]["image_review"]["gallery"]), 2)

    def test_official_category_query_is_cached(self):
        class Stub:
            def category(self, _category_id):
                return {"name": "Carburetors", "path_from_root": [], "children_categories": [], "settings": {}}

            def category_attributes(self, _category_id):
                return [{"id": "BRAND", "name": "Brand", "tags": {"required": True}}]

            def technical_specs(self, _category_id):
                return {"groups": []}

        result = query_official_category("CBT413715", self.root, Stub())
        cache = self.root / "data" / "categories" / "CBT413715.json"
        self.assertEqual(result["query_source"], "MERCADOLIBRE_OFFICIAL_API_LIVE")
        self.assertEqual(result["required_attributes"][0]["id"], "BRAND")
        self.assertTrue(cache.is_file())

    def test_rejects_invalid_category_id(self):
        with self.assertRaises(EditorError):
            query_official_category("../secret", self.root, object())

    def test_does_not_send_chinese_supplier_title_to_category_discovery(self):
        with self.assertRaisesRegex(EditorError, "中文采集标题不能直接"):
            discover_official_category("割草机化油器40-5套装", "CBT", "", self.root, object())

    def test_upload_accepts_real_png_and_rejects_fake_image(self):
        import base64
        png = b"\x89PNG\r\n\x1a\n" + b"test"
        path = save_uploaded_image("x.png", "data:image/png;base64," + base64.b64encode(png).decode(), self.root / "assets")
        self.assertTrue((self.root / "assets" / path).is_file())
        with self.assertRaises(EditorError):
            save_uploaded_image("x.png", "data:image/png;base64," + base64.b64encode(b"fake").decode(), self.root / "assets")

    def test_review_gallery_updates_main_draft_and_creates_backup(self):
        assets = self.root / "assets" / "uploads"
        assets.mkdir(parents=True)
        local_file = "uploads/" + "a" * 64 + ".png"
        (self.root / "assets" / local_file).write_bytes(b"image")
        result = update_review_gallery(
            "sample-draft.json",
            [{"local_file": local_file, "source": "local_edit"}],
            self.drafts,
            self.versions,
            self.root,
        )
        saved = json.loads(self.source.read_text(encoding="utf-8"))
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertEqual(saved["evidence"]["image_review"]["gallery"][0]["role"], "main")
        self.assertTrue(saved["evidence"]["images_human_reviewed"])
        self.assertTrue(saved["edit_metadata"]["field_locks"]["pictures"])

    def test_approval_rejects_item_outside_review_stage(self):
        with self.assertRaises(EditorError):
            approve_local_review("sample-draft.json", self.drafts, self.root)

    def test_save_keeps_manual_review_request(self):
        draft = json.loads(self.source.read_text(encoding="utf-8"))
        draft["edit_metadata"] = {"manual_review_requested_at": "2026-09-17T00:00:00+00:00"}
        self.source.write_text(json.dumps(draft), encoding="utf-8")
        changes = {
            "payload": draft["payload"],
            "physical": {"weight_g": 100, "length_cm": 1, "width_cm": 1, "height_cm": 1},
            "pricing": {"purchase_cost_cny": 1, "domestic_shipping_cny": 1, "exchange_rate_cny_per_usd": 7, "packaging_cost_usd": 0.3},
        }
        save_edited_draft("sample-draft.json", changes, self.drafts, self.versions)
        saved = json.loads(self.source.read_text(encoding="utf-8"))
        self.assertEqual(saved["edit_metadata"]["manual_review_requested_at"], "2026-09-17T00:00:00+00:00")

    def test_approval_requires_saved_sites_inventory_and_site_amount(self):
        ready_row = {"name": "sample-draft.json", "stage": "READY_FOR_HUMAN_REVIEW", "review_ready": True}
        draft = json.loads(self.source.read_text(encoding="utf-8"))
        draft["payload"]["category_id"] = "CBT123"
        draft["payload"]["sites_to_sell"] = []
        self.source.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        with patch("src.draft_editor.list_drafts", return_value=[ready_row]):
            with self.assertRaisesRegex(EditorError, "目标站点"):
                approve_local_review("sample-draft.json", self.drafts, self.root)

            draft["payload"]["sites_to_sell"] = [{"site_id": "MLB", "net_proceeds": 4.62}]
            draft["payload"]["available_quantity"] = None
            self.source.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(EditorError, "库存"):
                approve_local_review("sample-draft.json", self.drafts, self.root)

            draft["payload"]["available_quantity"] = 1
            draft["payload"]["sites_to_sell"] = [{"site_id": "MLB"}]
            self.source.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(EditorError, "价格或净收益"):
                approve_local_review("sample-draft.json", self.drafts, self.root)

    def test_review_sku_gallery_keeps_known_sku_and_generated_image(self):
        draft = json.loads(self.source.read_text(encoding="utf-8"))
        draft["evidence"]["supplier_page"] = {"skus": [{"sku_id": "sku-1", "variant": "14件套"}]}
        self.source.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        jobs = self.root / "jobs"
        jobs.mkdir()
        (jobs / "sample-draft-sku-image-job.json").write_text(json.dumps({"items": [
            {"status": "done", "local_file": "ai-generated/sample/01.png"}
        ]}), encoding="utf-8")
        result = update_review_sku_galleries("sample-draft.json", [{
            "sku_id": "sku-1", "gallery": [{"local_file": "ai-generated/sample/01.png", "source": "sku_ai_generated"}]
        }], self.drafts, self.versions, self.root)
        saved = json.loads(self.source.read_text(encoding="utf-8"))
        self.assertEqual(saved["evidence"]["image_review"]["sku_listing_galleries"][0]["variant"], "14件套")
        self.assertTrue(saved["evidence"]["sku_images_human_reviewed"])
        self.assertTrue(Path(result["backup"]).is_file())


if __name__ == "__main__":
    unittest.main()
