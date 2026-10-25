import copy
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from src import product_match_v3 as match
from src import sku_processing_v3 as processing


class MatchingTests(unittest.TestCase):
    def sample(self, model='VF5000', variant='单个装'):
        sku = {'sku_id':'S1', 'variant':variant, 'supplier_facts':{'attributes':{'型号':model}}}
        ref = {'market_item_id':'MLM1234567', 'title':'Paquete De 2 Filtros VF3500', 'spec_match':'SOURCE_SELECTED_PENDING_REVIEW'}
        return {'payload':{}, 'evidence':{'sku_details':[sku], 'market_reference':ref}}, sku

    def test_model_and_bundle_conflicts_are_separate(self):
        d, s = self.sample()
        r = match.assess(d, s)
        self.assertEqual(r['state'], 'CONFLICT')
        self.assertTrue({'REFERENCE_MODEL_CONFLICT','REFERENCE_PACK_CONFLICT'}.issubset({i['code'] for i in r['issues']}))

    def test_single_sku_is_not_a_one_unit_pack(self):
        d, s = self.sample('VF3500', '无规格选项单品')
        r = match.assess(d, s)
        self.assertEqual(r['state'], 'CANDIDATE')
        self.assertIsNone(r['comparisons'][-1]['supplier_value'])

    def test_human_confirmation_preserved_but_conflict_still_visible(self):
        d, s = self.sample()
        d['evidence']['market_reference']['spec_match'] = 'HUMAN_CONFIRMED_SAME_SKU'
        before = copy.deepcopy(d)
        r = match.refresh(d)
        self.assertEqual(r['rows'][0]['state'], 'CONFLICT')
        self.assertEqual(d['evidence']['market_reference'], before['evidence']['market_reference'])
        self.assertEqual(d['evidence']['sku_details'], before['evidence']['sku_details'])

    def test_redirect_is_not_original_listing(self):
        d, s = self.sample('VF3500', '2件装')
        d['evidence']['market_reference'].update(spec_match='HUMAN_CONFIRMED_SAME_SKU', resolved_item_id='MLM7654321')
        self.assertEqual(match.assess(d, s)['issues'][0]['code'], 'REFERENCE_REDIRECT_CONFLICT')

    def test_shared_reference_does_not_bind_multiple_skus(self):
        d, s = self.sample('VF3500', '2件装')
        d['evidence']['market_reference']['spec_match']='HUMAN_CONFIRMED_SAME_SKU'
        d['evidence']['sku_details'].append({'sku_id':'S2'})
        self.assertEqual(match.assess(d,s)['state'],'CANDIDATE')

    def test_verified_flag_needs_actual_scoped_evidence(self):
        d,s=self.sample('VF3500','2件装');ref=d['evidence']['market_reference']
        ref['spec_match']='VERIFIED_SAME_SKU'
        self.assertEqual(match.assess(d,s)['state'],'CANDIDATE')
        d['evidence']['task_evidence']={'source_sha256':'snapshot'}
        ref.update(source_sku_id='S1',source_capture_sha256='snapshot',required_identity_fields=['model','units_per_pack'],identity_evidence=[{'field':k,'supplier_value':v,'market_value':v,'supplier_source':'raw/sku/S1','market_source':'market/MLM1234567'} for k,v in [('model','VF3500'),('units_per_pack',2)]])
        self.assertEqual(match.assess(d,s)['state'],'CONFIRMED')

    def test_no_reference_keeps_procurement_only_processing(self):
        d,s=self.sample();d['evidence'].pop('market_reference')
        self.assertEqual(match.assess(d,s)['state'],'NOT_LINKED')
        self.assertEqual(match.blocking_issues(d),[])

    def test_conflict_stops_before_expensive_stages_preserves_output(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root=Path(folder);path=root/'drafts/test-draft.json';path.parent.mkdir()
            d,s=self.sample();s['ai_content']={'description':'valid prior content'}
            path.write_text(json.dumps(d),encoding='utf-8')
            job=root/'jobs/job.json';job.parent.mkdir();job.write_text('{}')
            read=lambda p:json.loads(p.read_text('utf-8'))
            legacy=types.SimpleNamespace(DRAFTS_DIR=path.parent,ROOT=root,read_json=read,publish_preflight_issues=lambda d:[],
                query_official_category=Mock(),discover_official_category=Mock(),_apply_ai_content_and_attributes=Mock())
            def save_job(p,j,**kw):
                j.update(kw);p.write_text(json.dumps(j),encoding='utf-8')
            legacy._save_full_job=save_job
            processing.install(legacy)
            with patch('src.task_evidence_v3.restore',return_value=[]),patch('src.round2_evidence_v3.recover',return_value=[]):
                legacy._run_full_ai_flow(path,root,job)
            self.assertEqual(read(job)['completion_state'],'NEEDS_MATCH')
            self.assertEqual(read(path)['evidence']['sku_details'][0]['ai_content'],s['ai_content'])
            legacy.query_official_category.assert_not_called()
            legacy._apply_ai_content_and_attributes.assert_not_called()


if __name__=='__main__':unittest.main()
