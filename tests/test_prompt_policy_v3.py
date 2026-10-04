import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.prompt_policy_v3 import build_image_prompts, LISTING_INSTRUCTIONS, ATTRIBUTE_INSTRUCTIONS
from src.flow_guards_v3 import install, selected_context
from src.flow_resume_v3 import text_signature
from src import ai_listing_pipeline


def draft():
    return {'payload': {'title': 'English title 250ml', 'category_id': 'A', 'sites_to_sell': [{'site_id': 'MLB', 'title': 'Pulverizador 250ml'}, {'site_id': 'MLM', 'title': 'Pulverizador manual 250ml'}]}, 'evidence': {'current_pricing_sku_id': '250', 'sku_details': [{'sku_id': '250', 'variant': '250ML+伸缩杆'}, {'sku_id': '500', 'variant': '500ML'}], 'supplier_page': {'title': '手动授粉器'}, 'packed_weight': {'value': 100, 'status': 'USER_INPUT_UNVERIFIED'}}}


class PromptTests(unittest.TestCase):
    def test_default_two_images_have_no_ad_text_or_forced_repair_scene(self):
        d = draft(); original = copy.deepcopy(d)
        rows = build_image_prompts(d)
        self.assertEqual(len(rows), 2); self.assertEqual(d, original)
        self.assertTrue(all(r['language'] == 'none' for r in rows))
        self.assertNotIn('English title', rows[1]['prompt'])
        self.assertIn('没有明确使用场景证据时不安排', rows[1]['prompt'])
        self.assertIn('250ML+伸缩杆', rows[0]['prompt']); self.assertNotIn('500ML', rows[0]['prompt'])

    def test_market_title_uses_requested_language_not_first_site(self):
        rows = build_image_prompts(draft(), 'es', {'image_count': 3, 'main_background': 'white', 'image_text': 'market'})
        self.assertEqual(rows[0]['language'], 'none')
        self.assertIn('Pulverizador manual 250ml', rows[1]['prompt'])
        self.assertNotIn('Pulverizador 250ml', rows[1]['prompt'])
        self.assertNotIn('English title', rows[1]['prompt'])

    def test_ambiguous_regional_titles_fall_back_to_no_text(self):
        d = draft(); d['payload']['sites_to_sell'].append({'site_id': 'MLC', 'title': 'Otro título'})
        rows = build_image_prompts(d, 'es', {'image_count': 2, 'main_background': 'white', 'image_text': 'market'})
        self.assertEqual(rows[1]['language'], 'none')

    def test_actual_request_builders_use_new_instructions_without_api_calls(self):
        requests = []
        response = {'output': [{'content': [{'text': json.dumps({'attributes': [], 'unresolved': []})}]}]}
        fake = SimpleNamespace(publish_preflight_issues=lambda d: [], _prepare_multi_sku_ai_workflow=lambda d: {}, generate_quality_checked_proposal=lambda *a: {}, call_responses_api=lambda config, req: requests.append(req) or response, start_sku_image_generation=lambda *a: {}, _output_text=lambda r: r['output'][0]['content'][0]['text'], EditorError=ValueError, DRAFTS_DIR=Path('.'), ROOT=Path('.'))
        with patch.object(ai_listing_pipeline, 'LISTING_INSTRUCTIONS'), patch.object(ai_listing_pipeline, 'sanitized_product_context'), patch.object(ai_listing_pipeline, '_input_images'):
            ai_listing_pipeline.sanitized_product_context = lambda d: {}
            install(fake, ai_listing_pipeline)
            request = ai_listing_pipeline.build_responses_request(draft(), 'test')
            self.assertEqual(request['instructions'], LISTING_INSTRUCTIONS)
            fake.call_responses_api({}, {'text': {'format': {'name': 'category_attributes'}}, 'input': '{"existing_attributes":[]}'})
            self.assertEqual(requests[0]['instructions'], ATTRIBUTE_INSTRUCTIONS)

    def test_packaging_status_and_conflicts_reach_copy_request(self):
        d = draft(); d['evidence']['packaging_conflict'] = {'status': 'UNRESOLVED'}
        context = selected_context(d, {})
        self.assertEqual(context['packaging_evidence']['weight']['status'], 'USER_INPUT_UNVERIFIED')
        self.assertEqual(context['packaging_conflict']['status'], 'UNRESOLVED')

    def test_prompt_version_and_category_change_invalidate_checkpoint(self):
        d = draft(); before = text_signature(d, {})
        with patch('src.prompt_policy_v3.PROMPT_VERSION', 'new'):
            self.assertNotEqual(before, text_signature(d, {}))
        d['payload']['category_id'] = 'B'
        self.assertNotEqual(before, text_signature(d, {}))


if __name__ == '__main__': unittest.main()
