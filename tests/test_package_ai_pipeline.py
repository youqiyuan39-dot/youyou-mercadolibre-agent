import unittest

from src.package_ai_pipeline import build_package_request


class PackageAIPipelineTests(unittest.TestCase):
    def test_request_uses_images_structured_output_and_no_storage(self):
        draft = {
            "evidence": {
                "supplier_page": {"product_weight_g": 60, "package_table_evidence": "visible_package_table"},
                "image_review": {
                    "detail_gallery": [{"remote_url": "https://cbu01.alicdn.com/detail.jpg"}],
                    "source_gallery": [{"remote_url": "https://cbu01.alicdn.com/product.jpg"}],
                },
            }
        }
        request = build_package_request(draft, "test-model")
        content = request["input"][0]["content"]
        self.assertFalse(request["store"])
        self.assertEqual(request["model"], "test-model")
        self.assertEqual([item["type"] for item in content], ["input_text", "input_image", "input_image"])
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        self.assertTrue(request["text"]["format"]["strict"])


if __name__ == "__main__":
    unittest.main()
