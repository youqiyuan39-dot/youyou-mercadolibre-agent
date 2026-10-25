"""One-purpose signed detail client. Credentials stay inside this local runtime.

No search, order creation, token refresh, redirects, automatic retry or paid
model fallback. Public methods return only flags and sanitized business data.
"""
import base64
import copy
import hashlib
import hmac
import json
import re
import ssl
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .atomic_json_v3 import write
from . import mercadolibre_oauth as private_store

VERSION='1688_DETAIL_RUNTIME_20261018_V3'
LOCK=threading.RLock()
MAX_RESPONSE=8_000_000
VAULT='supplier1688_app.dpapi'
METHOD='alibaba.product.get'
NAMESPACE='com.alibaba.product'


class Error(ValueError):
    def __init__(self, code, message, diagnostics=None):
        super().__init__(message)
        self.code=code
        self.diagnostics=diagnostics or {}


class _HTTPFailure(Error):
    """Transient in-memory error bytes, never returned or journaled verbatim."""
    def __init__(self, status, data):
        super().__init__('HTTP_'+str(status),'详情接口返回HTTP '+str(status))
        self.status=status
        self.data=data


def _provider_diagnostic(data, private_values=()):
    """Classify provider errors into fixed labels; do not echo arbitrary strings."""
    try:
        body=json.loads(data.decode('utf-8')) if isinstance(data,bytes) else data
        if not isinstance(body,dict):return {'provider_category':'UNCLASSIFIED'}
        body=_sanitize_business(body,private_values)
        code=str(body.get('error_code') or '')
        mapped={'gw.APIACLDecline':'API_PERMISSION_REQUIRED','gw.AppKeyNotFound':'APP_CONFIGURATION_REJECTED',
                'gw.SignatureInvalid':'SIGNATURE_INVALID','gw.InvalidTimestamp':'TIMESTAMP_INVALID'}
        # Unknown codes are only exposed when the calling runtime supplied all
        # private values for redaction. Standalone classification stays closed.
        safe_code=code if (private_values or code in mapped) and re.fullmatch(r'(?:[A-Za-z][A-Za-z0-9_.-]{0,79}|\d{1,4}(?:_\d{1,3})?)',code) else 'SUPPRESSED'
        if code in mapped:return {'provider_category':mapped[code],'provider_code':safe_code,'diagnostic_basis':'PROVIDER_CODE'}
        fields=[body.get(k) for k in ('error_code','error_message','error_description','error','errMsg','errorMessage','message')]
        text=' '.join(v for v in fields if isinstance(v,str)).lower()
    except (ValueError,UnicodeError):return {'provider_category':'NON_JSON_ERROR'}
    rules=[
        ('SIGNATURE_INVALID',('signature','签名')),
        ('TIMESTAMP_INVALID',('timestamp','时间戳')),
        ('APP_CONFIGURATION_REJECTED',('invalid_app','invalidapp','appkey not found','appkey不存在','应用不存在')),
        ('USER_AUTHORIZATION_REJECTED',('access_token','accesstoken','授权过期','token')),
        ('API_PERMISSION_REQUIRED',('permission','unauthorized','权限','无权','未订购')),
        ('RATE_OR_QUOTA_LIMIT',('quota','limit','频率','配额','流量不足')),
        ('PRODUCT_UNAVAILABLE',('product not','product_not','商品不存在','商品已下架')),
        ('PARAMETER_INVALID',('parameter','argument','参数')),
    ]
    for category,needles in rules:
        if any(n in text for n in needles):return {'provider_category':category,'provider_code':safe_code,'diagnostic_basis':'MESSAGE_KEYWORD_HEURISTIC'}
    return {'provider_category':'UNCLASSIFIED','provider_code':safe_code,'diagnostic_basis':'UNCLASSIFIED'}


def _diagnostic_message(diagnostic):
    return {
        'SIGNATURE_INVALID':'网关提示签名校验问题',
        'TIMESTAMP_INVALID':'网关提示时间戳问题',
        'APP_CONFIGURATION_REJECTED':'网关提示应用配置问题',
        'USER_AUTHORIZATION_REJECTED':'网关提示用户授权问题',
        'API_PERMISSION_REQUIRED':'网关提示接口权限问题',
        'RATE_OR_QUOTA_LIMIT':'网关提示频率或额度问题',
        'PRODUCT_UNAVAILABLE':'网关提示商品不可用',
        'PARAMETER_INVALID':'网关提示请求参数问题',
        'NON_JSON_ERROR':'网关未返回JSON错误信息',
    }.get(diagnostic.get('provider_category'),'错误类型尚未识别')


