"""Fresh workbench tools and isolated local AgentScope jobs; writes require user confirmation."""
from __future__ import annotations
import copy
import json, re, subprocess, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import build_opener, ProxyHandler

from . import local_model_runtime
VERSION = '2026-10-16-project-rag-multisku'
_PROFILE = local_model_runtime.profile(Path(__file__).resolve().parents[1])
MODEL = _PROFILE['model']
MODEL_URL = _PROFILE['base_url']
RUN_LOCK = threading.Lock()
MODEL_LOCK = threading.Lock()
WORKER_LOCK = threading.Lock()
OWNED_WORKERS = set()
SHUTTING_DOWN = threading.Event()
TERMINAL = {'done', 'failed', 'budget_stop'}
STAGES = {'BLOCKED_EVIDENCE':'缺资料','CAPTURE_READY':'采集资料待确认','READY_FOR_AI':'待处理','AI_PROCESSING':'AI处理中','AI_PAUSED':'已暂停','AI_FAILED':'AI失败','READ_ERROR':'读取失败','READY_FOR_HUMAN_REVIEW':'待发布检查','PUBLISH_BLOCKED':'待发布·需补资料','READY_TO_PUBLISH':'待发布','PUBLISHED':'发布成功','PUBLISH_PARTIAL':'部分成功','PUBLISH_PENDING_CONFIRMATION':'发布待回查'}
ROW_FIELDS = ('name','title','category_id','stage','next_action','errors','warnings','evidence_issues','publish_issues','image_count','generated_image_count','image_job_status','image_job_error','full_ai_status','full_ai_stage','full_ai_progress','full_ai_message','full_ai_error','sku_count','weight_g','dimensions_cm','pricing_status','review_ready','approved','publish_ready','in_publish_center','publish_status','publish_error','publish_checked_at','ai_processing_pricing','sku_processing')
HELP = {'overview':'运营总览显示本机草稿和任务记录；部分成功不能计为全部发布成功。','processing':'商品处理查看采集、AI处理状态与日志；修改事实后需重新核验。','review':'独立商品审核步骤已取消，AI处理完成直接进入发布中心；证据与发布字段问题在发布中心处理。','publish':'AI处理完成自动进入发布中心，无需单独审核通过；在此检查和修改发布资料。进入发布中心不代表发布成功。发布中心已接入选择店铺、发布前核对和结果回查；真实商品发布尚未验收，必须由用户在发布中心确认具体内容，助手不能代发布。','operations':'店铺运营打开时读取缓存。需在相应栏目主动查询后才取得平台数据；助手本身只读缓存状态。','settings':'AI全局设置用于原有生成流程。工作台助手当前独立使用本地Qwen，不会自动使用付费配置。','create':'图片上品创建本机草稿，图片不能证明所有材质、规格、重量等事实。','library':'商品库查看所有本机草稿，打开商品可检查详情、证据和日志。'}

def now(): return datetime.now(timezone.utc).isoformat()

def selection_question(page,question):
    if re.search('哪些功能|功能范围|新增功能|功能清单',str(question)):return False
    return page=='selection' or bool(re.search('选品|线索|货源|1688|去重|探索|月销量|选品批次|研究记录',str(question)))

def selection_initial_tool(question):
    if re.search('探索|去重|重复率|扩展|采集连接|采集状态',question):return 'get_selection_discovery'
    if re.search('货源|1688|研究|同规格|包装|成本',question):return 'get_selection_research'
    return 'get_selection_batch'

