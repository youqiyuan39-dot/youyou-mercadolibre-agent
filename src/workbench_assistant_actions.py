"""Proposal-only model tools; business writes require a separate user confirmation."""
from __future__ import annotations
import copy, hashlib, json, math, re, threading, time, uuid
from pathlib import Path
from . import workbench_assistant as bridge
from .generation_strategy_v3 import load as load_generation, validate as validate_generation
LOCK=threading.RLock()
LIVE_LOCK=threading.Lock()
LIVE={}
ACTIVE={'queued','running','generating'}
KINDS={'product','product_prompt','template','generation','task'}
LABELS={'product':'商品资料','product_prompt':'当前商品提示词','template':'公共提示词模板','generation':'全局生图参数','task':'任务操作'}
SUFFIXES=('full-ai-job','image-job','sku-image-job')

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

def change_path(root, ident):
    if not re.fullmatch('[a-f0-9]{32}',str(ident)): raise ValueError('变更编号无效')
    return bridge.inside(Path(root)/'data/v3/workbench-assistant/changes'/ (ident+'.json'),Path(root)/'data/v3/workbench-assistant/changes')

def relative(root, rel):
    return bridge.inside(Path(root)/rel,Path(root))

def jobs(root,name=''):
    base=Path(root)/'jobs'
    paths=[base/(Path(name).stem+'-'+s+'.json') for s in SUFFIXES] if name else sorted(base.glob('*-job.json'))
    return [(bridge.inside(p,base),bridge.read(p)) for p in paths if p.is_file()]

def idle(root,name=''):
    with LIVE_LOCK:
        if any(count and key[0]==str(Path(root).resolve()) and (not name or key[1]==name) for key,count in LIVE.items()):raise ValueError('当前任务线程尚未结束，暂停请求后请等待在途请求完成')
    if any(j.get('status') in ACTIVE for _,j in jobs(root,name)): raise ValueError('相关生成任务仍在运行，请先暂停并等待当前请求结束，再修改或重试')

def text(value,label,limit=6000):
    if not isinstance(value,str) or not value.strip() or len(value)>limit: raise ValueError(label+'需为非空文本，长度不超过'+str(limit))
    # Do not allow credential assignments to enter assistant proposals or prompts.
    if re.search(r'(?i)(api.?key|access.?token|password|cookie|authorization)\s*[:=]',value): raise ValueError('不能通过助手保存凭据')
    return value.strip()

def fields(value,allowed):
    if not isinstance(value,dict) or not value or set(value)-set(allowed): raise ValueError('修改字段不在允许范围：'+', '.join(allowed))

def number(value,label):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<value<=1000000: raise ValueError(label+'需为大于0的有限数值')
    return value

