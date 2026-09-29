import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.ai_listing_pipeline import (
    build_responses_request,
    build_review,
    load_approved_examples,
    proposal_quality_issues,
    sanitized_product_context,
    call_responses_api,
    AIListingError,
)


class AIListingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.draft = {
            "status": "DRAFT_LOCAL_ONLY",
            "payload": {
                "title": "Carburetor C1Q-W40A",
                "family_name": "Carburetor C1Q-W40A",
                "category_id": "CBT413715",
                "attributes": [
                    {"id": "MODEL", "value_name": "C1Q-W40A"},
                    {"id": "PACKAGE_WEIGHT", "value_name": "200 g"},
                ],
            },
            "evidence": {
                "supplier_url": "https://example.invalid/item",
                "purchase_cost": {"amount": 23, "currency": "CNY"},
                "packed_weight": {"value": 200, "unit": "g"},
                "packed_dimensions": {"length": 6, "width": 4, "height": 3, "unit": "cm"},
                "supplier_page": {"selected_variant": "化油器", "material_listed": "金属"},
                "gtin_or_exemption": "",
                "compatibility_evidence": "",
                "images_human_reviewed": True,
                "image_review": {"gallery": [{"role": "main"}]},
            },
            "human_approved": False,
        }

    def test_request_uses_structured_outputs_and_never_contains_api_key(self):
        request = build_responses_request(self.draft, "gpt-test")
        self.assertFalse(request["store"])
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertNotIn("api_key", json.dumps(request))
        self.assertIn("natural English", request["instructions"])
        self.assertIn("methodology", request["text"]["format"]["schema"]["required"])
        self.assertIn("exact product type", request["instructions"])
        self.assertIn("site_titles", request["text"]["format"]["schema"]["required"])

    def test_shared_copy_is_english_and_brazil_gets_portuguese_title(self):
        self.draft["payload"]["sites_to_sell"] = ["MLB", "MLM"]
        request = build_responses_request(self.draft, "gpt-test")
        self.assertIn("Brazilian", request["instructions"])
        self.assertIn("Spanish", request["instructions"])
        context = json.loads(request["input"][0]["content"][0]["text"])
        self.assertEqual(context["target_sites"], ["MLB", "MLM"])

    def test_context_excludes_supplier_url_and_cost(self):
        context = sanitized_product_context(self.draft)
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("example.invalid", serialized)
        self.assertNotIn('"amount": 23', serialized)
        self.assertEqual(context["attributes"]["MODEL"], "C1Q-W40A")

    def test_review_never_allows_publish(self):
        review = build_review(self.draft)
        self.assertFalse(review["publish_allowed"])
        self.assertFalse(review["human_approved"])
        self.assertIn("缺少真实条码或平台无条码依据", review["errors"])

    def test_only_human_approved_examples_are_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "examples.jsonl"
            path.write_text(
                '{"human_approved": false, "title": "bad"}\n'
                '{"human_approved": true, "title": "good"}\n',
                encoding="utf-8",
            )
            rows = load_approved_examples(path)
        self.assertEqual([row["title"] for row in rows], ["good"])

    def test_quality_gate_rejects_shallow_internal_copy(self):
        proposal = {
            "title": "Accesorios para cortadora",
            "description": "La información de origen menciona un carburador.",
            "site_titles": [],
        }
        issues = proposal_quality_issues(proposal, self.draft)
        self.assertTrue(any("单薄" in issue for issue in issues))
        self.assertTrue(any("内部证据" in issue for issue in issues))

    def test_quality_gate_accepts_structured_english_copy(self):
        description = (
            "Replacement carburetor kit for routine brush cutter fuel-system maintenance and repair.\n\n"
            "Confirmed product details:\n- Product type: carburetor kit.\n- Model reference: C1Q-W40A.\n"
            "- Material listed: metal.\n\nPackage information:\n- Packed weight: 200 g.\n"
            "- Package size: 6 x 4 x 3 cm.\n\nBefore purchase, compare the model number, mounting points, "
            "fuel connections and included parts with the original equipment."
        )
        proposal = {
            "title": "Brush Cutter Carburetor Kit C1Q-W40A",
            "description": description,
            "site_titles": [],
            "methodology": {"title_basis": [], "description_basis": [], "language": "English CBT source"},
        }
        self.assertEqual(proposal_quality_issues(proposal, self.draft), [])

    def test_listing_request_uses_longer_bounded_read_timeout(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return b'{}'
        with patch("src.ai_listing_pipeline.urllib.request.urlopen", return_value=Response()) as open_request:
            call_responses_api({"base_url": "https://example.test/v1", "api_key_env": "UNUSED", "_api_key": "key"}, {})
        self.assertEqual(open_request.call_args.kwargs["timeout"], 300)

    def test_listing_read_timeout_has_a_clear_retry_message(self):
        with patch("src.ai_listing_pipeline.urllib.request.urlopen", side_effect=TimeoutError("The read operation timed out")):
            with self.assertRaisesRegex(AIListingError, "已等待 300 秒"):
                call_responses_api({"base_url": "https://example.test/v1", "api_key_env": "UNUSED", "_api_key": "key"}, {})


if __name__ == "__main__":
    unittest.main()
