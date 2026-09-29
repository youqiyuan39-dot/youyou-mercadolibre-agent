import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import publish_service as publishing


def sample_draft():
    return {
        "payload": {
            "title": "Carburetor and Spark Plug Kit",
            "family_name": "Brush Cutter Carburetor Kit",
            "category_id": "CBT413716",
            "currency_id": "USD",
            "available_quantity": 1000,
            "description": "Confirmed product description.",
            "seller_sku": "AUTO-123",
            "item_condition_value_id": "2230284",
            "warranty_type": "No warranty",
            "warranty_type_value_id": "6150835",
            "sites_to_sell": [
                {"site_id": "MLB", "net_proceeds": 6.99, "logistic_type": "remote", "listing_type_id": "gold_special"},
                {"site_id": "MLM", "net_proceeds": 6.99, "logistic_type": "remote", "listing_type_id": "gold_special"},
            ],
            "attributes": [
                {"id": "BRAND", "value_name": "Generic"},
                {"id": "MODEL", "value_name": ""},
            ],
        },
        "pricing_plan": {"target_net_proceeds_usd": 6.99},
        "evidence": {"image_review": {"gallery": [{"local_file": "product.png"}]}},
    }


class StubClient:
    def __init__(self, partial=False):
        self.uploads = []
        self.posts = []
        self.partial = partial

    def me(self):
        return {"tags": ["user_product_seller"], "status": {"sell": {"allow": True}}}

    def category(self, category_id):
        domain = "GARDEN_MACHINE_RECOIL_STARTERS" if category_id.endswith("455591") else "CARBURETORS"
        return {"id": category_id, "name": "Category", "settings": {
            "catalog_domain": f"{category_id[:3]}-{domain}", "status": "enabled", "listing_allowed": True,
        }}

    def upload_picture(self, data, filename, content_type):
        self.uploads.append((data, filename, content_type))
        return {"id": "123-CBT456_092026"}

    def post_json(self, path, payload):
        self.posts.append((path, payload))
        if path.startswith("/global/user-products/") and path != "/global/user-products/families":
            return {"item_id": "CBT1", "siteless_user_product_id": path.rsplit("/", 1)[-1], "siteless_family_id": 9,
                    "site_items": [{"site_id": row["site_id"], "item_id": f"{row['site_id']}2"} for row in payload["sites_to_sell"]]}
        result = []
        for index, item in enumerate(payload):
            sites = []
            for row in item["sites_to_sell"]:
                if self.partial and row["site_id"] == "MLM":
                    sites.append({"site_id": "MLM", "error": {"message": "shipping unsupported"}})
                else:
                    sites.append({"site_id": row["site_id"], "item_id": f"{row['site_id']}{index + 1}"})
            result.append({"item_id": f"CBT{index + 1}", "siteless_user_product_id": f"U{index + 1}",
                           "siteless_family_id": 9, "site_items": sites})
        return result


