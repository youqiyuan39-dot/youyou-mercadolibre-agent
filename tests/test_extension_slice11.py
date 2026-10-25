import json,re,unittest
from pathlib import Path
E=Path(__file__).resolve().parents[1]/'collector_extension'
class ExtensionSlice11Tests(unittest.TestCase):
 def setUp(self):self.m=json.loads((E/'manifest.json').read_text(encoding='utf-8'))
 def test_json_no_bom(self):self.assertFalse((E/'manifest.json').read_bytes().startswith(b'\xef\xbb\xbf'));self.assertEqual(self.m['manifest_version'],3)
 def test_chrome_version(self):
  v=self.m['version'];self.assertRegex(v,r'^(0|[1-9]\d*)(\.(0|[1-9]\d*)){0,3}$');self.assertTrue(all(int(x)<=65535 for x in v.split('.')));self.assertEqual(v,'0.8.9')
 def test_all_referenced_files(self):
  refs=[]
  for g in self.m['content_scripts']:refs+=g.get('js',[])+g.get('css',[])
  bg=self.m.get('background',{});refs+=bg.get('scripts',[])+([bg['service_worker']] if 'service_worker' in bg else [])
  for g in self.m.get('web_accessible_resources',[]):refs+=g['resources']
  def icons(value):return list(value.values()) if isinstance(value,dict) else [value] if value else []
  refs+=icons(self.m.get('icons'))+icons(self.m.get('action',{}).get('default_icon'))
  for ref in refs:self.assertTrue(list(E.glob(ref)),ref)
 def test_project_globals_defined_before_use(self):
  for g in self.m['content_scripts']:
   known=set()
   for file in g['js']:
    t=(E/file).read_text(encoding='utf-8')
    # Assignment providers include direct globalThis and parser factory root exports.
    definitions=set(re.findall(r'(?:globalThis|root|g)\.(Youyou\w+|collectVisible1688Product)\s*=',t))
    references=set(re.findall(r'\b(Youyou[A-Za-z0-9_]+|collectVisible1688Product)\b',t))
    self.assertFalse(references-known-definitions,(file,sorted(references-known-definitions)))
    known|=definitions
 def test_spa_coverage(self):
  for g in self.m['content_scripts']:
   if 'zying_detail.js' in g['js']:self.assertIn('https://meli.zying.net/*',g['matches'])
 def test_panel_consumers_guard_before_initialization(self):
  for file in ['zying_detail.js','offer_candidates.js']:
   t=(E/file).read_text(encoding='utf-8');self.assertLess(t.index("typeof YouyouCapturePanel"),t.index('YouyouCapturePanel.section'));self.assertLess(t.index("typeof YouyouCapturePanel"),t.index('=true') if '=true' in t else t.index('= true'));self.assertIn('扩展组件未加载，请重载扩展并刷新页面',t)
 def test_shared_globals_idempotent(self):
  for file,glob in [('packaging.js','YouyouPackaging'),('local_transport.js','YouyouLocal'),('zying_detail_parser.js','YouyouZyingDetail'),('zying_list_cache.js','YouyouZyingList'),('offer_candidates_parser.js','YouyouOfferDom')]:
   t=(E/file).read_text(encoding='utf-8');self.assertRegex(t,r'(?:globalThis|root|g)\.'+glob+r'\s*=\s*(?:globalThis|root|g)\.'+glob+r'\s*\|\|')
 def test_no_top_level_lexical_shared_declarations(self):
  for g in self.m['content_scripts']:
   for file in g['js']:self.assertNotRegex((E/file).read_text(encoding='utf-8'),r'(?m)^(?:const|let|class)\s+')