def stamp():return datetime.now(timezone.utc).isoformat()


def location(root):return Path(root)/'data/v3/supplier1688'


def vault(root):return Path(root)/'.secrets'/VAULT


def public_status(root):
    # Never decrypt merely to render a settings page.
    return {'version':VERSION,'configured':vault(root).is_file(),'credential_values_returned':False,
            'scope':'ONE_READ_ONLY_PRODUCT_GET','automatic_retries':0,
            'permissions':'NOT_VERIFIED','fees':'REQUIRES_ACCOUNT_CONFIRMATION',
            'last_result':last_result(root)}


def last_result(root):
    p=location(root)/'last-result.json'
    try:return json.loads(p.read_text('utf-8')) if p.is_file() else None
    except (ValueError,OSError):return None


def configure(root, value):
    credentials={k:value.get(k) for k in ('app_key','app_secret','access_token')}
    if not isinstance(credentials['app_key'],str) or not re.fullmatch(r'\d{4,30}',credentials['app_key']):
        raise Error('CONFIG_INVALID','请输入本应用的完整配置；不会回显输入')
    if any(not isinstance(credentials[k],str) or not 6<=len(credentials[k])<=2048 or
           credentials[k]!=credentials[k].strip() or any(ord(c)<32 for c in credentials[k])
           for k in ('app_secret','access_token')):
        raise Error('CONFIG_INVALID','密钥或授权配置为空、含空白或格式无效；未保存')
    if value.get('own_application_confirmed') is not True:
        raise Error('CONFIG_CONFIRMATION_REQUIRED','请确认这些配置属于当前企业自用应用')
    target=vault(root)
    with LOCK:
        target.parent.mkdir(parents=True,exist_ok=True)
        candidate=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            private_store.save_private_json(candidate,credentials)
            candidate.replace(target)
        except Exception:
            # Do not return the provider or DPAPI exception, which may retain
            # request/context objects. The temporary file is already encrypted.
            raise Error('CONFIG_SAVE_FAILED','本机加密配置未保存成功；请检查当前Windows账户权限') from None
    return {'ok':True,'configured':True,'credential_values_returned':False}


def _credentials(root):
    try:
        value=private_store.load_private_json(vault(root))
        if not all(isinstance(value.get(k),str) and value[k] for k in ('app_key','app_secret','access_token')):
            raise ValueError('invalid config')
        return value
    except Exception:
        raise Error('CONFIG_REQUIRED','请由本人在本机配置页保存应用配置；无需发送到聊天') from None


def _signature(path, params, secret):
    text=path+''.join(str(k)+str(params[k]) for k in sorted(params))
    return hmac.new(secret.encode('utf-8'),text.encode('utf-8'),hashlib.sha1).hexdigest().upper()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise Error('REDIRECT_REJECTED','详情接口发生重定向，已停止；没有向其他地址转发配置')


def _transport(url, body):
    request=Request(url,data=body,method='POST',headers={'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8','Accept':'application/json'})
    opener=build_opener(ProxyHandler({}),NoRedirect(),HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request,timeout=15) as response:
            data=response.read(MAX_RESPONSE+1)
            if len(data)>MAX_RESPONSE:raise Error('RESPONSE_TOO_LARGE','详情响应超过读取上限，未导入')
            if response.status!=200:raise Error('HTTP_FAILURE','详情请求未成功；未自动重试')
            return data
    except HTTPError as exc:
        status=int(exc.code)
        try:data=exc.read(MAX_RESPONSE+1)
        except Exception:data=b''
        finally:exc.close()
        raise _HTTPFailure(status,data if len(data)<=MAX_RESPONSE else b'') from None
    except Error:raise
    except (URLError,OSError,TimeoutError) as exc:
        cause=exc.reason if isinstance(exc,URLError) else exc
        category=('TLS_CERTIFICATE' if isinstance(cause,ssl.SSLCertVerificationError) else
                  'TLS' if isinstance(cause,ssl.SSLError) else
                  'TIMEOUT' if isinstance(cause,(TimeoutError,)) else
                  'DNS' if type(cause).__name__=='gaierror' else 'CONNECTION')
        raise Error('NETWORK_FAILED','详情请求超时或网络失败；结果不明，未自动重试',{'network_category':category}) from None
    except Exception:
        raise Error('TRANSPORT_FAILED','详情请求未完成；未保存网络异常上下文，未自动重试') from None


