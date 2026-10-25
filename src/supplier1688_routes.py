"""Loopback, same-origin UI routes. No credential GET, echo or request logging."""
import json
from pathlib import Path
from urllib.parse import urlparse
from . import supplier1688_runtime as runtime
from .source_intake import IntakeError

PREFIX='/api/v3/supplier1688/'
WEB=Path(__file__).resolve().parents[1]/'web'


def get(handler,root,path):
    if path in {'/supplier1688','/supplier1688.js'}:
        name='supplier1688.html' if path=='/supplier1688' else 'supplier1688.js'
        handler._file(WEB/name,'text/html; charset=utf-8' if name.endswith('.html') else 'text/javascript; charset=utf-8')
        return True
    if not path.startswith(PREFIX):return False
    try:
        if path==PREFIX+'status':
            handler._json(200,{'ok':True,**runtime.public_status(root),
                              'setup_only':getattr(handler.server,'supplier_setup_only',False)})
        else:handler._json(404,{'ok':False,'error':'未接入的1688读取入口'})
    except Exception:handler._json(500,{'ok':False,'error':'1688本机状态读取未完成'})
    return True


def post(handler,root,path):
    if not path.startswith(PREFIX):return False
    try:
        host=str(handler.headers.get('Host') or '')
        origin=urlparse(str(handler.headers.get('Origin') or ''))
        if host.split(':')[0] not in {'localhost','127.0.0.1'} or origin.scheme!='http' or origin.netloc!=host:
            raise runtime.Error('ORIGIN_REJECTED','只接受当前本机配置页面提交')
        if handler.headers.get('Content-Type','').split(';')[0]!='application/json':
            raise runtime.Error('CONTENT_TYPE_REJECTED','只接受当前页面的JSON请求')
        length=int(handler.headers.get('Content-Length') or 0)
        if not 0<length<=16_000:raise runtime.Error('INPUT_INVALID','本机请求大小不正确')
        try:value=json.loads(handler.rfile.read(length).decode('utf-8'))
        except (ValueError,UnicodeError):raise runtime.Error('INPUT_INVALID','本机请求无法解析') from None
        if not isinstance(value,dict):raise runtime.Error('INPUT_INVALID','本机请求格式不正确')
        if path==PREFIX+'configure':output=runtime.configure(root,value)
        elif path==PREFIX+'fetch':output=runtime.fetch_detail(root,value)
        elif path==PREFIX+'preview':output={'ok':True,**runtime.preview_import(root,value.get('result_id'),value.get('quote_basis'))}
        elif path==PREFIX+'import':
            if getattr(handler.server,'supplier_setup_only',False):
                raise runtime.Error('WORKBENCH_RESTART_REQUIRED','当前是准备页；请先保存编辑并正常重开工作台，再从商品采集入口导入。未启动旧版处理。')
            output=runtime.import_result(root,value)
        else:raise runtime.Error('ROUTE_NOT_FOUND','未接入的1688操作')
        handler._json(200,output)
    except runtime.Error as exc:handler._json(400,{'ok':False,'error':str(exc),'code':exc.code,'diagnostics':exc.diagnostics})
    except IntakeError:handler._json(400,{'ok':False,'error':'真实业务响应尚不满足现有导入字段要求；原响应已保留，请核对商品/SKU/spec及包装、价格字段','code':'ADAPTER_INPUT_REQUIRED'})
    except Exception:handler._json(500,{'ok':False,'error':'1688本机处理未完成；没有输出配置、签名或原始异常','code':'LOCAL_FAILURE'})
    return True
