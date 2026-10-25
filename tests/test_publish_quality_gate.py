"""Keep incomplete product facts and image sets out of the publish confirmation."""
import copy
import unittest

from src.draft_editor import publish_preflight_issues


def draft_with_selected_images():
    return {
        "payload": {
            "title": "Recoil starter", "category_id": "CBT455591",
            "available_quantity": 200,
            "seller_sku": "TEST-RECOIL-1",
            "sites_to_sell": [{"site_id": "MLM", "net_proceeds": 4.78}],
            "attributes": [
                {"id": "BRAND", "value_name": "Generic"},
                {"id": "MODEL", "value_name": "Known model"},
            ],
        },
        "evidence": {
            "images_human_reviewed": True,
            "image_review": {"gallery": [
                {"local_file": f"generated/{i}.png", "review_status": "HUMAN_SELECTED"}
                for i in range(3)
            ]},
        },
    }


class PublishQualityGateTests(unittest.TestCase):
    def test_three_distinct_selected_images_pass(self):
        self.assertEqual(publish_preflight_issues(draft_with_selected_images()), [])

    def test_two_images_cannot_reach_publish_confirmation(self):
        draft = draft_with_selected_images()
        draft["evidence"]["image_review"]["gallery"].pop()
        self.assertTrue(any("至少需要 3 张" in issue for issue in publish_preflight_issues(draft)))

    def test_three_configured_images_do_not_require_extra_review_flags(self):
        draft = draft_with_selected_images()
        draft["evidence"]["images_human_reviewed"] = False
        for image in draft["evidence"]["image_review"]["gallery"]:
            image["review_status"] = "UNREVIEWED"
        self.assertEqual(publish_preflight_issues(draft), [])

    def test_duplicate_images_do_not_inflate_configured_count(self):
        draft = draft_with_selected_images()
        gallery = draft["evidence"]["image_review"]["gallery"]
        gallery[1]["local_file"] = gallery[0]["local_file"]
        gallery[2]["review_status"] = "UNREVIEWED"
        self.assertTrue(any("当前已配置 2 张" in issue for issue in publish_preflight_issues(draft)))

    def test_known_rejected_image_cannot_be_submitted(self):
        draft = draft_with_selected_images()
        draft["evidence"]["image_review"]["gallery"].append(
            {"local_file": "generated/rejected.png", "review_status": "REJECTED"})
        self.assertTrue(any("已否决图片" in issue for issue in publish_preflight_issues(draft)))

    def test_each_sku_uses_its_actual_gallery_not_the_common_count(self):
        draft = draft_with_selected_images()
        draft["evidence"]["sku_images_human_reviewed"] = False
        draft["evidence"]["sku_details"] = [
            {"sku_id": sku, "seller_sku": sku, "available_quantity": 200,
             "variation_attributes": [{"name": "Packaging", "value": sku}],
             "site_pricing": [{"site_id": "MLM", "net_proceeds": 4.78}]}
            for sku in ("A", "B")]
        galleries = [{"sku_id": sku, "gallery": [
            {"local_file": f"generated/{sku}-{i}.png", "review_status": "UNREVIEWED"}
            for i in range(3)]} for sku in ("A", "B")]
        draft["evidence"]["image_review"]["sku_listing_galleries"] = galleries
        self.assertEqual(publish_preflight_issues(draft), [])
        galleries[1]["gallery"].pop()
        issues = publish_preflight_issues(draft)
        self.assertEqual(len(issues), 1)
        self.assertIn("SKU B", issues[0])
        self.assertIn("当前已配置 2 张", issues[0])

    def test_blank_brand_and_model_are_not_invented(self):
        draft = copy.deepcopy(draft_with_selected_images())
        for row in draft["payload"]["attributes"]:
            row["value_name"] = ""
        issues = publish_preflight_issues(draft)
        self.assertTrue(any("品牌未填写" in issue for issue in issues))
        self.assertTrue(any("型号未填写" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
