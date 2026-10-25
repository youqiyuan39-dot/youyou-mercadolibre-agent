import json,unittest,tempfile
from pathlib import Path
from unittest.mock import patch,MagicMock
from src import local_model_runtime as local,model_settings as settings
from src.ai_listing_pipeline import call_responses_api,AIListingError
class CostBoundaryTests(unittest.TestCase):
    def config(self,vision=False):return {'model':'local-test','base_url':'http://127.0.0.1:18080/v1','vision':vision}
    def test_old_paid_config_always_routes_local_without_key(self):
        with patch.object(local,'responses',return_value={'paid_api_used':False}) as run:
            result=call_responses_api({'base_url':'https://paid.example/v1'}, {'input':'x'})
        self.assertFalse(result['paid_api_used']);run.assert_called_once()
    def test_no_paid_fallback_when_local_fails(self):
        with patch.object(local,'responses',side_effect=local.LocalModelError('offline')),patch('urllib.request.urlopen') as paid:
            with self.assertRaises(AIListingError):call_responses_api({'base_url':'https://paid.example/v1'}, {})
        paid.assert_not_called()
    def test_text_only_model_rejects_images_explicitly(self):
        with patch.object(local,'ensure',return_value=self.config()),patch.object(local,'opener') as transport:
            with self.assertRaisesRegex(local.LocalModelError,'不能看图'):local.responses(Path('.'),{'input':[{'role':'user','content':[{'type':'input_image','image_url':'https://example.invalid/image.png'}]}]})
        transport.assert_not_called()
    def test_local_request_uses_loopback_schema_and_no_bearer(self):
        result={'id':'test','choices':[{'finish_reason':'stop','message':{'content':'{"query":"wrench"}'}}]}
        fake=MagicMock();fake.open.return_value.__enter__.return_value.read.return_value=json.dumps(result).encode()
        schema={'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False}
        with patch.object(local,'ensure',return_value=self.config()),patch.object(local,'opener',return_value=fake):
            response=local.responses(Path('.'),{'input':'translate','text':{'format':{'schema':schema}}})
        req=fake.open.call_args.args[0]
        self.assertEqual(req.full_url,'http://127.0.0.1:18080/v1/chat/completions');self.assertIsNone(req.get_header('Authorization'))
        self.assertEqual(json.loads(req.data)['response_format']['type'],'json_schema');self.assertFalse(response['paid_api_used'])
    def test_malformed_output_rejected_without_write(self):
        fake=MagicMock();fake.open.return_value.__enter__.return_value.read.return_value=json.dumps({'choices':[{'message':{'content':'{"query":123}'}}]}).encode()
        with patch.object(local,'ensure',return_value=self.config()),patch.object(local,'opener',return_value=fake):
            with self.assertRaises(local.LocalModelError):local.responses(Path('.'),{'input':'x','text':{'format':{'schema':{'type':'object','properties':{'query':{'type':'string'}}}}}})
    def test_nonimage_role_never_returns_api_key(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(settings,'get_public_settings',return_value={'roles':{'vision':{'model':'local'}}}),patch.object(settings,'get_api_key') as secret:
            self.assertEqual(settings.get_runtime_role(Path(tmp),'vision')['api_key'],'')
        secret.assert_not_called()
    def test_settings_only_images_require_key_and_keep_image_provider(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(settings,'_load_keys',return_value={}):
            public=settings.get_public_settings(Path(tmp))
        self.assertEqual(public['roles']['images']['base_url'],settings.ROLE_DEFAULTS['images']['base_url'])
        self.assertTrue(public['roles']['images']['requires_key'])
        for name in ('vision','attributes'):
            self.assertFalse(public['roles'][name]['requires_key']);self.assertEqual(public['roles'][name]['provider'],'local')
    def test_redirect_is_blocked(self):
        with self.assertRaises(local.LocalModelError):local.NoRedirect().redirect_request(None,None,302,'',{},'https://paid.example')