def clean(value):
    if isinstance(value, dict):
        return {str(k):clean(v) for k,v in value.items() if str(k) in {'buyer_price_usd','buyer_price_mxn'} or not re.search(r'(?i)token|secret|password|cookie|api.?key|authorization|buyer|email|phone|address',str(k))}
    if isinstance(value, list): return [clean(x) for x in value[:40]]
    if isinstance(value, str):
        value=re.sub(r'(?i)(bearer\s+)[\w.\-]+',r'\1[已隐藏]',value)
        value=re.sub(r'(?i)((?:api[_ -]?key|access[_ -]?token|secret|password|cookie)\s*[=:]\s*)[^\s,;]+',r'\1[已隐藏]',value)
        value=re.sub(r'https?://[^\s<>"\']+', '[链接已省略]', value)
        return value[:900]
    return value

def write(path, record):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)

def read(path): return json.loads(path.read_text(encoding='utf-8'))

def inside(path, parent):
    if not path.resolve().is_relative_to(parent.resolve()): raise ValueError('访问范围不正确')
    return path

class DataSource:
    def __init__(self, root): self.root=Path(root)
    def rows(self):
        from .workbench_v3 import draft_rows
        # Do not follow draft links outside the intended directory.
        for path in (self.root/'drafts').glob('*-draft.json'): inside(path,self.root/'drafts')
        return draft_rows(self.root)
    def wrap(self, label, data, name=None, scope='本机当前记录'):
        return {'source':{'label':label,'read_at':now(),'scope':scope,**({'name':name} if name else {})},'data':clean(data)}
    def row(self, row):
        result={key:row.get(key) for key in ROW_FIELDS}
        result['stage_label']=STAGES.get(row.get('stage'),row.get('stage'))
        return result
    def get_workbench_overview(self):
        rows=self.rows(); counts={}
        for row in rows:
            stage=STAGES.get(row.get('stage'),row.get('stage'));counts[stage]=counts.get(stage,0)+1
        issues=[self.row(r) for r in rows if r.get('errors') or r.get('evidence_issues') or r.get('publish_error')]
        return self.wrap('工作台总览',{'商品总数':len(rows),'首页待处理合计':sum(r.get('stage') in {'BLOCKED_EVIDENCE','CAPTURE_READY','READY_FOR_AI','AI_PROCESSING','AI_PAUSED','AI_FAILED','READ_ERROR'} for r in rows),'首页待处理口径':'包含缺资料、待处理及处理中等状态；下方各状态是细分数量','各状态数量':counts,'需关注商品数量':len(issues),'证据问题汇总':dict(__import__('collections').Counter(issue for row in rows for issue in row.get('evidence_issues',[]))),'详情提示':'如需定位具体商品，请在助手的关联商品列表选择具体商品。总览统计不提供单件发布结论。','助手权限':'可诊断并准备修改预览；用户确认后执行本机修改，付费重试另行确认；不能提交发布','正式发布接口':'已接入店铺绑定、发布前核对和结果回查；真实发布尚未验收，用户在发布中心确认，助手不能提交'})
    def get_business_brief(self, name: str=''):
        if name: self.product_path(name)
        from .business_brief_v3 import build
        return self.wrap('业务处理建议', build(self.rows(), name), name or None)
    def find_products(self, query: str=''):
        query=str(query).strip()[:120].casefold();rows=self.rows()
        matches=[r for r in rows if not query or query in ' '.join(str(r.get(k) or '') for k in ('name','title','stage')).casefold() or query in STAGES.get(r.get('stage'),'')]
        return self.wrap('商品检索',{'matched':len(matches),'items':[self.row(r) for r in matches[:5]],'truncated':len(matches)>5})
    def product_path(self, name):
        if not isinstance(name,str) or not re.fullmatch(r'[\w.\-]{1,180}-draft\.json',name) or '..' in name: raise ValueError('需使用商品检索返回的草稿文件名')
        return inside(self.root/'drafts'/name,self.root/'drafts')
    def inspect_product(self, name: str, detail_level: str='full'):
        path=self.product_path(name)
        if not path.is_file(): return self.wrap('商品详情',{'found':False,'message':'未找到该商品，不能推测它的状态'},name)
        row=next((r for r in self.rows() if r['name']==name),None)
        draft=read(path);evidence=draft.get('evidence') or {};payload=draft.get('payload') or {}
        from .sku_diagnostics_v3 import summarize
        diagnostics=summarize(draft)
        product=self.row(row) if row else None
        if detail_level=='summary' and product:
            groups=[f"{b['label']}：{b['sku_count']}个SKU" for b in diagnostics['blockers']]
            for field in ('publish_issues','evidence_issues'):product[field]=groups
            product['errors']=list(dict.fromkeys(product.get('errors') or []))[:3]
            if product.get('sku_processing'):
                process=product['sku_processing'];product['sku_processing']={k:process.get(k) for k in ('version','paid_image_calls','pricing_error','profit_requires_human_confirmation')}
            if product.get('ai_processing_pricing'):
                pricing=product['ai_processing_pricing'];pricing['error_count']=len(pricing.get('errors') or []);pricing['errors']=(pricing.get('errors') or [])[:2]
        selected_id=evidence.get('current_pricing_sku_id')
        selected_sku=next((s for s in evidence.get('sku_details',[]) if str(s.get('sku_id'))==str(selected_id)),{})
        facts={'selected_sku':{k:selected_sku.get(k) for k in ('sku_id','variant','seller_sku')},'packed_weight':{k:(evidence.get('packed_weight') or {}).get(k) for k in ('value','status','source')},'packed_dimensions':{k:(evidence.get('packed_dimensions') or {}).get(k) for k in ('length','width','height','status','source')},'attributes':[{k:a.get(k) for k in ('id','value_name','status','source')} for a in (payload.get('attributes') or [])[:20]]}
        return self.wrap('商品详情',{'found':row is not None,'product':product,'sku_diagnostics':diagnostics,'model_resolution':selected_sku.get('model_resolution') or {},'detail_level':detail_level,'detail_note':'summary逐SKU统计缺项；完整逐条问题可用full再次读取' if detail_level=='summary' else '完整商品字段','recorded_facts_unverified_unless_status_confirms':facts,'editable_draft_fields':{k:payload.get(k) for k in ('title','description','seller_sku','available_quantity')},'draft_updated_at':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()},name)
    def get_product_logs(self, name: str):
        path=self.product_path(name)
        if not path.is_file(): return self.wrap('处理日志',{'found':False},name)
        from .draft_editor import draft_ai_log
        for sub in ('jobs','ai_outputs'):
            for f in (self.root/sub).glob(path.stem+'*.json'): inside(f,self.root/sub)
        events=draft_ai_log(name,self.root/'drafts',self.root)
        detail_keys=('status','stage','progress','message','error','prompt_count','language','kind','model','draft')
        events=[{'time':e.get('time'),'event':e.get('event'),'detail':{k:v for k,v in (e.get('detail') or {}).items() if k in detail_keys}} for e in events[:12]]
        return self.wrap('处理日志',{'found':True,'events':events},name)
    def get_operations_state(self):
        directory=self.root/'data/v3/operations/mercadolibre'; records=[]
        for path in sorted(directory.glob('*/*.json')):
            if path.stem not in {'listings','metrics','orders','billing','messages','pending','campaigns'}: continue
            inside(path,directory);item=read(path)
            records.append({'store':path.parent.name,'kind':path.stem,**{k:item.get(k) for k in ('state','total')},'platform_queried_at':item.get('time'),'cached_rows_count':len(item.get('rows') or [])})
        times=[r['platform_queried_at'] for r in records if r.get('platform_queried_at')]
        latest=max(times) if times else None
        result=self.wrap('店铺运营缓存状态',{'最新缓存平台查询时间':latest,'records':records,'时间口径':'source.read_at只是本次读取本机缓存的时间，绝不是平台数据更新时间。各条platform_queried_at才是上次平台查询时间。','message':'仅核对缓存状态和数量，不读取买家或订单明细；没有记录表示本次未取得。需要最新平台数据时先去店铺运营相应栏目查询。'},scope='平台查询缓存，非实时平台状态')
        result['source']['platform_queried_at']=latest
        return result
    def get_pricing_inputs(self):
        from .pricing_tools_v3 import inputs
        return self.wrap('利润与比价输入资料',inputs(self.root))
    def calculate_profit(self, inputs_json: str):
        from .pricing_tools_v3 import calculate
        return self.wrap('贡献利润情景计算',calculate(json.loads(inputs_json)),scope='输入情景计算，非实际结算')
    def compare_market_prices(self, store_id: str, site_id: str='MLM', query: str='', item_ids: str=''):
        from .pricing_tools_v3 import compare
        result=compare(self.root,dict(store_id=store_id,site_id=site_id,query=query,item_ids=item_ids))
        wrapped=self.wrap('美客多只读价格查询',result,scope='本次API或公开页面查询，实际来源见data.source；同款规格未核验')
        wrapped['source']['platform_queried_at']=result['platform_queried_at']
        return wrapped
    def prepare_autonomous_price(self, name: str, store_id: str, query: str, site_id: str='MLM', sku_id: str=''):
        from .autopricing_v3 import prepare
        result=prepare(self.root,dict(name=name,store_id=store_id,query=query,site_id=site_id,sku_id=sku_id))
        result=copy.deepcopy(result)
        result['comparison']['items']=result['comparison']['items'][:4]
        return self.wrap('本机预算定价方案',result,name,scope='预算定价预览，未写入；在利润与比价页面可计算并写入，实际发布另行确认')
    def get_workbench_capabilities(self):
        return self.wrap('助手功能范围',{'read_tools':list(TOOLS),'areas':{
            '主线':'商品采集后自动本地AI处理：筛选并采用可用原图→逐SKU文案与属性→利润预算→发布中心核对。付费生图及利润需人工确认。AI选品模块已撤下。',
            '知识与教程':'解释选品原则、利润口径、定价和采集教程；通用知识与当前记录分开。',
            '商品与流程':'读取详情日志和集成定价摘要，诊断阻塞；准备资料、提示词、参数、失败任务修改预览。',
            '利润与比价':'AI处理集成墨西哥Remote全SKU预算定价；利润与比价页保留高级核对。未知成本不填0。'},
            'limits':'助手不能自行启动付费重试或确认发布；不能承诺自动解决缺失事实或登录验证。真实采集和发布须取得回执才算完成。'},scope='当前源码功能清单；实际状态另查商品和日志')
    def get_business_knowledge(self, topic: str='使用教程'):
        from .assistant_business_knowledge import lookup
        data=lookup(topic,self.root)
        result=self.wrap('业务知识与使用教程',data,scope='当前项目规则与带来源参考；非实时平台规则或商品事实')
        result['source'].update(knowledge_version=data.get('version'),retrieval_method=data.get('retrieval_method'),document_ids=list(data.get('guides') or {}),index_digest=data.get('index_digest'))
        return result
    def get_selection_overview(self):
        from .assistant_selection_tools import overview
        return self.wrap('选品批次与研究目录',overview(self.root),scope='本机历史记录，包含采集时间；非实时市场数据')
    def get_selection_batch(self, batch_id: str='', item_id: str=''):
        from .assistant_selection_tools import batch
        return self.wrap('选品规则与批次复核',batch(self.root,batch_id,item_id),scope='按今天日期复核已保存快照，不重新查询平台')
    def get_selection_discovery(self):
        from .assistant_selection_tools import exploration
        return self.wrap('选品探索与采集反馈',exploration(self.root),scope='本机探索回执与扩展最近心跳；无新采集操作')
    def get_selection_research(self, revision_id: str=''):
        from .assistant_selection_tools import case
        return self.wrap('货源与利润研究依据',case(self.root,revision_id),scope='本机研究证据及按当前日期复核；未经平台或供应商实时核验')
    def get_page_help(self, page: str='overview'):
        return self.wrap('工作台使用说明',{'page':page,'help':HELP.get(page,'可在商品库找到商品，打开商品详情查看证据和日志；提问时请说明商品和遇到的问题。')},scope='当前版本功能说明')