def build(root,kind,name,changes):
    root=Path(root); writes={};diffs=[];effect=[]
    def put(path,value): writes[str(path.relative_to(root))]=value
    def diff(field,before,after):
        if before!=after: diffs.append({'field':field,'before':before,'after':after})
    if kind in {'product','product_prompt'}:
        path=bridge.DataSource(root).product_path(name)
        if not path.is_file(): raise ValueError('未找到目标商品')
        idle(root,name);old=bridge.read(path);d=copy.deepcopy(old);payload=d.setdefault('payload',{});evidence=d.setdefault('evidence',{})
        if kind=='product':
            fields(changes,('title','description','seller_sku','available_quantity','packed_weight_g','packed_dimensions_cm'))
            for key,value in changes.items():
                if key in {'title','description','seller_sku'}:
                    value=text(value,key,{'title':200,'description':6000,'seller_sku':80}[key]);diff(key,payload.get(key),value);payload[key]=value
                elif key=='available_quantity':
                    if type(value) is not int or not 0<=value<=1000000: raise ValueError('库存需为0–1000000的整数')
                    diff(key,payload.get(key),value);payload[key]=value
                elif key=='packed_weight_g':
                    value=number(value,'包装重量');oldvalue=(evidence.get('packed_weight') or {}).get('value')
                    diff(key,oldvalue,value)
                    evidence['packed_weight']={**(evidence.get('packed_weight') or {}),'value':value,'status':'ASSISTANT_EDITED_UNVERIFIED','confidence':'UNVERIFIED','source':'用户确认的助手修改，待实测核验'}
                else:
                    if not isinstance(value,dict) or set(value)!={'length','width','height'}: raise ValueError('尺寸需包含length、width、height，单位cm')
                    value={k:number(v,'包装尺寸') for k,v in value.items()};before={k:(evidence.get('packed_dimensions') or {}).get(k) for k in value};diff(key,before,value)
                    evidence['packed_dimensions']={**(evidence.get('packed_dimensions') or {}),**value,'status':'ASSISTANT_EDITED_UNVERIFIED','confidence':'UNVERIFIED','source':'用户确认的助手修改，待实测核验'}
        else:
            fields(changes,('content',));content=text(changes.get('content'),'提示词')
            oldprompt=evidence.get('prompt_template') or {};diff('prompt_template.content',oldprompt.get('content'),content)
            evidence['prompt_template']={'id':'assistant-local','version':int(oldprompt.get('version') or 0)+1,'content':content,'status':'BOUND_FOR_NEXT_AI_REQUEST'}
            effect.append('仅绑定当前商品的下一次文案/属性请求；本次不调用模型，也不会修改公共模板或已生成图片。')
        d['human_approved']=False
        d.setdefault('edit_metadata',{}).update(assistant_modified_at=bridge.now(),assistant_review_required=True)
        put(path,d)
        approval=root/'reviews'/(path.stem+'-approval.json')
        review=bridge.read(approval) if approval.is_file() else {}
        review.update(approved=False,invalidated_at=bridge.now(),reason='助手修改后必须重新人工审核',external_publish_performed=False)
        put(bridge.inside(approval,root/'reviews'),review)
        effect.append('只修改本机草稿，清除该商品的旧审核通过状态；本次填入的重量和尺寸会标为未核验，未修改字段保留原值。不会发布、改价或修改平台库存。')
    elif kind=='generation':
        fields(changes,('image_count','image_text','main_background'));idle(root)
        before=load_generation(root);after=validate_generation({**before,**changes})
        for key in changes: diff(key,before.get(key),after[key])
        put(root/'data/v3/generation.json',after)
        effect.append('影响之后启动的公共生图流程；不是单件商品参数，本次不生成图片。')
    elif kind=='template':
        fields(changes,('id','name','content'));ident=changes.get('id') or uuid.uuid4().hex
        if not isinstance(ident,str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}',ident): raise ValueError('模板编号无效')
        path=bridge.inside(root/'data/v3/templates'/(ident+'.json'),root/'data/v3/templates');old=bridge.read(path) if path.is_file() else {}
        label=text(changes.get('name') or old.get('name'),'模板名称',80);content=text(changes.get('content'),'模板内容')
        for key,value in {'name':label,'content':content}.items(): diff(key,old.get(key),value)
        put(path,{'id':ident,'name':label,'content':content,'version':int(old.get('version') or 0)+1,'updated_at':bridge.now(),'deleted':False,'previous':([{k:old.get(k) for k in ('version','name','content','updated_at')}]+(old.get('previous') or [])) if old else []})
        effect.append('保存公共模板的新版本；已绑定到商品的旧版本保持原样，需要另外绑定才生效。本次不调用模型。')
    elif kind=='task':
        fields(changes,('action','language'));action=changes.get('action');language=changes.get('language','es')
        if action not in {'pause','retry_images','retry_full'}: raise ValueError('任务操作只支持pause、retry_images、retry_full')
        if language not in {'es','pt','en'}: raise ValueError('语言需为es、pt或en')
        path=bridge.DataSource(root).product_path(name)
        if not path.is_file(): raise ValueError('未找到目标商品')
        existing=jobs(root,name)
        if action=='pause':
            if not any(j.get('status') in ACTIVE for _,j in existing): raise ValueError('当前没有可暂停的任务')
            effect.append('协作式暂停：已发出的API请求仍可能完成并计费，不等于撤销请求。')
        else:
            idle(root,name);suffix='image-job' if action=='retry_images' else 'full-ai-job'
            job=next((j for p,j in existing if p.name==path.stem+'-'+suffix+'.json'),{})
            if job.get('status') not in {'failed','paused','partial','partial_failed'} and not any(i.get('status')=='failed' for i in job.get('items') or []): raise ValueError('未找到该类失败/暂停任务，不能当作失败任务重试')
            draft=bridge.read(path);draft['human_approved']=False;draft.setdefault('edit_metadata',{}).update(assistant_review_required=True,assistant_retry_at=bridge.now());put(path,draft)
            approval=bridge.inside(root/'reviews'/(path.stem+'-approval.json'),root/'reviews');review=bridge.read(approval) if approval.is_file() else {};review.update(approved=False,invalidated_at=bridge.now(),reason='重试生成后必须重新审核',external_publish_performed=False);put(approval,review)
            diff('人工审核状态',{'human_approved':bridge.read(path).get('human_approved'),'approved':bridge.read(approval).get('approved') if approval.is_file() else False}, {'human_approved':False,'approved':False})
            effect.append('重试前清除旧审核通过状态；失败时也保持待重新审核，不会将旧审核用于新产物。')
            effect.append('重试图片：仅在输入签名一致、成功图片文件仍在时复用已有图片；否则可能重新生成。' if action=='retry_images' else '重试完整AI流程：文案和公共图片仅在检查点匹配时复用，SKU图片等步骤可能再次生成。')
        diff('任务操作', [{'kind':p.stem.removeprefix(path.stem+'-'),'status':j.get('status')} for p,j in existing],{'action':action,'language':language})
        effect.append('仅提交任务，不代表生成成功或故障已经修复；执行后需查看任务日志。')
    else: raise ValueError('不支持的变更类型')
    if not diffs: raise ValueError('修改前后相同，无需保存')
    return writes,diffs,effect

