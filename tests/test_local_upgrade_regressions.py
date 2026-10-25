import json,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from src import local_model_runtime as local
from src.ai_listing_pipeline import proposal_quality_issues
class UpgradeRegressionTests(unittest.TestCase):
    def proposal(self):
        return {'title':'10-inch Adjustable Wrench - Blue','description':('Adjustable wrench for fastening nuts and bolts. '*7)+'Confirmed product details: Size: 10 inches. Color: blue.','site_titles':[{'site_id':'MLM','title':'Llave ajustable azul de 10 pulgadas','language':'es'}],'methodology':{}}
    def test_selected_sites_checked_from_payload(self):
        issues=proposal_quality_issues(self.proposal(),{'payload':{'sites_to_sell':['MLM','MLB']}})
        self.assertTrue(any('MLB' in issue for issue in issues))
    def test_language_label_does_not_make_english_title_spanish(self):
        p=self.proposal();p['site_titles'][0]['title']=p['title']
        self.assertTrue(any('照搬英文' in issue for issue in proposal_quality_issues(p,{'payload':{'sites_to_sell':['MLM']}})))
    def test_unknown_packaging_stays_out_of_customer_copy(self):
        p=self.proposal();p['description']+=' Weight and dimensions are not provided in the source data.'
        self.assertTrue(any('内部证据' in issue for issue in proposal_quality_issues(p,{})))
    def test_tool_history_and_thinking_setting_survive_adapter(self):
        fake=MagicMock();fake.open.return_value.__enter__.return_value.read.return_value=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'done'}}]}).encode()
        config={'model':'local-test','base_url':'http://127.0.0.1:18080/v1','vision':False}
        body={'input':[{'role':'assistant','content':[{'type':'output_text','text':'previous answer'}]},{'type':'function_call','call_id':'call1','name':'read_product','arguments':'{}'},{'type':'function_call_output','call_id':'call1','output':'{"found":false}'}]}
        with patch.object(local,'ensure',return_value=config),patch.object(local,'opener',return_value=fake):local.responses(Path('.'),body)
        payload=json.loads(fake.open.call_args.args[0].data)
        self.assertEqual(payload['messages'][0]['content'][0]['text'],'previous answer')
        self.assertEqual(payload['messages'][2]['tool_call_id'],'call1')
        self.assertFalse(payload['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(payload['thinking_budget_tokens'],512)
if __name__=='__main__':unittest.main()
