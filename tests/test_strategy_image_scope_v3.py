import json,tempfile,unittest,types
from pathlib import Path
from unittest.mock import patch
from src import source_image_screening_v3 as s

class ScopeTests(unittest.TestCase):
    def test_partial_retry_does_not_replace_other_sku_gallery(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as f:
            source={'remote_url':'https://cbu01.alicdn.com/a.jpg'}
            other={'sku_id':'B','gallery':[{'local_file':'human.jpg','review_status':'HUMAN_SELECTED'}]}
            draft={'evidence':{'sku_details':[{'sku_id':'A'},{'sku_id':'B'}],'image_review':{'sku_source_galleries':[{'sku_id':'A','gallery':[source]}],'sku_listing_galleries':[other]}}}
            with patch('src.source_image_resolution_v3.recover_image',side_effect=lambda r,i:(i,{})):
                s.screen(Path(f),draft,[{'sku_id':'A'}],inspector=lambda *a:{'usable':True,'contains_watermark':False,'wrong_variant':False,'uncertain':False,'fingerprint':'A'})
            self.assertIn(other,draft['evidence']['image_review']['sku_listing_galleries'])
            self.assertEqual(draft['evidence']['image_review'].get('gallery') or [],[])

    def test_recovered_asset_does_not_redownload_its_own_thumbnail(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as f:
            root=Path(f);url='https://cbu01.alicdn.com/a.jpg_sum.jpg'
            draft={'evidence':{'image_review':{'sku_source_galleries':[{'sku_id':'A','gallery':[{'remote_url':url}]}]}}}
            verdict={'usable':True,'reason':'clear','contains_watermark':False,'wrong_variant':False,'uncertain':False}
            response={'output':[{'content':[{'text':json.dumps(verdict)}]}]}
            with patch.object(s,'_image_bytes',return_value=(b'jpeg','a.jpg','image/jpeg')) as reads,patch('src.local_model_runtime.responses',return_value=response) as model:
                s.inspect_image(root,draft,{'sku_id':'A'},{'remote_url':url,'local_file':'resolved/high.jpg'})
            self.assertEqual(reads.call_count,1)
            body=model.call_args.args[1]
            self.assertEqual(sum(p['type']=='input_image' for p in body['input'][0]['content']),1)

    def test_three_secondary_candidates_do_not_replace_a_main_image(self):
        from src.sku_processing_v3 import install
        legacy=types.SimpleNamespace(DRAFTS_DIR=Path('drafts'),ROOT=Path('.'),publish_preflight_issues=lambda d:[])
        install(legacy)
        draft={'evidence':{'sku_details':[{'sku_id':'A'}],'image_review':{'sku_listing_galleries':[{'sku_id':'A','gallery':[{'source_listing_role':'secondary','review_status':'AUTO_SCREENED'} for _ in range(3)]}]}}}
        self.assertTrue(any('仍缺清晰主图' in x for x in legacy.publish_preflight_issues(draft)))

if __name__=='__main__':unittest.main()
