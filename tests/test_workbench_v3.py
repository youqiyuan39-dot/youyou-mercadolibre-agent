import copy
import io
import json
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from src.product_core_v3 import OfflineSecondPlatformAdapter, clean_description, product_from_draft, review_issues, target_identity
from src.workbench_v3 import create_product, export_xlsx, import_csv, save_template, templates
from src.flow_guards_v3 import selected_context, freeze_selected, install
from src import ai_listing_pipeline
from src.generation_strategy_v3 import apply as apply_generation, validate as validate_generation
from src.flow_resume_v3 import merge_completed, image_signature, text_signature
from src import launch_workbench_v3


def sample():
    return {"payload": {"title": "500ml Manual Powder Sprayer", "attributes": [{"id": "GTIN", "value_name": "2621186952983", "source": "auto_generated"}]}, "evidence": {"current_pricing_sku_id": "250", "sku_details": [{"sku_id": "250", "variant": "250ML+伸缩杆", "package": {"weight_g": 100}}, {"sku_id": "500", "variant": "500ML"}], "packed_weight": {"value": 93, "status": "AI_ESTIMATED"}, "supplier_page": {"title": "手动喷粉器", "description": "粉末喷洒工具【平台活动下价格】平台声明"}}, "edit_metadata": {"field_locks": {"weight": True}}}