HELP['pricing']='利润与比价可带入商品SKU的历史采购成本和暂定净回款，未知成本留空，不适用填0；参考汇率可查询或手填。关键词接口403时自动用公开页面查询；两种来源均不可用才是本次未取得。自主定价支持Remote预算净回款，成本和预留项列明；可备份、写入本机并撤销，不自动发布。不能把净回款拿去和竞品买家售价比较。'

HELP['selection']='AI选品功能已撤下；选品知识仍可询问助手，不会启动选品任务。'
HELP['processing']='采集完成自动加入本地处理队列；先筛原图，逐SKU生成文案与属性，长资料分段提取，计算利润预算。一个SKU失败不阻止其他SKU。可用原图优先采用，付费生图须另行确认；利润在发布前人工核对。缺资料保持待补，不自动发布。'
HELP['collection']='在1688详情采集，工作台收到后自动进入本地AI处理队列。重复采集相同资料不重复启动，旧商品不会批量补跑；工作台需保持打开。接收端在线不等于真实回传。'
TOOLS=('get_workbench_capabilities','get_business_knowledge','prepare_autonomous_price','get_pricing_inputs','calculate_profit','compare_market_prices','get_business_brief','get_workbench_overview','find_products','inspect_product','get_product_logs','get_operations_state','get_page_help')

