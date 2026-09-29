import os
import unittest

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.image_generation import ImageGenerationError, _persist_sku_galleries, build_image_prompts, build_sku_image_prompts, call_image_api, resolve_market_language


class ImageGenerationTests(unittest.TestCase):
    def setUp(self):
        self.draft = {
            "payload": {"title": "Carburetor Kit", "attributes": [{"id": "MODEL", "value_name": "FS120"}]},
            "evidence": {"compatibility_evidence": "FS120 supplier table"},
        }

    def test_prompt_set_has_three_distinct_roles(self):
        prompts = build_image_prompts(self.draft, "es")
        self.assertEqual([row["kind"] for row in prompts], ["main", "scene", "infographic"])
        self.assertEqual(prompts[0]["language"], "none")
        self.assertEqual(prompts[1]["language"], "es")
        self.assertIn("不得复用同一机位", prompts[2]["prompt"])

    def test_auto_language_uses_spanish_except_brazil_only(self):
        self.assertEqual(resolve_market_language({"payload": {"sites_to_sell": []}}, "auto"), "es")
        self.assertEqual(resolve_market_language({"payload": {"sites_to_sell": ["MLB"]}}, "auto"), "pt")
        self.assertEqual(resolve_market_language({"payload": {"sites_to_sell": ["MLB", "MLM"]}}, "auto"), "none")

    def test_portuguese_prompt_forbids_other_languages(self):
        prompt = build_image_prompts(self.draft, "pt")[2]["prompt"]
        self.assertIn("葡萄牙语", prompt)
        self.assertIn("不得出现中文、英文", prompt)

    def test_missing_key_stops_before_paid_request(self):
        env_name = "YOYOU_TEST_IMAGE_KEY_MISSING"
        os.environ.pop(env_name, None)
        with self.assertRaises(ImageGenerationError):
            call_image_api({"endpoint": "https://example.invalid/images", "model": "test", "api_key_env": env_name}, "x", [])

    def test_relative_async_poll_url_is_resolved_against_endpoint(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, *_args): return json.dumps(self.payload).encode("utf-8")

        submitted = Response({"task_id": "task-1", "poll_url": "/v1/images/tasks/task-1"})
        with patch("src.image_generation.urlopen", return_value=submitted), \
             patch("src.image_generation.time.sleep"), \
             patch("src.image_generation._json_request", return_value={"data": [{"b64_json": "iVBORw0KGgo="}]}) as poll:
            body, meta = call_image_api({
                "endpoint": "https://api.example.com/v1/images/edits/async", "model": "test",
                "api_key_env": "UNUSED", "_api_key": "secret", "poll_interval_seconds": 0,
            }, "prompt", [])
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(meta["task_id"], "task-1")
        poll.assert_called_once_with("https://api.example.com/v1/images/tasks/task-1", "secret")

    def test_multi_sku_prompts_are_one_text_free_hero_per_sku(self):
        self.draft["evidence"]["supplier_page"] = {"skus": [
            {"sku_id": "1", "variant": "14件套"}, {"sku_id": "2", "variant": "15件套"}
        ]}
        prompts = build_sku_image_prompts(self.draft)
        self.assertEqual([row["sku_id"] for row in prompts], ["1", "2"])
        self.assertTrue(all(row["language"] == "none" and "不得从其他规格借用" in row["prompt"] for row in prompts))

    def test_completed_sku_job_composes_hero_then_shared_gallery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "drafts").mkdir()
            draft_path = root / "drafts" / "sample.json"
            draft_path.write_text(json.dumps({"evidence": {"image_review": {"gallery": [
                {"remote_url": "https://example.com/common.jpg", "source": "source_gallery"}
            ], "sku_source_galleries": [{"sku_id": "1", "variant": "14件套", "gallery": [
                {"remote_url": "https://example.com/sku-detail.jpg", "source": "sku_source"}
            ]}]}}}), encoding="utf-8")
            job = {"items": [{"status": "done", "local_file": "ai-generated/sample/01.png", "sku_id": "1", "variant": "14件套"}]}
            _persist_sku_galleries(root, "sample.json", job)
            saved = json.loads(draft_path.read_text(encoding="utf-8"))
            gallery = saved["evidence"]["image_review"]["sku_listing_galleries"][0]["gallery"]
            self.assertEqual(gallery[0]["source"], "sku_ai_generated")
            self.assertEqual(gallery[1]["remote_url"], "https://example.com/sku-detail.jpg")
            self.assertEqual(gallery[2]["remote_url"], "https://example.com/common.jpg")
            self.assertTrue(list((root / "draft_versions").glob("*-before-sku-images-*.json")))


if __name__ == "__main__":
    unittest.main()
