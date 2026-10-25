import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.category_resolution_v3 import assessment, intent_for, resolve, semantic_issues
from tests.test_category_resolution_v3 import category


TITLE = '跨境热卖1件户外链条锯钢丝锯手拉锯绳锯野外生存工手工锯木伐木'
HAND = category('manual-leaf', 'Saws', 'Tools', 'Hand Tools', 'HAND_SAWS')
BLADE = category('blade-leaf', 'Saw Blades', 'Tools', 'Tool Accessories', 'HAND_SAW_BLADES')
POWER = category('power-leaf', 'Chainsaws', 'Tools', 'Garden Tools', 'ELECTRIC_CHAINSAWS')
FARM = category('farm-leaf', 'Saws', 'Industries and Offices', 'Farming', 'HAND_SAWS')


class ManualPullSawTests(unittest.TestCase):
    def test_type_mapping_uses_supplier_identity_not_model_or_measurements(self):
        from src.attribute_context_v3 import source_value_supported
        from src.round2_evidence_v3 import candidate_attributes
        definition = {'id': 'HAND_SAW_TYPE', 'name': 'Hand saw type', 'value_type': 'string',
            'values_preview': [{'id': 'rip-value', 'name': 'Rip'}]}
        supplier = {'title': TITLE, 'attributes': {'规格': '11齿链锯', '货号': '11齿链锯'}}
        rows = candidate_attributes(supplier, [definition, {'id': 'MODEL', 'value_type': 'string'}])
        self.assertEqual(rows, [{'id': 'HAND_SAW_TYPE', 'value_id': None, 'value_name': 'Manual chain saw'}])
        self.assertTrue(source_value_supported(rows[0], supplier, definition))
        self.assertFalse(source_value_supported({'id': 'HAND_SAW_TYPE', 'value_name': 'Rip'}, supplier, definition))
        self.assertFalse(source_value_supported({'id': 'MODEL', 'value_name': '11齿链锯'}, supplier))
        for unsupported in [{'title': '户外切割工具', 'attributes': {}},
                {'title': '手拉锯替换锯条配件', 'attributes': {}},
                {'title': '', 'attributes': {'商品长度': '65cm'}}]:
            self.assertFalse(candidate_attributes(unsupported, [definition]))

    def test_complete_saw_rejects_blade_power_and_farming_paths(self):
        intent = intent_for({'source_title': TITLE})
        self.assertEqual(intent['kind'], 'manual_pull_saw')
        self.assertTrue(assessment(HAND, intent)[0])
        for wrong in [BLADE, POWER, FARM]:
            self.assertFalse(assessment(wrong, intent)[0])
            self.assertTrue(semantic_issues({'evidence': {'source_title': TITLE}}, wrong))

    def test_replacement_and_starter_offers_do_not_become_complete_hand_saws(self):
        for title in ['手拉锯替换锯条配件', 'manual chain saw replacement blade', '油锯链条更换配件']:
            intent = intent_for({'source_title': title})
            self.assertNotEqual((intent or {}).get('kind'), 'manual_pull_saw')
        self.assertEqual(intent_for({'source_title': '油锯手拉启动器'})['kind'], 'recoil_starter')

    def test_wrong_prediction_recovers_hand_tool_from_current_tree(self):
        class API:
            def domain_discovery(self, site, query):
                return [{'category_id': BLADE['id']}]

            def category(self, cid):
                return copy.deepcopy({c['id']: c for c in [HAND, BLADE, POWER, FARM]}[cid])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tree = root / 'data/categories/taxonomy-CBT.json'
            tree.parent.mkdir(parents=True)
            tree.write_text(json.dumps({'queried_at': datetime.now(timezone.utc).isoformat(),
                'leaves': [HAND, BLADE, POWER, FARM]}), encoding='utf-8')
            with patch('src.category_resolution_v3.model_json', side_effect=AssertionError('No semantic guess needed')):
                result = resolve(root, {'evidence': {'source_title': TITLE}}, API())
            self.assertEqual(result['selected']['id'], HAND['id'])
            self.assertTrue(any(c['category_id'] == BLADE['id'] and not c['accepted'] for c in result['candidates']))


if __name__ == '__main__':
    unittest.main()