def knowledge_question(question,context=None):
    question=str(question)
    if (context or {}).get('product') and re.search(r'这件|这个商品|当前商品|该SKU|该商品|\d+\s*(?:元|美元|MXN|USD)',question,re.I):return False
    return bool(re.search('知识|教程|原则|怎么用|如何使用|怎么选品|如何选品|选品建议|什么是|是什么|是什么意思|怎么算|如何计算|区别|知识库|RAG|MCP|Token|Skill',question,re.I))

def model_status():
    try:
        with build_opener(ProxyHandler({})).open(MODEL_URL+'/models',timeout=1) as r: data=json.load(r)
        return 'ready' if any(x.get('id')==MODEL for x in data.get('data',[])) else 'other_service'
    except Exception: return 'offline'

def runtime_paths(root):
    model_root=Path(root)
    return model_root,Path(root)/'tools/assistant_env/Scripts/python.exe'

def ensure_model(root):
    return local_model_runtime.ensure(root)


def status(root):
    model_root,python=runtime_paths(root)
    return {'ok':True,'version':VERSION,'model':MODEL,'model_state':model_status(),'runtime_installed':python.is_file(),'model_installed':Path(local_model_runtime.profile(root)['model_file']).is_file(),'busy':RUN_LOCK.locked(),'mode':'PREVIEW_THEN_CONFIRM_LOCAL','paid_fallback':False}