def get_flow_configuration(root,name=''):
    result={'generation':load_generation(root),'allowed_product_fields':['title','description','seller_sku','available_quantity','packed_weight_g','packed_dimensions_cm'],'task_actions':['pause','retry_images','retry_full'],'template_scope':'公共模板保存版本，不会自动覆盖已绑定商品；product_prompt只绑定当前商品','execution':'模型仅创建预览；用户点击确认后执行，付费重试需要额外勾选'}
    if name:
        path=bridge.DataSource(root).product_path(name)
        if not path.is_file(): raise ValueError('未找到目标商品')
        draft=bridge.read(path);result['product_prompt']=(draft.get('evidence') or {}).get('prompt_template')
        result['tasks']=[{'kind':p.stem.removeprefix(path.stem+'-'),**{k:j.get(k) for k in ('status','stage','error','created_at','updated_at')}} for p,j in jobs(root,name)]
    result['templates']=[{k:bridge.read(p).get(k) for k in ('id','name','version','content')} for p in sorted((Path(root)/'data/v3/templates').glob('*.json')) if not bridge.read(bridge.inside(p,Path(root)/'data/v3/templates')).get('deleted')][:10]
    return bridge.DataSource(root).wrap('流程配置与任务状态',result,name or None)

def prepare(root,kind,changes_json,reason,name=''):
    if kind not in KINDS: raise ValueError('变更类型无效')
    if not isinstance(changes_json,str) or len(changes_json)>16000: raise ValueError('修改内容需为JSON对象文本，最大16000字符')
    changes=json.loads(changes_json);reason=text(reason,'修改原因',500)
    with LOCK:
        writes,diffs,effects=build(root,kind,name,changes)
        # Freeze complete affected records and draft/config dependencies for stale detection.
        deps=set(writes)
        if kind=='task':
            deps.add('drafts/'+name);deps.add('data/v3/generation.json')
            for p,j in jobs(root,name): deps.add(str(p.relative_to(root)))
            if changes.get('action')=='pause':
                # Progress changes are expected; a new job identity must still invalidate the preview.
                base_jobs={str(p.relative_to(root)):{k:j.get(k) for k in ('created_at','input_signature')} for p,j in jobs(root,name)}
            else: base_jobs={}
        else:base_jobs={}
        ident=uuid.uuid4().hex;paid=kind=='task' and changes.get('action')!='pause'
        record={'id':ident,'kind':kind,'label':LABELS[kind],'name':name,'reason':reason,'changes':changes,'diffs':diffs,'effects':effects,'state':'pending','created_at':bridge.now(),'expires_epoch':time.time()+1800,'base':{rel:digest(relative(root,rel)) for rel in deps},'base_jobs':base_jobs,'writes':writes,'requires_paid_confirmation':paid,'cost_notice':'将使用现有AI生成配置，可能消耗付费API额度；本地聊天免费不代表生成流程免费。当前未取得准确金额，不承诺只重跑失败步骤。' if paid else '本次不调用付费生成接口。','publishing_performed':False}
        bridge.write(change_path(root,ident),record)
        return bridge.DataSource(root).wrap('变更预览（尚未执行）',public(record),name or None)

