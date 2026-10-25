"""Small task control layer: scoped traces, bounded feedback, evidence/candidate separation."""
import contextlib,contextvars,copy,hashlib,json,re,time,uuid
from datetime import datetime,timezone
from pathlib import Path
VERSION='TASK_CONTROL_20261016_V1'
ACTIVE=contextvars.ContextVar('yoyo_product_task',default=None)
class BudgetExceeded(RuntimeError):pass
class CandidateInvalid(RuntimeError):
    def __init__(self,message,candidate=None):super().__init__(message);self.candidate=candidate;self.validation_error=True

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def sanitize(value):
    if isinstance(value,dict):
        return {k:sanitize(v) for k,v in value.items() if not re.search(r'cookie|password|api_key|authorization|token$',k,re.I)}
    if isinstance(value,list):return [sanitize(v) for v in value]
    if isinstance(value,str) and value.startswith('data:image/'):
        return {'image_data_sha256':hashlib.sha256(value.encode()).hexdigest(),'encoded_chars':len(value)}
    return value

@contextlib.contextmanager
def task(root,product,sku=None,source_title='',request_limit=16,seconds=240):
    safe=hashlib.sha256(str(product).encode()).hexdigest()[:20]
    context={'root':Path(root),'product':str(product),'sku_id':str(sku) if sku else None,'source_title':source_title,'task_id':uuid.uuid4().hex,
        'requests':0,'tokens':0,'started':time.monotonic(),'request_limit':request_limit,'seconds':seconds,
        'trace_path':Path(root)/'data/v3/task-traces'/(safe+'.jsonl')}
    token=ACTIVE.set(context)
    try:yield context
    finally:ACTIVE.reset(token)

def record(context,event):
    path=context['trace_path'];path.parent.mkdir(parents=True,exist_ok=True)
    row={'version':VERSION,'time':datetime.now(timezone.utc).isoformat(),'task_id':context['task_id'],'product':context['product'],'sku_id':context['sku_id'],**sanitize(event)}
    with path.open('a',encoding='utf-8') as file:file.write(json.dumps(row,ensure_ascii=False)+'\n')

def schema_feedback(body,result):
    name=((body.get('text') or {}).get('format') or {}).get('name')
    if name!='original_quality':return
    text=''.join(p.get('text','') for o in result.get('output') or [] for p in o.get('content') or [])
    value=json.loads(text)
    if value.get('usable_candidate') is False and all(value.get(k) is False for k in ('watermark','promotional_poster','blurred','mixed_variants')):
        raise CandidateInvalid('$.usable_candidate: rejection contradicts all concrete rejection flags being false. A clear secondary scene photo and printed product artwork are allowed; re-evaluate against the named criteria, not a new real-photo-only rule.',text)

def invoke(root,body,timeout,call):
    context=ACTIVE.get()
    if context is None:return call(root,body,timeout)
    current=copy.deepcopy(body);failed=set();usage={'input_tokens':0,'output_tokens':0}
    for attempt in range(2):
        elapsed=time.monotonic()-context['started']
        if context['requests']>=context['request_limit'] or elapsed>=context['seconds'] or context['tokens']>=48000:
            record(context,{'event':'STOP_BUDGET','requests':context['requests'],'seconds':elapsed});raise BudgetExceeded('本SKU已到调用/时间预算；保留已通过步骤，不重复空跑')
        context['requests']+=1;start=time.monotonic();safe=sanitize(current)
        record(context,{'event':'MODEL_REQUEST','attempt':attempt,'request_number':context['requests'],'request_digest':digest(safe),'request':safe,'rag_used':False,'rules_origin':'stage_instructions','facts_origin':'scoped_supplier_evidence'})
        result=None
        try:
            result=call(root,current,min(timeout,max(5,int(context['seconds']-elapsed))))
            for key in usage:usage[key]+=(result.get('usage') or {}).get(key,0)
            context['tokens']+=sum((result.get('usage') or {}).get(k,0) for k in ('input_tokens','output_tokens'))
            schema_feedback(current,result)
            record(context,{'event':'MODEL_RESULT','seconds':time.monotonic()-start,'model':result.get('model'),'provider':result.get('provider'),'paid_api_used':result.get('paid_api_used'),'usage':result.get('usage'),'output':result.get('output')})
            result=copy.deepcopy(result);result['usage'].update(usage);result['task_control_model_calls']=attempt+1
            return result
        except Exception as exc:
            candidate=getattr(exc,'candidate',None);validation=getattr(exc,'validation_error',False)
            fingerprint=digest({'candidate':candidate,'reason':str(exc)})
            record(context,{'event':'MODEL_ERROR','seconds':time.monotonic()-start,'error':str(exc),'validation_error':validation,'candidate':candidate,'repair_allowed':bool(validation and attempt==0),'usage':(result or {}).get('usage'),'provider':(result or {}).get('provider')})
            if not validation or attempt or fingerprint in failed:raise
            failed.add(fingerprint)
            feedback={'sku_id':context['sku_id'],'supplier_title':context['source_title'],'error':str(exc),'field_path':(re.search(r'\$[.\[\]A-Za-z0-9_]*',str(exc)) or ['structured_output'])[0],
                'rejected_candidate':str(candidate or '')[:4000],'remaining_repairs':0,'rule':'Fix the specified output error using the same evidence. No new facts or broader SKU scope.'}
            original=current.get('input')
            if isinstance(original,str):
                current['input']=json.dumps({'original_input':original,'validation_feedback':feedback},ensure_ascii=False)
            else:
                current['input']=copy.deepcopy(original or [])+[{'role':'user','content':[{'type':'input_text','text':json.dumps({'validation_feedback':feedback},ensure_ascii=False)}]}]
    raise RuntimeError('Unreachable')
