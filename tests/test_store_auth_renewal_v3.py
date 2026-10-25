import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from src import store_operations_v3 as ops


class RenewalTests(unittest.TestCase):
    def exercise(self, responses):
        row={'id':'bound','merchant_id':123}
        client=Mock(); client.me.side_effect=responses
        with patch.object(ops,'MercadoLibreClient',return_value=client), \
             patch.object(ops.oauth,'load_private_json',return_value={'access_token':'synthetic'}), \
             patch.object(ops.stores,'get_runtime_oauth_config',return_value={'client_id':'test','client_secret':'synthetic'}), \
             patch.object(ops.oauth,'refresh_tokens') as refresh:
            try:
                result=ops.client_for(Path('sandbox'),row)
            except Exception as exc:
                result=exc
            return result, refresh.call_args_list

    def test_401_renews_once_for_bound_store(self):
        expired=ops.MercadoLibreAPIError('localized error',http_status=401)
        result,calls=self.exercise([expired,expired,{'id':123}])
        self.assertEqual(result[1]['id'],123)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].kwargs['token_file'].name,'mercadolibre_tokens_bound.dat')

    def test_already_refreshed_by_other_request_is_reused(self):
        result,calls=self.exercise([ops.MercadoLibreAPIError('expired',http_status=401),{'id':123}])
        self.assertEqual(calls,[])
        self.assertEqual(result[1]['id'],123)

    def test_network_or_forbidden_does_not_rotate_credentials(self):
        for error in (TimeoutError(),ops.MercadoLibreAPIError('失效',http_status=403)):
            result,calls=self.exercise([error])
            self.assertIs(result,error);self.assertEqual(calls,[])

    def test_still_unauthorized_or_wrong_identity_stops(self):
        bad=ops.MercadoLibreAPIError('expired',http_status=401)
        for last in (bad,{'id':456}):
            result,calls=self.exercise([bad,bad,last])
            self.assertIsInstance(result,Exception);self.assertEqual(len(calls),1)


if __name__=='__main__':unittest.main()
