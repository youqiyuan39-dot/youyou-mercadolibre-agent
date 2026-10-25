const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const dom=require('./zying_minidom.cjs'),p=require('../collector_extension/zying_detail_parser.js');
const html=fs.readFileSync(path.join(__dirname,'fixtures/zying_detail_slice9.html'),'utf8');
function read(h){const scope=dom(h).querySelector('main'),entries=p.readDOM(scope);const v=p.parse({skuId:'MLM3536800017',visibleIds:['MLM3536800017'],entries,sourceUrl:'https://meli.zying.net/',capturedAt:'2026-10-22T01:00:00Z'});v.diagnostics=p.diagnostics(scope,entries,v);return v;}
const v=read(html);assert.equal(v.fields.dimensions,'10 × 10 × 10 cm');assert.equal(v.fields.weight,'146 g');assert.equal(v.fields.volumetric_weight,'200.00 g');assert.equal(v.fields.price,'MXN 549.63');assert.equal(v.diagnostics.fields.category_path.reason,'类目未选择');
assert.ok(v.diagnostics.snippets.some(x=>x.text.includes('商品尺寸 inputs=4')));
assert.equal(new Set(v.diagnostics.snippets.map(x=>x.kind+x.text)).size,v.diagnostics.snippets.length);
assert.ok(Buffer.byteLength(JSON.stringify(v))<32768);
for(const attr of ['aria-valuenow','data-value']) assert.equal(read(html.replace('type="number" value="10"','type="number" '+attr+'="10"')).fields.dimensions,'10 × 10 × 10 cm');
assert.equal(read(html.replaceAll('value="10"','value=""')).fields.dimensions,''); // never borrow price/commission/weight
const sibling=html.replace('<div class="el-form-item"><div class="el-form-item__label"><span>商品尺寸</span></div><div class="el-form-item__content">','<div class="el-form-item"><div><span>商品尺寸</span></div><div class="el-form-item__content">');
assert.equal(read(sibling).fields.dimensions,'10 × 10 × 10 cm');
(async()=>{
 let listener,mode='ok';
 const chrome={storage:{local:{get:async()=>{if(mode==='storage')throw Error('quota test');return {};},set:async()=>{}}},tabs:{onUpdated:{addListener(){}}},runtime:{getManifest:()=>({version:'0.8.7'}),onMessage:{addListener:f=>listener=f}}};
 const ctx=vm.createContext({chrome,URL,Date,AbortController,setTimeout,clearTimeout,fetch:async()=>{if(mode==='offline')throw Error('Failed to fetch');if(mode==='parse')return {ok:true,status:200,json:async()=>{throw Error('Unexpected token')}};return {ok:mode!=='http',status:mode==='http'?403:200,json:async()=>mode==='http'?{error:'extension_not_allowed'}:{ok:true}};}});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../collector_extension/background.js'),'utf8'),ctx);
 const send=type=>new Promise(resolve=>{assert.equal(listener({type,payload:{}},{tab:{url:'https://meli.zying.net/'}},resolve),true);});
 mode='storage';let r=await send('YOUYOU_OFFER_RESERVE');assert.equal(r.step,'rate_storage');assert.match(r.error,/storage.local.get.*quota test/);
 for(const [m,s] of [['offline','background→8789'],['parse','response_parse'],['http','receiver_http'],['ok','saved']]){mode=m;r=await send('YOUYOU_ZYING_DETAIL');assert.equal(r.step,s);assert.equal(r.background_extension_version,'0.8.7');}
 let runtimeMode='empty';const content=vm.createContext({chrome:{runtime:{sendMessage:async()=>{if(runtimeMode==='invalid')throw Error('Extension context invalidated');return undefined;}}}});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../collector_extension/local_transport.js'),'utf8'),content);
 await assert.rejects(content.YouyouLocal.send({type:'YOUYOU_OFFER_RESERVE'}),/content→background.*后台未响应.*刷新/);runtimeMode='invalid';await assert.rejects(content.YouyouLocal.send({}),/Extension context invalidated.*刷新/);
 for(const file of ['content.js','zying_detail.js','offer_candidates.js','market_reference.js'])assert.doesNotMatch(fs.readFileSync(path.join(__dirname,'../collector_extension',file),'utf8'),/\bfetch\s*\(|XMLHttpRequest/);
 console.log('slice9 row scope, 3 fallback attributes, adjacent volume, dedup, transport/storage/http/parse errors passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