def _sanitize_business(value, private_values):
    sensitive=re.compile(r'(?:token|secret|password|cookie|signature|authorization|api.?key|app.?key)',re.I)
    replacements=set()
    for text in private_values:
        if text:
            replacements.add(text);replacements.add(quote(text,safe=''))
    def walk(item):
        if isinstance(item,dict):return {str(k):walk(v) for k,v in item.items() if not sensitive.search(str(k))}
        if isinstance(item,list):return [walk(v) for v in item]
        if isinstance(item,str):
            for private in sorted(replacements,key=len,reverse=True):item=item.replace(private,'[REDACTED]')
            return item
        if isinstance(item,(int,float)) and str(item) in replacements:return '[REDACTED]'
        return item
    return walk(value)


def result(root, result_id):
    if not re.fullmatch(r'[a-f0-9]{32}',str(result_id)):raise Error('RESULT_INVALID','响应编号无效')
    path=location(root)/'responses'/(result_id+'.json')
    audit=location(root)/'responses'/(result_id+'.audit.json')
    if not path.is_file() or not audit.is_file():raise Error('RESULT_NOT_FOUND','本次未取得可导入的真实业务响应')
    return {'ok':True,'response':json.loads(path.read_text('utf-8')),'audit':json.loads(audit.read_text('utf-8'))}


