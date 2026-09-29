import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mercadolibre_client as ml


class MercadoLibreClientTests(unittest.TestCase):
    def test_relevant_tags_drops_unneeded_account_data(self):
        self.assertEqual(
            ml.relevant_tags(["normal", "warehouse_management", "user_product_seller"]),
            ["user_product_seller", "warehouse_management"],
        )

    def test_category_preflight_combines_required_attributes(self):
        class Stub:
            def category(self, _category_id):
                return {"name": "Parts", "path_from_root": [], "children_categories": [], "settings": {}}

            def category_attributes(self, _category_id):
                return [{"id": "BRAND", "name": "Brand", "tags": {"required": True}}]

            def technical_specs(self, _category_id):
                return {"groups": [{"components": [{"attributes": [
                    {"id": "BRAND", "name": "Brand", "tags": ["catalog_required"]},
                    {"id": "PACKAGE_WEIGHT", "name": "Weight", "tags": ["required"]}
                ]}]}]}

        result = ml.category_preflight(Stub(), "CBT1")
        self.assertTrue(result["is_leaf"])
        self.assertEqual({row["id"] for row in result["required_attributes"]}, {"BRAND", "PACKAGE_WEIGHT"})
        brand = next(row for row in result["required_attributes"] if row["id"] == "BRAND")
        self.assertIn("catalog_required", brand["tags"])

    def test_local_draft_never_requests_publish(self):
        draft = ml.create_local_draft(
            {
                "category_id": "CBT1",
                "required_attributes": [{"id": "BRAND"}],
                "all_attributes": [{"id": "BRAND"}, {"id": "ITEM_CONDITION"}],
            },
            "user_products",
        )
        self.assertEqual(draft["status"], "DRAFT_LOCAL_ONLY")
        self.assertFalse(draft["human_approved"])
        self.assertEqual(draft["publish_endpoint"], "/global/user-products")
        condition = next(row for row in draft["payload"]["attributes"] if row["id"] == "ITEM_CONDITION")
        self.assertEqual(condition["value_name"], "New")

    def test_category_preflight_keeps_attributes_when_specs_are_forbidden(self):
        class Stub:
            def category(self, _category_id):
                return {"name": "Parts", "path_from_root": [], "children_categories": [], "settings": {}}

            def category_attributes(self, _category_id):
                return [{"id": "BRAND", "name": "Brand", "tags": {"required": True}}]

            def technical_specs(self, _category_id):
                raise ml.MercadoLibreAPIError("HTTP 403")

        result = ml.category_preflight(Stub(), "CBT1")
        self.assertEqual(result["required_attributes"][0]["id"], "BRAND")
        self.assertEqual(result["technical_specs_status"], "UNAVAILABLE")

    def test_orders_snapshot_drops_buyer_and_payment_data(self):
        class Stub:
            def orders(self, _limit):
                return {
                    "paging": {"total": 1},
                    "results": [{
                        "buyer": {"id": 999},
                        "payments": [{"id": 888}],
                        "orders": [{"id": 123}],
                        "config": {"items": [{"id": "MLM1"}]},
                        "shipment": {"id": 456},
                    }],
                }

        result = ml.orders_snapshot(Stub(), 10)
        self.assertEqual(result["orders"][0]["order_ids"], [123])
        self.assertNotIn("buyer", result["orders"][0])
        self.assertNotIn("payments", result["orders"][0])

    def test_write_json_is_utf8(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "x.json"
            ml.write_json(path, {"name": "园林配件"})
            self.assertIn("园林配件", path.read_text(encoding="utf-8"))

    def test_public_category_client_sends_no_authorization_header(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'{"name":"Parts"}'

        original = ml.urllib.request.urlopen
        try:
            def fake(request, timeout):
                captured["authorization"] = request.get_header("Authorization")
                captured["timeout"] = timeout
                return Response()

            ml.urllib.request.urlopen = fake
            result = ml.MercadoLibreCategoryClient().category("CBT1")
        finally:
            ml.urllib.request.urlopen = original
        self.assertEqual(result["name"], "Parts")
        self.assertIsNone(captured["authorization"])

    def test_post_json_and_picture_upload_keep_token_out_of_body(self):
        requests = []

        class Response:
            def __init__(self, value):
                self.value = value
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def read(self):
                return json.dumps(self.value).encode("utf-8")

        original = ml.urllib.request.urlopen
        try:
            def fake(request, timeout):
                requests.append(request)
                return Response({"id": "PIC1"})
            ml.urllib.request.urlopen = fake
            client = ml.MercadoLibreClient("secret-token")
            client.post_json("/global/user-products/families", [{"category_id": "CBT1"}])
            client.upload_picture(b"image", "photo.png", "image/png")
        finally:
            ml.urllib.request.urlopen = original
        self.assertEqual(requests[0].get_method(), "POST")
        self.assertNotIn(b"secret-token", requests[0].data)
        self.assertIn("application/json", requests[0].get_header("Content-type"))
        self.assertIn(b'filename="photo.png"', requests[1].data)
        self.assertIn("multipart/form-data", requests[1].get_header("Content-type"))


if __name__ == "__main__":
    unittest.main()