class PublishServiceTests(unittest.TestCase):
    def test_category_candidate_prefers_recoil_domain_for_explicit_pull_starter(self):
        candidates = [
            {"category_id": "CBT457258", "domain_id": "CBT-VEHICLE_STARTERS"},
            {"category_id": "CBT455591", "domain_id": "CBT-GARDEN_MACHINE_RECOIL_STARTERS"},
        ]
        selected = publishing.select_category_candidate("Pull Starter for 49cc ATV", candidates)
        self.assertEqual(selected["category_id"], "CBT455591")

    def test_category_guard_rejects_pull_starter_in_electric_starter_domain(self):
        draft = sample_draft()
        draft["payload"]["title"] = "Pull Starter for 49cc 2-Stroke ATV"
        draft["payload"]["category_id"] = "CBT457258"

        class CategoryClient:
            def category(self, category_id):
                return {"id": category_id, "name": "Starter Motor", "settings": {
                    "catalog_domain": f"{category_id[:3]}-VEHICLE_STARTERS",
                    "status": "enabled", "listing_allowed": True,
                }}

        result = publishing.validate_category_sites(draft, CategoryClient())
        self.assertFalse(result["valid"])
        self.assertIn("手拉/回弹启动器", result["issues"][0])

    def test_recoil_starter_category_maps_to_mexico_and_brazil(self):
        draft = sample_draft()
        draft["payload"]["title"] = "Pull Starter for 49cc 2-Stroke ATV"
        draft["payload"]["category_id"] = "CBT455591"
        result = publishing.validate_category_sites(draft, StubClient())
        self.assertTrue(result["valid"])
        self.assertEqual([row["category_id"] for row in result["sites"]], ["MLB455591", "MLM455591"])

    def test_payload_matches_user_products_create_shape(self):
        payload = publishing.build_user_product_payload(sample_draft(), ["PIC1"])[0]
        self.assertEqual(payload["family_name"], "Brush Cutter Carburetor Kit")
        self.assertEqual(payload["currency_id"], "USD")
        self.assertEqual(payload["global_net_proceeds"], 6.99)
        self.assertNotIn("title", payload)
        self.assertEqual(payload["sites_to_sell"][0]["net_proceeds"], 6.99)
        self.assertEqual(next(row for row in payload["attributes"] if row["id"] == "BRAND")["value_name"], "Generic")
        self.assertFalse(any(row["id"] == "MODEL" for row in payload["attributes"]))
        self.assertEqual(next(row for row in payload["attributes"] if row["id"] == "SELLER_SKU")["value_name"], "AUTO-123")
        self.assertEqual(next(row for row in payload["attributes"] if row["id"] == "ITEM_CONDITION")["values"][0]["id"], "2230284")

    def test_listing_type_defaults_to_classic_and_preserves_explicit_premium(self):
        draft = sample_draft()
        draft["payload"]["sites_to_sell"][0].pop("listing_type_id")
        draft["payload"]["sites_to_sell"][1]["listing_type_id"] = "gold_pro"
        payload = publishing.build_user_product_payload(draft, ["PIC1"])[0]
        self.assertEqual(payload["sites_to_sell"][0]["listing_type_id"], "gold_special")
        self.assertEqual(payload["sites_to_sell"][1]["listing_type_id"], "gold_pro")

    def test_publish_requires_confirmation_and_records_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            drafts = root / "drafts"
            assets = root / "assets"
            reviews = root / "reviews"
            drafts.mkdir(); assets.mkdir(); reviews.mkdir()
            name = "1688-1-draft.json"
            (drafts / name).write_text(json.dumps(sample_draft()), encoding="utf-8")
            (assets / "product.png").write_bytes(b"png-data")
            (reviews / "1688-1-draft-approval.json").write_text(json.dumps({"approved": True}), encoding="utf-8")
            stub = StubClient()
            with self.assertRaises(publishing.PublishError):
                publishing.publish_draft(name, confirmed=False, drafts_dir=drafts, root=root, preflight_issues=lambda _: [], client=stub)
            result = publishing.publish_draft(name, confirmed=True, drafts_dir=drafts, root=root, preflight_issues=lambda _: [], client=stub)
            self.assertEqual(result["status"], "success")
            self.assertEqual(stub.posts[0][0], "/global/user-products/families")
            self.assertTrue((root / "data" / "publish_records" / "1688-1-draft.json").is_file())
            with self.assertRaises(publishing.PublishError):
                publishing.publish_draft(name, confirmed=True, drafts_dir=drafts, root=root, preflight_issues=lambda _: [], client=stub)

    def test_publish_creates_one_user_product_per_sku(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            drafts = root / "drafts"
            assets = root / "assets"
            reviews = root / "reviews"
            drafts.mkdir(); assets.mkdir(); reviews.mkdir()
            name = "1688-many-draft.json"
            draft = sample_draft()
            draft["evidence"]["sku_details"] = [
                {"sku_id": "A", "seller_sku": "AUTO-A", "available_quantity": 4, "target_net_proceeds_usd": 7.1,
                 "site_pricing": [
                     {**draft["payload"]["sites_to_sell"][0], "net_proceeds": 7.1},
                     {**draft["payload"]["sites_to_sell"][1], "net_proceeds": 7.4},
                 ], "variation_attributes": [{"name": "Source option", "value": "A"}]},
                {"sku_id": "B", "seller_sku": "AUTO-B", "available_quantity": 7, "target_net_proceeds_usd": 8.2,
                 "site_pricing": [
                     {**draft["payload"]["sites_to_sell"][0], "net_proceeds": 8.2},
                     {**draft["payload"]["sites_to_sell"][1], "net_proceeds": 8.6},
                 ],
                 "variation_attributes": [{"name": "Source option", "value": "B"}]},
            ]
            draft["evidence"]["image_review"]["sku_listing_galleries"] = [
                {"sku_id": "A", "gallery": [{"local_file": "product.png"}]},
                {"sku_id": "B", "gallery": [{"local_file": "product.png"}]},
            ]
            (drafts / name).write_text(json.dumps(draft), encoding="utf-8")
            (reviews / "1688-many-draft-approval.json").write_text(json.dumps({"approved": True}), encoding="utf-8")
            stub = StubClient()
            (assets / "product.png").write_bytes(b"png-data")
            result = publishing.publish_draft(name, confirmed=True, drafts_dir=drafts, root=root, preflight_issues=lambda _: [], client=stub)
            self.assertEqual(result["status"], "success")
            request = stub.posts[0][1]
            self.assertEqual(len(request), 2)
            self.assertEqual(request[0]["global_net_proceeds"], 7.1)
            self.assertEqual(
                [[site["net_proceeds"] for site in sku["sites_to_sell"]] for sku in request],
                [[7.1, 7.4], [8.2, 8.6]],
            )
            self.assertEqual(next(x for x in request[1]["attributes"] if x.get("id") == "SELLER_SKU")["value_name"], "AUTO-B")

    def test_multi_sku_missing_matrix_amount_does_not_use_legacy_default(self):
        draft = sample_draft()
        draft["evidence"]["sku_details"] = [
            {
                "sku_id": "A", "seller_sku": "AUTO-A", "available_quantity": 4,
                "target_net_proceeds_usd": 7.1,
                "site_pricing": [
                    {"site_id": "MLB", "net_proceeds": 7.1, "logistic_type": "remote"},
                    {"site_id": "MLM", "net_proceeds": "", "logistic_type": "remote"},
                ],
                "variation_attributes": [{"name": "Source option", "value": "A"}],
            },
            {
                "sku_id": "B", "seller_sku": "AUTO-B", "available_quantity": 4,
                "target_net_proceeds_usd": 8.2,
                "site_pricing": [
                    {"site_id": "MLB", "net_proceeds": 8.2, "logistic_type": "remote"},
                    {"site_id": "MLM", "net_proceeds": 8.6, "logistic_type": "remote"},
                ],
                "variation_attributes": [{"name": "Source option", "value": "B"}],
            },
        ]
        validation = publishing.validate_publish_skus(draft)
        self.assertIn("MLM 缺少独立价格或净回款", validation[0]["issues"])

    def test_partial_retry_sends_only_failed_site(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); drafts = root / "drafts"; assets = root / "assets"; reviews = root / "reviews"
            drafts.mkdir(); assets.mkdir(); reviews.mkdir()
            name = "1688-retry-draft.json"
            (drafts / name).write_text(json.dumps(sample_draft()), encoding="utf-8")
            (assets / "product.png").write_bytes(b"png-data")
            (reviews / "1688-retry-draft-approval.json").write_text(json.dumps({"approved": True}), encoding="utf-8")
            first = publishing.publish_draft(name, confirmed=True, drafts_dir=drafts, root=root,
                                             preflight_issues=lambda _: [], client=StubClient(partial=True))
            self.assertEqual(first["status"], "partial")
            retry_client = StubClient()
            second = publishing.publish_draft(name, confirmed=True, drafts_dir=drafts, root=root,
                                              preflight_issues=lambda _: [], client=retry_client, retry_failed_only=True)
            self.assertEqual(second["status"], "success")
            self.assertEqual(retry_client.posts[0][0], "/global/user-products/U1")
            self.assertEqual([x["site_id"] for x in retry_client.posts[0][1]["sites_to_sell"]], ["MLM"])

    def test_response_with_one_site_failure_is_partial(self):
        status, error = publishing._response_outcome([{"site_items": [
            {"site_id": "MLB", "item_id": "MLB1"},
            {"site_id": "MLM", "error": {"message": "shipping unsupported"}},
        ]}])
        self.assertEqual(status, "partial")
        self.assertIn("MLM", error)

    def test_refresh_treats_paused_item_with_platform_reason_as_failed(self):
        class RefreshClient:
            def get(self, path):
                self.assert_path = path
                return {"id": "MLM1", "status": "paused", "sub_status": ["category_error"],
                        "category_id": "MLM457258", "domain_id": "MLM-VEHICLE_STARTERS"}

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); drafts = root / "drafts"; records = root / "data" / "publish_records"
            drafts.mkdir(); records.mkdir(parents=True)
            name = "1688-refresh-draft.json"
            record = {"status": "success", "variants": [{"seller_sku": "AUTO-1", "sites": [
                {"site_id": "MLM", "item_id": "MLM1", "status": "success"},
            ]}]}
            (records / name).write_text(json.dumps(record), encoding="utf-8")
            refreshed = publishing.refresh_publish_record(name, drafts_dir=drafts, root=root, client=RefreshClient())
            site = refreshed["variants"][0]["sites"][0]
            self.assertEqual(refreshed["status"], "failed")
            self.assertEqual(site["status"], "failed")
            self.assertIn("category_error", site["error"])


if __name__ == "__main__":
    unittest.main()
