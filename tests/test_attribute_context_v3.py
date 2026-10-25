import copy,json,unittest
from src.attribute_context_v3 import batches,merge

def request():
 return {'input':json.dumps({'supplier':{'description':'旧页面'*20000,'skus':[{'variant':'250ml'},{'variant':'500ml'}]},'proposal':{'title':'Tool','description':'AI guess '*2000},'official_attributes':[{'id':'FIELD'+str(i),'name':'Field '+str(i),'tags':{'required':True},'values':[{'id':str(n),'name':'Value'} for n in range(100)]} for i in range(52)],'existing_attributes':[]}), 'text':{'format':{'schema':{'properties':{'attributes':{'items':{'properties':{'id':{'type':'string'}}}},'unresolved':{'items':{'type':'string'}}}}}}}
class AttributeBatchTests(unittest.TestCase):
 def test_unknown_and_fake_value_ids_are_not_verified_attributes(self):
  r=request();c=json.loads(r['input']);c['supplier']={'attributes':{'商品尺寸':'28 mm'}};c['official_attributes']=[{'id':'BRAND'},{'id':'WEIGHT'},{'id':'SIZE'}];r['input']=json.dumps(c)
  chunks=batches(r)
  answer={'output':json.dumps({'attributes':[{'id':'BRAND','value_name':'Unknown'},{'id':'WEIGHT','value_id':'null','value_name':'null'},{'id':'SIZE','value_id':'28','value_name':'28 mm'}],'unresolved':[]})}
  data=json.loads(merge([answer],chunks,lambda r:r['output'])['output'][0]['content'][0]['text'])
  self.assertEqual(data['attributes'],[{'id':'SIZE','value_id':None,'value_name':'28 mm'}]);self.assertEqual(set(data['unresolved']),{'BRAND','WEIGHT'})
 def test_every_field_retained_with_small_requests(self):
  r=request();before=copy.deepcopy(r);chunks=batches(r);self.assertEqual(r,before)
  ids=[a['id'] for q in chunks for a in json.loads(q['input'])['official_attributes']]
  self.assertGreaterEqual(len(ids),52);self.assertEqual(len(set(ids)),52)
  self.assertTrue(all(len(q['input'])<7000 for q in chunks));self.assertTrue(all(len(json.loads(q['input'])['official_attributes'])<=10 for q in chunks))
  self.assertTrue(all(a['tags']['required'] for q in chunks for a in json.loads(q['input'])['official_attributes']))
 def test_no_other_sku_or_ai_description_promoted(self):
  chunks=batches(request());data=json.loads(chunks[0]['input']);self.assertNotIn('proposal',data)
  self.assertNotIn('250ml',json.dumps(chunks));self.assertNotIn('500ml',json.dumps(chunks));self.assertNotIn('AI guess',json.dumps(chunks))
  pieces={}
  for q in chunks:
   for leaf in json.loads(q['input']).get('supplier_segments',[]):
    if leaf['path']=='/description_excerpt':pieces[leaf['offset']]=leaf['text']
  self.assertEqual(''.join(pieces[k] for k in sorted(pieces)),'旧页面'*20000)
 def test_conflicting_segment_values_are_left_unresolved(self):
  r=request();c=json.loads(r['input']);c['official_attributes']=c['official_attributes'][:1];r['input']=json.dumps(c)
  chunks=batches(r)[:2]
  answers=[{'output':json.dumps({'attributes':[{'id':'FIELD0','value_name':v}],'unresolved':[]})} for v in ('steel','plastic')]
  value=merge(answers,chunks,lambda r:r['output']);data=json.loads(value['output'][0]['content'][0]['text'])
  self.assertEqual(data['attributes'],[]);self.assertEqual(data['unresolved'],['FIELD0'])
 def test_merge_filters_invented_field_and_marks_omitted_unresolved(self):
  chunks=batches(request());responses=[{'output':json.dumps({'attributes':[{'id':'OUTSIDE','value_name':'bad'}],'unresolved':[]})} for q in chunks]
  result=merge(responses,chunks,lambda r:r['output']);data=json.loads(result['output'][0]['content'][0]['text'])
  self.assertEqual(data['attributes'],[]);self.assertEqual(len(data['unresolved']),52)
if __name__=='__main__':unittest.main()