def public(record):
    return {k:record.get(k) for k in ('id','kind','label','name','reason','state','created_at','diffs','effects','requires_paid_confirmation','cost_notice','result','error','can_undo','applied_at','publishing_performed')}

def get(root,ident):
    path=change_path(root,ident)
    if not path.is_file():raise ValueError('未找到修改预览')
    return {'ok':True,'change':public(bridge.read(path))}

def check_base(root,record):
    for rel,expected in record['base'].items():
        path=relative(root,rel)
        if rel in record.get('base_jobs',{}):
            current=bridge.read(path) if path.is_file() else {};identity={k:current.get(k) for k in ('created_at','input_signature')}
            if identity!=record['base_jobs'][rel]:raise ValueError('目标任务已被替换，请重新生成预览')
        elif digest(path)!=expected:raise ValueError('预览后资料或配置已有变动，请重新生成预览')

def save_audit(root,record,event):
    from .workbench_v3 import audit
    audit(Path(root),event,{'change_id':record['id'],'kind':record['kind'],'name':record['name'],'state':record['state'],'paid_confirmed':record.get('paid_confirmed',False)})

def act(root,ident,operation,value):
    root=Path(root)
    with LOCK:
        path=change_path(root,ident)
        if not path.is_file():raise ValueError('未找到修改预览')
        record=bridge.read(path)
        if operation=='cancel':
            if record['state']!='pending':raise ValueError('该预览已处理')
            record.update(state='cancelled');bridge.write(path,record);return get(root,ident)
        if value.get('confirmed') is not True:raise ValueError('需要用户在修改预览中明确确认')
        if operation=='undo':return undo(root,path,record)
        if operation!='apply' or record['state']!='pending':raise ValueError('该预览已处理，不能重复执行')
        if time.time()>record['expires_epoch']:raise ValueError('预览已过期，请重新生成')
        if record['requires_paid_confirmation'] and value.get('confirm_paid') is not True:raise ValueError('请额外确认本次可能消耗API额度')
        check_base(root,record)
        # Revalidate execution-time guards. Preserve the frozen, reviewed writes below.
        build(root,record['kind'],record['name'],record['changes'])
        backups={};backupdir=path.parent/ident/'backups'
        # Back up business JSON exactly, including the affected task when retrying/pausing.
        for index,(rel,expected) in enumerate(record['base'].items()):
            target=relative(root,rel);saved=backupdir/(str(index)+'.json')
            if target.is_file():saved.parent.mkdir(parents=True,exist_ok=True);saved.write_bytes(target.read_bytes());backups[rel]=str(saved.relative_to(root))
            elif rel in record['writes']:backups[rel]=None
        record.update(state='executing',backups=backups,paid_confirmed=value.get('confirm_paid') is True,applied_at=bridge.now());bridge.write(path,record)
        written=[]
        try:
            if record['kind']=='task':
                from . import draft_editor as legacy
                for rel,content in record['writes'].items():
                    target=relative(root,rel);bridge.write(target,content)
                    if bridge.read(target)!=content:raise ValueError('重试前审核状态回读不一致')
                action=record['changes']['action'];name=record['name']
                if action=='pause':result=legacy.batch_draft_action('pause',[name],root/'drafts',root)
                elif action=='retry_images':result=legacy.start_image_generation(name,record['changes'].get('language','es'),drafts_dir=root/'drafts',root=root)
                else:result=legacy.start_full_ai_flow(name,root/'drafts',root)
                record['result']={'message':'已提交暂停请求，当前在途请求仍可能完成。' if action=='pause' else '已提交重试；尚未验收生成结果，请查看最新日志。','engine_status':result.get('status') or (result.get('results') or [{}])[0].get('status'),'task_state':get_flow_configuration(root,name)['data']['tasks']}
                record['can_undo']=False
            else:
                for rel,content in record['writes'].items():
                    target=relative(root,rel);written.append(rel);bridge.write(target,content)
                    if bridge.read(target)!=content:raise ValueError('写入回读不一致')
                from .product_core_v3 import review_issues
                issues=review_issues(bridge.read(root/'drafts'/record['name'])) if record['kind'] in {'product','product_prompt'} else []
                record['result']={'message':'已保存并回读核对；本次未发布、未调用付费生成接口。','review_issues':issues,'review_required':bool(record['name']),'saved_changes':record['diffs']}
                record['applied_hashes']={rel:digest(relative(root,rel)) for rel in record['writes']};record['can_undo']=True
            record['state']='applied';bridge.write(path,record)
        except Exception as exc:
            if record['kind']!='task':
                try:
                    restore(root,record,written);record['state']='failed_rolled_back'
                except Exception as rollback_error:record['state']='rollback_failed';record['rollback_error']=str(rollback_error)[:300]
            else:record['state']='failed_or_uncertain'  # Never blindly replay a task request.
            record['error']=bridge.clean(str(exc));record['can_undo']=False;bridge.write(path,record)
        save_audit(root,record,'助手确认执行');return get(root,ident)

