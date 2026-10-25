import unittest
from unittest.mock import Mock, patch
from src.site_category_v3 import discover
from src.publish_service import validate_category_sites


def category(cid, names):
    return {'id': cid, 'name': names[-1], 'children_categories': [],
            'path_from_root': [{'name': n} for n in names],
            'settings': {'catalog_domain': cid[:3]+'-WRENCHES',
                         'listing_allowed': True, 'status': 'enabled'}}


class SiteCategoryTests(unittest.TestCase):
    def setUp(self):
        self.source = category('CBT30831', ['Tools','Hand Tools','Fixing','Wrenches'])
        self.correct = category('MLM9171', ['Herramientas','Herramientas Manuales','Fijación','Llaves'])
        self.vehicle = category('MLM438037', ['Accesorios para Vehículos','Herramientas para Vehículos','Llaves','Ajustables'])
        self.api = Mock()
        self.api.domain_discovery.return_value = [{'category_id': 'MLM438037'}, {'category_id': 'MLM9171'}]
        def get(cid):
            return {'CBT30831':self.source,'MLM9171':self.correct,'MLM438037':self.vehicle}[cid]
        self.api.category.side_effect = get

    def test_not_first_prediction_or_same_domain_only(self):
        with patch('src.site_category_v3.PATH_CANDIDATES', {}):
            self.assertEqual(discover(self.api,self.source,'MLM','wrench')['id'],'MLM9171')

    def test_missing_prefix_id_recovers_by_actual_full_path(self):
        result=validate_category_sites({'payload':{'category_id':'CBT30831','title':'Wrench',
            'sites_to_sell':[{'site_id':'MLM'}]}},self.api)
        self.assertTrue(result['valid'],result)
        self.assertEqual(result['sites'][0]['category_id'],'MLM9171')

    def test_parent_or_disabled_category_cannot_pass(self):
        for change in ({'children_categories':[{'id':'child'}]}, {'settings':{'status':'disabled'}}):
            with self.subTest(change=change):
                original=self.correct.copy(); self.correct.update(change)
                with self.assertRaises(ValueError):discover(self.api,self.source,'MLM','wrench')
                self.correct.clear(); self.correct.update(original)

    def test_unknown_path_or_domain_cannot_pass(self):
        self.correct['path_from_root'][1]['name']='Unknown'
        with self.assertRaises(ValueError):discover(self.api,self.source,'MLM','wrench')

    def test_known_candidate_is_still_refreshed_and_can_expire(self):
        self.api.category.side_effect = OSError('unavailable')
        with self.assertRaises(ValueError):discover(self.api,self.source,'MLM','wrench')
        self.api.category.assert_called_once_with('MLM9171')

    def test_ambiguous_full_paths_are_not_chosen_by_order(self):
        self.api.domain_discovery.return_value=[{'category_id':'MLM1'},{'category_id':'MLM2'}]
        self.api.category.side_effect=lambda cid: {**self.correct,'id':cid}
        with patch('src.site_category_v3.PATH_CANDIDATES', {}):
            with self.assertRaises(ValueError):discover(self.api,self.source,'MLM','wrench')

    def test_conflicting_explicit_override_is_not_silently_replaced(self):
        result=validate_category_sites({'payload':{'category_id':'CBT30831','sites_to_sell':['MLM']},
            'evidence':{'site_category_checks':{'MLM':{'category_id':'MLM438037'}}}},self.api)
        self.assertFalse(result['valid'])
        self.api.domain_discovery.assert_not_called()


if __name__=='__main__':unittest.main()