class CoreTests(unittest.TestCase):
    def test_launcher_reuses_verified_instance_without_spawning(self):
        identity = {'version': '0.3.0-evidence', 'workspace': str(launch_workbench_v3.ROOT)}
        with patch.object(launch_workbench_v3, 'identity', return_value=identity), patch.object(launch_workbench_v3.webbrowser, 'open') as opened, patch.object(launch_workbench_v3.subprocess, 'Popen') as spawn:
            launch_workbench_v3.main(); launch_workbench_v3.main()
            self.assertEqual(opened.call_count, 2); spawn.assert_not_called()

    def test_launcher_refuses_wrong_workbench_on_same_port(self):
        with patch.object(launch_workbench_v3, 'identity', return_value={'version': 'other', 'workspace': str(launch_workbench_v3.ROOT)}), patch.object(launch_workbench_v3.webbrowser, 'open') as opened:
            with self.assertRaises(RuntimeError): launch_workbench_v3.main()
            opened.assert_not_called()
    def test_ai_outputs_do_not_become_supplier_facts(self):
        d = sample(); d['evidence']['supplier_page']['description'] = ''
        before = product_from_draft(d)
        d['payload']['description'] = 'AI assertion is not supplier evidence'
        d['evidence']['image_review'] = {'generated_gallery': [{'local_file': 'generated/a.png'}], 'gallery': [{'local_file': 'generated/a.png'}]}
        after = product_from_draft(d)
        self.assertEqual(before['facts_version'], after['facts_version'])
        self.assertEqual(after['facts']['description'], '')

    def test_image_resume_reuses_only_matching_existing_successes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'assets').mkdir(); (root/'assets'/'done.jpg').write_bytes(b'actual')
            generated = [{'kind': 'main', 'label': '主图'}, {'kind': 'scene', 'label': '场景'}]
            old = {'input_signature': 'same', 'items': [{'index': 0, 'kind': 'main', 'status': 'done', 'local_file': 'done.jpg'}, {'index': 1, 'kind': 'scene', 'status': 'failed'}]}
            result = merge_completed(old, generated, root, 'same')
            self.assertEqual([r['status'] for r in result], ['done', 'queued'])
            self.assertTrue(result[0]['reused'])
            self.assertEqual([r['status'] for r in merge_completed(old, generated, root, 'changed')], ['queued', 'queued'])
            (root/'assets'/'done.jpg').unlink()
            self.assertEqual(merge_completed(old, generated, root, 'same')[0]['status'], 'queued')

    def test_image_resume_invalidated_by_actual_reference_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder)/'product.jpg'; image.write_bytes(b'250ml')
            before = image_signature([{'prompt': 'same'}], [image], {'model': 'A'})
            image.write_bytes(b'500ml')
            after = image_signature([{'prompt': 'same'}], [image], {'model': 'A'})
            self.assertNotEqual(before, after)

    def test_text_resume_invalidated_by_template_and_selected_facts(self):
        d = sample(); settings = {'roles': {'vision': {'model': 'A'}}}
        before = text_signature(d, settings)
        d['evidence']['prompt_template'] = {'id': 'T', 'version': 2}
        self.assertNotEqual(before, text_signature(d, settings))
        d = sample(); d['evidence']['current_pricing_sku_id'] = '500'
        self.assertNotEqual(before, text_signature(d, settings))

    def test_generation_zero_two_and_ten_preserve_original_prompts(self):
        prompts = [{'kind': 'main', 'prompt': 'Keep the exact product', 'label': '主图'}, {'kind': 'scene', 'prompt': 'Same product in use', 'label': '场景图'}, {'kind': 'infographic', 'prompt': 'Show evidence', 'label': '细节图'}]
        before = copy.deepcopy(prompts)
        for count in [0, 2, 10]:
            result = apply_generation(prompts, {'image_count': count, 'main_background': 'white'})
            self.assertEqual(len(result), count)
            self.assertEqual(len({r['kind'] for r in result}), count)
        self.assertEqual(prompts, before)
        for count in [-1, 11, True, 1.5]:
            with self.assertRaises(ValueError): validate_generation({'image_count': count, 'main_background': 'white'})

    def test_local_asset_reference_cannot_escape_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'assets').mkdir(); (root / 'other.jpg').write_bytes(b'test')
            with self.assertRaises(ValueError): create_product(root, {'title': '测试', 'images': [{'url': '/assets/../other.jpg'}]})

    def test_runtime_rejects_unbound_sku_before_paid_model(self):
        called = []
        fake = SimpleNamespace(publish_preflight_issues=lambda d: [], _prepare_multi_sku_ai_workflow=lambda d: {}, generate_quality_checked_proposal=lambda *a: called.append(a), call_responses_api=lambda *a: {}, start_sku_image_generation=lambda *a: {}, EditorError=ValueError, DRAFTS_DIR=Path('.'), ROOT=Path('.'))
        pipeline = SimpleNamespace(sanitized_product_context=lambda d: {}, _input_images=lambda d: [])
        install(fake, pipeline)
        with self.assertRaisesRegex(ValueError, "专属原图"): fake.generate_quality_checked_proposal({}, sample(), 'mock')
        self.assertEqual(called, [])
        d = sample(); d['payload']['available_quantity'] = None
        fake.apply_inventory_rule(d)
        self.assertIsNone(d['payload']['available_quantity'])

    def test_attribute_model_cannot_invent_barcode(self):
        raw = {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({'attributes': [{'id': 'GTIN', 'value_name': '2621186952983'}, {'id': 'COLOR', 'value_name': 'Blue'}], 'unresolved': []})}]}]}
        fake = SimpleNamespace(publish_preflight_issues=lambda d: [], _prepare_multi_sku_ai_workflow=lambda d: {}, generate_quality_checked_proposal=lambda *a: {}, call_responses_api=lambda *a: raw, start_sku_image_generation=lambda *a: {}, _output_text=lambda r: r['output'][0]['content'][0]['text'], EditorError=ValueError, DRAFTS_DIR=Path('.'), ROOT=Path('.'))
        pipeline = SimpleNamespace(sanitized_product_context=lambda d: {}, _input_images=lambda d: [])
        install(fake, pipeline)
        result = fake.call_responses_api({}, {'text': {'format': {'name': 'category_attributes'}}, 'input': json.dumps({'existing_attributes': []})})
        data = json.loads(fake._output_text(result))
        self.assertEqual([a['id'] for a in data['attributes']], ['COLOR'])
        self.assertEqual(data['unresolved'], ['GTIN'])
        self.assertIn('GTIN', raw['output'][0]['content'][0]['text'])

    def test_actual_request_context_selects_sku_and_records_template(self):
        d = sample()
        d["evidence"]["prompt_template"] = {"id": "short", "version": 2, "content": "Use short sentences"}
        d["evidence"]["supplier_page"]["skus"] = d["evidence"]["sku_details"]
        before = copy.deepcopy(d)
        original = ai_listing_pipeline.sanitized_product_context
        with patch.object(ai_listing_pipeline, "sanitized_product_context", side_effect=lambda value: selected_context(value, original(value))):
            request = ai_listing_pipeline.build_responses_request(d, "test-model")
        context = json.loads(request["input"][0]["content"][0]["text"])
        self.assertEqual([s["sku_id"] for s in context["supplier_skus"]], ["250"])
        self.assertEqual(context["editorial_template"]["version"], 2)
        self.assertNotIn("价格", context["supplier_description"])
        self.assertEqual(d, before)

    def test_freeze_preserves_locks_zero_and_unknown_inventory(self):
        from src.draft_editor import _prepare_multi_sku_ai_workflow
        d = sample(); d["evidence"]["sku_details"][0]["available_quantity"] = 0
        freeze_selected(d, _prepare_multi_sku_ai_workflow)
        self.assertEqual(d["payload"]["available_quantity"], 0)
        self.assertEqual(d["evidence"]["packed_weight"]["value"], 93)
        d["evidence"]["sku_details"][0].pop("available_quantity")
        freeze_selected(d, _prepare_multi_sku_ai_workflow)
        self.assertIsNone(d["payload"]["available_quantity"])

    def test_freeze_rejects_missing_selection_before_mutation(self):
        from src.draft_editor import _prepare_multi_sku_ai_workflow
        d = sample(); d["evidence"].pop("current_pricing_sku_id"); before = copy.deepcopy(d)
        with self.assertRaises(ValueError): freeze_selected(d, _prepare_multi_sku_ai_workflow)
        self.assertEqual(d, before)

    def test_observed_sku_and_auto_barcode_are_blocked(self):
        issues = review_issues(sample())
        self.assertTrue(any("容量" in i for i in issues))
        self.assertTrue(any("条码" in i for i in issues))
        self.assertTrue(any("重量" in i for i in issues))

    def test_valid_selected_capacity_and_evidenced_barcode(self):
        d = sample(); d["payload"]["title"] = "250ml Manual Powder Sprayer"
        d["payload"]["attributes"] = [{"id": "GTIN", "value_name": "1234567890123"}]
        d["evidence"]["gtin"] = "1234567890123"
        d["evidence"]["packed_weight"]["status"] = "SUPPLIER_PACKAGING_INFO"
        self.assertEqual(review_issues(d), [])

    def test_site_title_and_capacity_attribute_checked(self):
        d = sample(); d["payload"] = {"title": "250ml Sprayer", "sites_to_sell": [{"site_id": "MLM", "title": "500 ml rociador"}], "attributes": [{"id": "TOTAL_CAPACITY", "value_name": "500 ml"}]}
        issues = review_issues(d)
        self.assertTrue(any("站点标题" in i for i in issues)); self.assertTrue(any("容量属性" in i for i in issues))

    def test_capacity_unit_and_spanish_decimal_conversion(self):
        d = sample(); d['payload'] = {'title': '0,5 litros Sprayer'}
        self.assertTrue(any('容量' in i for i in review_issues(d)))
        d['payload']['title'] = '0.25 L Sprayer'
        self.assertFalse(any('容量' in i for i in review_issues(d)))

    def test_pending_no_barcode_reason_stays_blocked(self):
        d = sample(); d['evidence']['gtin_or_exemption'] = 'NO_GTIN_PENDING_CATEGORY_CHECK'
        self.assertTrue(any('无条码' in i for i in review_issues(d)))

    def test_unknown_selected_sku_does_not_fall_back_silently(self):
        d = sample(); d["evidence"]["current_pricing_sku_id"] = "unknown"
        self.assertTrue(any("不在" in i for i in review_issues(d)))

    def test_adapters_do_not_change_original_facts_or_locks(self):
        d = sample(); before = copy.deepcopy(d); product = product_from_draft(d); frozen = copy.deepcopy(product)
        a = OfflineSecondPlatformAdapter().preview(product, {"title_limit": 5, "store_id": "A"})
        b = OfflineSecondPlatformAdapter().preview(product, {"title_limit": 20, "store_id": "B"})
        self.assertEqual(product, frozen); self.assertEqual(d, before)
        self.assertNotEqual(a["store_id"], b["store_id"])
        self.assertEqual(a["facts_version"], b["facts_version"])
        self.assertEqual(product["facts"]["locks"], {"weight": True})

    def test_target_key_separates_store_and_market(self):
        p = product_from_draft(sample())
        keys = {target_identity(p, "mercadolibre", s, m) for s in ["A", "B"] for m in ["MLM", "MLB"]}
        self.assertEqual(len(keys), 4)
        with self.assertRaises(ValueError): target_identity(p, "mercadolibre", "", "MLM")

    def test_target_key_changes_when_output_content_changes(self):
        d = sample(); before = product_from_draft(d)
        d['payload']['title'] = '250ml Verified Sprayer'; after = product_from_draft(d)
        self.assertEqual(before['facts_version'], after['facts_version'])
        self.assertNotEqual(target_identity(before, 'mercadolibre', 'A', 'MLM'), target_identity(after, 'mercadolibre', 'A', 'MLM'))

    def test_raw_text_not_mutated_by_cleaning(self):
        raw = "商品用途【平台活动下价格】价格说明"
        self.assertEqual(clean_description(raw), "商品用途"); self.assertIn("价格说明", raw)


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_multisku_images_are_bound_without_copying_other_sku(self):
        r = create_product(self.root, {"title": "喷粉器", "mode": "multi", "images": [{"url": "https://example.com/250.png"}, {"url": "https://example.com/500.png"}], "skus": [{"variant": "250ml", "image_indices": [0]}, {"variant": "500ml", "image_indices": [1]}]})
        d = json.loads((self.root / "drafts" / r["name"]).read_text(encoding="utf-8"))
        galleries = d["evidence"]["image_review"]["sku_source_galleries"]
        self.assertNotEqual(galleries[0]["gallery"][0]["remote_url"], galleries[1]["gallery"][0]["remote_url"])
        self.assertFalse(d["human_approved"]); self.assertEqual(d["payload"]["available_quantity"], None)
        self.assertEqual(d["payload"]["attributes"], [])

    def test_missing_multisku_image_binding_rejected(self):
        with self.assertRaises(ValueError): create_product(self.root, {"title": "喷粉器", "mode": "multi", "images": [{"url": "https://example.com/a.png"}], "skus": [{"variant": "250ml", "image_indices": [3]}]})

    def test_bundle_requires_real_components(self):
        with self.assertRaises(ValueError): create_product(self.root, {"title": "套装", "mode": "bundle", "images": [{"url": "https://example.com/a.png"}]})

    def test_csv_one_bad_row_does_not_hide_success(self):
        result = import_csv(self.root, "title,image_urls\n喷粉器,https://example.com/a.png\n坏图片,http://example.com/a.png\n")
        self.assertEqual([r["status"] for r in result["results"]], ["created", "failed"])
        self.assertEqual(result["results"][1]["row"], 3)

    def test_csv_size_limit_checked_before_any_creates(self):
        with self.assertRaises(ValueError): import_csv(self.root, "title,image_urls\n" + "喷粉器,https://example.com/a.png\n" * 101)
        self.assertFalse((self.root / "drafts").exists())

    def test_template_history_and_recoverable_archive(self):
        first = save_template(self.root, {"name": "商品识别", "content": "只使用当前SKU证据"})
        second = save_template(self.root, {**first, "content": "识别当前SKU；冲突留空"})
        self.assertEqual(second["version"], 2); self.assertEqual(second["previous"][0]["content"], first["content"])
        save_template(self.root, {**second, "deleted": True}); self.assertEqual(templates(self.root), [])
        recovered = save_template(self.root, {**second, "deleted": False}); self.assertEqual(recovered["version"], 4)

    def test_xlsx_is_well_formed_and_preserves_unicode_and_formula_as_text(self):
        data = export_xlsx([{"name": "喷粉器-draft.json", "title": "=HYPERLINK(危险)", "evidence_issues": ["容量不符"]}])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist(): ET.fromstring(z.read(name))
            xml = z.read("xl/worksheets/sheet1.xml").decode()
            self.assertIn("容量不符", xml); self.assertIn("=HYPERLINK", xml); self.assertNotIn("<f>", xml)


if __name__ == "__main__": unittest.main()