def restore(root,record,rels):
    for rel in rels:
        target=relative(root,rel);saved=record['backups'].get(rel)
        if saved is None:
            if target.exists():
                archive=change_path(root,record['id']).parent/record['id']/('recovered-'+uuid.uuid4().hex+'.json');archive.parent.mkdir(parents=True,exist_ok=True);target.replace(archive)
        else:
            data=relative(root,saved).read_bytes();target.parent.mkdir(parents=True,exist_ok=True);tmp=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp');tmp.write_bytes(data);tmp.replace(target)
            if digest(target)!=hashlib.sha256(data).hexdigest():raise ValueError('恢复回读不一致')

def undo(root,path,record):
    if record['state']!='applied' or not record.get('can_undo'):raise ValueError('该操作不能撤销')
    idle(root,record['name'] if record['kind'] in {'product','product_prompt'} else '')
    for rel,expected in record['applied_hashes'].items():
        if digest(relative(root,rel))!=expected:raise ValueError('保存后已有其他修改，为防止覆盖不能撤销')
    record['state']='undoing';bridge.write(path,record)
    try:restore(root,record,list(record['writes']));record.update(state='undone',can_undo=False,result={'message':'已恢复修改前文件并回读核对；期间新建文件已移入可恢复目录。'})
    except Exception as exc:record.update(state='undo_failed',can_undo=False,error=bridge.clean(str(exc)))
    bridge.write(path,record);save_audit(root,record,'助手撤销修改');return get(root,record['id'])


def install_task_tracking(legacy):
    """Track live worker lifetime; paused JSON alone does not establish an idle engine."""
    if getattr(legacy,'_assistant_task_tracking',False):return
    from . import image_generation
    original_full=legacy._run_full_ai_flow;original_image=image_generation.run_job;original_save=legacy._save_full_job
    def tracked(key,fn,*args,**kwargs):
        with LIVE_LOCK:LIVE[key]=LIVE.get(key,0)+1
        try:return fn(*args,**kwargs)
        finally:
            with LIVE_LOCK:
                LIVE[key]-=1
                if not LIVE[key]:LIVE.pop(key,None)
    def full(path,root,full_path):return tracked((str(Path(root).resolve()),Path(path).name),original_full,path,root,full_path)
    def images(root,draft_name,*args,**kwargs):return tracked((str(Path(root).resolve()),draft_name),original_image,root,draft_name,*args,**kwargs)
    def save(full_path,job,**updates):
        if full_path.is_file() and bridge.read(full_path).get('status')=='paused' and updates.get('status')!='paused':raise legacy.EditorError('收到暂停请求，已停止后续流程')
        return original_save(full_path,job,**updates)
    legacy._run_full_ai_flow=full;legacy._save_full_job=save;legacy.run_job=images;image_generation.run_job=images;legacy._assistant_task_tracking=True
