import io, json, unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from src import assistant_inference_v3 as inference

class InferencePolicyTests(unittest.TestCase):
    def policy(self, slots=None, error=None, knowledge=False):
        opener = MagicMock()
        if error: opener.open.side_effect = error
        else: opener.open.return_value = io.BytesIO(json.dumps(slots).encode())
        with patch.object(inference.runtime, 'profile', return_value={'context_size':32768}), patch.object(inference.runtime,'opener',return_value=opener):
            return inference.policy('.',knowledge=knowledge)
    def test_running_slot_limits_context_not_config_alone(self):
        self.assertEqual(self.policy([{'n_ctx':8192}])['context_size'],8192)
        self.assertEqual(self.policy([{'n_ctx':32768}])['context_size'],32768)
        self.assertEqual(self.policy([{'n_ctx':32768},{'n_ctx':16384}])['context_size'],16384)
    def test_unreadable_slots_fall_back_conservatively(self):
        for data in ([],[{'n_ctx':'32768'}],[{'n_ctx':-1}]):
            result=self.policy(data)
            self.assertEqual(result['context_size'],8192)
            self.assertFalse(result['context_verified'])
        self.assertEqual(self.policy(error=OSError('offline'))['context_size'],8192)
    def test_only_knowledge_skips_thinking(self):
        self.assertFalse(self.policy([{'n_ctx':32768}],knowledge=True)['enable_thinking'])
        self.assertTrue(self.policy([{'n_ctx':32768}])['enable_thinking'])
        self.assertTrue(self.policy([])['extra_body']['cache_prompt'])
    def test_compaction_preserves_unknowns_errors_sources_and_quoted_spaces(self):
        value={'sku':'A B','weight':None,'errors':['403 未取得','保留  两空格'], 'source':{'read_at':'2026-10-15','verified':False}, 'nested':[0,False,{'a':'b'}]}
        self.assertEqual(json.loads(inference.compact_json(value)),value)
        self.assertLess(len(inference.compact_json(value)),len(json.dumps(value,ensure_ascii=False)))
    def test_metrics_capture_counts_not_content(self):
        row=inference.usage_row(SimpleNamespace(usage=SimpleNamespace(input_tokens=110,output_tokens=12,cache_input_tokens=90)),1.1234)
        self.assertEqual(row,{'seconds':1.123,'input_tokens':110,'output_tokens':12,'cache_input_tokens':90})

if __name__=='__main__':unittest.main()