def fetch_detail(root, value):
    """One consent authorizes exactly one request for one product; no retries."""
    product_id=str(value.get('product_id') or '')
    operation_id=str(value.get('operation_id') or '')
    if not re.fullmatch(r'\d{6,20}',product_id) or not re.fullmatch(r'[a-f0-9]{32}',operation_id):
        raise Error('INPUT_INVALID','请输入一个真实1688商品编号；每次查询必须有独立操作编号')
    if value.get('authorize_one_read') is not True or value.get('no_extra_fee_confirmed') is not True:
        raise Error('CONSENT_REQUIRED','请先核对该账户的接口权限、额度及本次无额外收费，并明确授权1次详情只读请求')
    root=Path(root);journal=location(root)/'operations'/(operation_id+'.json')
    with LOCK:
        if journal.is_file():
            prior=json.loads(journal.read_text('utf-8'))
            if prior.get('product_id')!=product_id:raise Error('OPERATION_CONFLICT','操作编号已对应其他商品；未调用接口')
            if prior.get('state') in {'RESPONSE_RECEIVED','SCHEMA_REVIEW_REQUIRED'}:return {'ok':True,'duplicate':True,**result(root,operation_id)}
            raise Error('OPERATION_ALREADY_USED','这次请求已执行或结果不明；已阻止重复调用，请先检查结果')
        credentials=_credentials(root)
        entry={'version':VERSION,'product_id':product_id,'operation_id':operation_id,
               'method':METHOD,'state':'IN_FLIGHT','started_at':stamp(),'request_limit':1,'requests_attempted':1,
               'authorization':'EXPLICIT_LOCAL_UI_ONE_READ','fees_confirmed_by_operator':'NO_EXTRA_FEE',
               'fees_independently_verified':False,'request_or_credentials_stored':False}
        write(journal,entry)
    started=time.monotonic()
    try:
        path='param2/1/'+NAMESPACE+'/'+METHOD+'/'+credentials['app_key']
        params={'productID':product_id,'webSite':'1688','scene':'1688',
                'access_token':credentials['access_token'],'_aop_timestamp':str(int(time.time()*1000))}
        signature=_signature(path,params,credentials['app_secret'])
        params['_aop_signature']=signature
        try:raw=_transport('https://gw.open.1688.com/openapi/'+path,urlencode(params,encoding='utf-8').encode('ascii'))
        except _HTTPFailure as exc:
            diagnostic={**_provider_diagnostic(exc.data,[*credentials.values(),signature]),'http_status':exc.status}
            raise Error(exc.code,'详情接口返回HTTP '+str(exc.status)+'；'+_diagnostic_message(diagnostic)+'。未自动重试，未保存错误正文',diagnostic) from None
        try:body=json.loads(raw.decode('utf-8'))
        except (ValueError,UnicodeError):raise Error('RESPONSE_INVALID','详情接口没有返回可解析的业务JSON') from None
        if not isinstance(body,dict):raise Error('RESPONSE_INVALID','详情接口业务JSON不是对象')
        if body.get('error_code') or body.get('errMsg') or body.get('success') is False:
            diagnostic=_provider_diagnostic(body,[*credentials.values(),signature])
            raise Error('BUSINESS_REJECTED','详情接口返回业务错误；'+_diagnostic_message(diagnostic)+'。未保存错误正文',diagnostic)
        business=_sanitize_business(body,[*credentials.values(),signature])
        # JSON after sanitizing is the ONLY response persisted or returned.
        product=business.get('productInfo')
        identity=bool(isinstance(product,dict) and str(product.get('productID'))==product_id)
        audit={**entry,'state':'RESPONSE_RECEIVED' if identity else 'SCHEMA_REVIEW_REQUIRED','request_success':True,'business_identity_verified':identity,
               'observed_at':stamp(),'seconds':round(time.monotonic()-started,4),
               'response_sha256':hashlib.sha256(json.dumps(business,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
               'provenance':'LIVE_PROJECT_SIGNED_HTTP','automatic_retries':0,'field_sufficiency':'NOT_YET_VERIFIED'}
        write(location(root)/'responses'/(operation_id+'.json'),business)
        write(location(root)/'responses'/(operation_id+'.audit.json'),audit)
        write(journal,audit);write(location(root)/'last-result.json',{k:audit[k] for k in ('operation_id','product_id','state','observed_at','seconds')})
        return {'ok':True,'duplicate':False,'response':business,'audit':audit}
    except Error as exc:
        entry.update(state='NOT_COMPLETED',error_code=exc.code,diagnostics=exc.diagnostics,seconds=round(time.monotonic()-started,4))
        write(journal,entry)
        write(location(root)/'last-result.json',{k:entry[k] for k in ('operation_id','product_id','state','error_code','diagnostics','seconds')})
        raise
    except Exception:
        entry.update(state='NOT_COMPLETED',error_code='LOCAL_FAILURE',seconds=round(time.monotonic()-started,4))
        write(journal,entry)
        raise Error('LOCAL_FAILURE','本机详情处理未完成；原始业务资料保留，未输出配置或请求上下文') from None


def preview_import(root, result_id, quote_basis=None):
    from .supplier_api_v3 import adapt_product_get
    saved=result(root,result_id);audit=saved['audit']
    if audit.get('business_identity_verified') is not True:raise Error('SCHEMA_REVIEW_REQUIRED','真实响应已保存，需先核对响应封装及请求商品身份；未导入')
    return adapt_product_get(saved['response'],audit['product_id'],observed_at=audit['observed_at'],
                             evidence_kind='LIVE_OFFICIAL_API',quote_basis=quote_basis)


def import_result(root, value):
    if value.get('confirmed') is not True:raise Error('IMPORT_CONFIRMATION_REQUIRED','请先核对真实返回的SKU、单位及字段缺口，再导入本机处理')
    saved=result(root,value.get('result_id'));audit=saved['audit']
    if audit.get('provenance')!='LIVE_PROJECT_SIGNED_HTTP':raise Error('PROVENANCE_INVALID','此响应不是本项目真实签名HTTP取得的结果')
    if audit.get('business_identity_verified') is not True:raise Error('SCHEMA_REVIEW_REQUIRED','响应封装及商品身份尚未核对，未导入')
    from .supplier_api_v3 import import_product_get
    imported=import_product_get(root,saved['response'],audit['product_id'],observed_at=audit['observed_at'],
                                quote_basis=value.get('quote_basis'),evidence_kind='LIVE_OFFICIAL_API',confirmed=True)
    data=imported['intake']
    return {'ok':True,'audit':imported['audit'],'draft':Path(data['draft']).name,
            'auto_processing':data.get('auto_processing'),'publishing_performed':False}