def job_path(root, job_id):
    if not re.fullmatch('[a-f0-9]{32}',str(job_id)): raise ValueError('任务编号无效')
    return Path(root)/'data/v3/workbench-assistant/jobs'/(job_id+'.json')

def job_status(root, job_id):
    path=job_path(root,job_id)
    if not path.is_file(): raise ValueError('未找到该次提问')
    data=read(path)
    return {'ok':True,'job':{k:data.get(k) for k in ('id','state','phase','created_at','updated_at','answer','error','sources','trace','checks','elapsed_seconds','model','finished_reason','actions')}}

def ask(root, value):
    if SHUTTING_DOWN.is_set():raise ValueError("工作台正在关闭，不接受新任务")
    question=str(value.get('question') or '').strip()
    if not 1<=len(question)<=1500: raise ValueError('请输入1–1500字的问题')
    context=value.get('context') or {}
    if not isinstance(context,dict): raise ValueError('页面上下文格式无效')
    context={k:str(context.get(k) or '')[:200] for k in ('page','product')}
    if context['product']: DataSource(root).product_path(context['product'])
    history=value.get('history') or []
    if not isinstance(history,list): raise ValueError('历史消息格式无效')
    history=[{'role':x.get('role'),'text':str(x.get('text') or '')[:700]} for x in history[-4:] if isinstance(x,dict) and x.get('role') in ('user','assistant')]
    _,python=runtime_paths(root)
    if not python.is_file(): raise ValueError('助手运行环境未安装，本次未取得回答')
    if not RUN_LOCK.acquire(blocking=False): raise ValueError('助手正在处理上一条问题，请稍后再试')
    job_id=uuid.uuid4().hex;path=job_path(root,job_id)
    record={'id':job_id,'state':'queued','phase':'准备本地模型','question':question,'context':context,'history':history,'created_at':now(),'updated_at':now(),'model':MODEL,'sources':[],'trace':[]}
    try:
        write(path,record)
        threading.Thread(target=_run,args=(Path(root),path,python),daemon=True).start()
    except Exception:
        RUN_LOCK.release();raise
    return {'ok':True,'job_id':job_id}

