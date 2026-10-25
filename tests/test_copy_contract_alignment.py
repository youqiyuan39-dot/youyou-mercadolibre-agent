import unittest
from src import ai_listing_pipeline as a
from unittest.mock import patch
class CopyContractTests(unittest.TestCase):
 def test_contract_survives_replaced_editorial_prompt(self):
  with patch.object(a,'LISTING_INSTRUCTIONS','Write concise verified copy.'),patch.object(a,'sanitized_product_context',return_value={}),patch.object(a,'_input_images',return_value=[]):
   r=a.build_responses_request({},'local')
  self.assertIn('no minimum character count',r['instructions']);self.assertIn('Confirmed product details:',r['instructions']);self.assertIn('do not invent',r['instructions'])
  self.assertEqual(r['text']['format']['schema']['properties']['description']['minLength'],1)
 def test_short_specific_copy_does_not_trigger_hallucination_prone_padding(self):
  p={'title':'Pull Starter','description':'Confirmed product details:\n- Manual pull starter.','key_facts':['Manual pull starter.'],'methodology':{}}
  self.assertEqual(a.proposal_quality_issues(p,{'payload':{}}),[])
 def test_long_copy_still_needs_details_heading(self):
  p={'title':'Pull Starter','description':'A starter. '*40,'methodology':{}}
  self.assertTrue(any('明细' in x for x in a.proposal_quality_issues(p,{'payload':{}})))
if __name__=='__main__':unittest.main()
