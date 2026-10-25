import copy,unittest
from src.ai_listing_pipeline import normalize_copy_sections,proposal_quality_issues
class CopySectionTests(unittest.TestCase):
 def proposal(self,description):return {'title':'Pull Starter','description':description,'methodology':{}}
 def test_known_heading_normalized_without_changing_facts(self):
  prefix='A replacement starter for the specified assembly. '*6
  p=self.proposal(prefix+'\n**Product details:**\n- A housing\n- A handle');before=copy.deepcopy(p);r=normalize_copy_sections(p)
  self.assertEqual(p,before);self.assertIn('Confirmed product details:',r['description']);self.assertIn('- A housing\n- A handle',r['description']);self.assertEqual(proposal_quality_issues(r,{'payload':{}}),[])
 def test_long_bullet_copy_gets_section_heading_only(self):
  desc='A starter for maintenance. '*12+'\n- A housing\n- A handle';r=normalize_copy_sections(self.proposal(desc))
  self.assertEqual(r['description'].replace('Confirmed product details:\n','').split(),desc.split())
 def test_short_copy_not_padded(self):
  p=self.proposal('- housing\n- handle');self.assertEqual(normalize_copy_sections(p),p);self.assertTrue(proposal_quality_issues(p,{'payload':{}}))
 def test_unstructured_copy_still_rejected(self):
  p=self.proposal('A starter. '*40);r=normalize_copy_sections(p);self.assertEqual(r,p);self.assertTrue(any('明细' in x for x in proposal_quality_issues(r,{'payload':{}})))
 def test_normalization_idempotent(self):
  p=self.proposal('A starter. '*40+'\nSpecifications\n- housing\n- handle');r=normalize_copy_sections(p);self.assertEqual(normalize_copy_sections(r),r)
if __name__=='__main__':unittest.main()