def _run(root,path,python):
    started=time.monotonic();proc=None
    try:
        if SHUTTING_DOWN.is_set():raise ValueError("工作台正在关闭")
        ensure_model(root)
        record=read(path);record.update(state='running',phase='查询工作台并分析',updated_at=now());write(path,record)
        # The child has no credential environment, network shell tool, or paid fallback.
        import os
        env={k:v for k,v in os.environ.items() if not re.search(r'(?i)key|token|secret|password|cookie|authorization',k)}
        env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',NO_PROXY='127.0.0.1,localhost')
        log=root/'logs'/('assistant-'+path.stem+'.log')
        with log.open('wb') as output:
            proc=subprocess.Popen([str(python),'-m','src.workbench_assistant_worker',str(path)],cwd=root,env=env,stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            with WORKER_LOCK:
                OWNED_WORKERS.add(proc)
                if SHUTTING_DOWN.is_set():proc.terminate()
            # A verified 32K local request can exceed the old 120s worker deadline.
            # This remains a finite end-to-end budget, never an unbounded retry loop.
            proc.wait(timeout=300 if local_model_runtime.profile(root)['context_size']>8192 else 120)
        record=read(path)
        if record['state'] not in TERMINAL: raise ValueError('助手进程未完成回答，本次未取得有效结果')
    except Exception as exc:
        if proc is not None and proc.poll() is None: proc.kill();proc.wait()
        record=read(path)
        error='分析超过时限，已停止；可缩小到一件商品后重试' if isinstance(exc,subprocess.TimeoutExpired) else str(exc)
        record.update(state='failed',phase='本次未完成',answer='',error=clean(error),updated_at=now());write(path,record)
    finally:
        with WORKER_LOCK:OWNED_WORKERS.discard(proc)
        record=read(path);record['elapsed_seconds']=round(time.monotonic()-started,2);write(path,record);RUN_LOCK.release()


def stop_owned_workers(root):
    SHUTTING_DOWN.set()
    with WORKER_LOCK:processes=list(OWNED_WORKERS)
    for proc in processes:
        if proc.poll() is None:
            try:proc.terminate()
            except OSError:pass
    directory=Path(root)/'data/v3/workbench-assistant/jobs'
    for path in directory.glob('*.json'):
        if not path.resolve().is_relative_to(directory.resolve()):continue
        try:
            job=read(path)
            if job.get('state') in ('queued','running'):
                job.update(state='failed',phase='工作台已关闭',error='窗口关闭，本次助手回答未完成；重新打开后可再次提问',answer='',updated_at=now())
                write(path,job)
        except (OSError,ValueError):continue
