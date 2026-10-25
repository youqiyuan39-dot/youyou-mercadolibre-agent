const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const parser=require('../collector_extension/offer_candidates_parser.js'),dom=require('./zying_minidom.cjs');
const html=n=>fs.readFileSync(path.join(__dirname,'fixtures/offer_dom_'+n+'.html'),'utf8');
const read=n=>parser.read(dom(html(n)),'https://meli.zying.net/');
const normal=read('normal');assert.equal(normal.candidates.length,1);
const c=normal.candidates[0];assert.equal(c.offer_id,'900000001');assert.equal(c.price,'¥18-20');assert.equal(c.currency,'CNY');assert.equal(c.min_order,'2件起批');assert.equal(c.shop_name,'Fixture factory');assert.ok(!c.offer_url.includes('?'));assert.ok(!c.image_url.includes('?'));
assert.equal(read('empty').candidates.length,0);
assert.equal(parser.read(dom(html('normal')+'<section id="youyou-offer-candidates">请登录，验证码待处理</section>'),'https://meli.zying.net').status,'READY');
assert.equal(parser.read(dom(html('normal').replace(/<img[^>]*>/g,'')),'https://meli.zying.net').candidates[0].image_url,'');
for(const n of ['slider','login']){assert.equal(read(n).status,'BLOCKED_ON_LOGIN');assert.deepEqual(read(n).candidates,[]);}
let many='';for(let i=0;i<60;i++)many+=html('normal').replaceAll('900000001',String(900000000+i));assert.equal(parser.read(dom(many),'https://meli.zying.net').candidates.length,40);
assert.ok(parser.read(dom(html('normal').replace('filter VF3500 black pack of 2','X'.repeat(600))),'https://meli.zying.net').candidates[0].title.length<=300);
assert.equal(parser.read(dom(html('normal').replace('detail.1688.com','evil.invalid')),'https://meli.zying.net').candidates.length,0);
(async()=>{
 let stored={},now=1792584000000,listener;const posts=[];
 const chrome={storage:{local:{get:async()=>stored,set:async v=>{stored={...stored,...v};}}},tabs:{onUpdated:{addListener(){}}},runtime:{getManifest:()=>({version:'0.8.7'}),onMessage:{addListener(f){listener=f;}}}};
 class FakeDate extends Date{constructor(...a){super(...(a.length?a:[now]));}static now(){return now;}}
 const context=vm.createContext({chrome,URL,Date:FakeDate,fetch:async(url,req)=>{posts.push([url,req]);return {ok:true,json:async()=>({ok:true})};}});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../collector_extension/background.js'),'utf8'),context);
 const reserve=()=>new Promise(resolve=>listener({type:'YOUYOU_OFFER_RESERVE'},{tab:{url:'https://meli.zying.net/'}},resolve));
 let r=await reserve();assert.equal(r.rate_limit.daily_count,1);assert.equal(posts.length,0);
 const concurrent=await Promise.all([reserve(),reserve()]);assert.ok(concurrent.every(x=>!x.ok));
 for(let i=2;i<=50;i++){now+=5000;r=await reserve();assert.equal(r.rate_limit.daily_count,i);}
 now+=5000;assert.match((await reserve()).error,/50/);
 now+=86400000;assert.equal((await reserve()).rate_limit.daily_count,1);
 assert.deepEqual(Object.keys(stored.offerCandidateRate).sort(),['count','day','last']);
 for(const name of ['offer_candidates.js','offer_candidates_parser.js']){const source=fs.readFileSync(path.join(__dirname,'../collector_extension',name),'utf8');assert.doesNotMatch(source,/fetch\s*\(|\.click\s*\(|dispatchEvent\s*\(|XMLHttpRequest|localStorage|document\.cookie/);}
 const web=fs.readFileSync(path.join(__dirname,'../web/workbench_v3.js'),'utf8');
 const fn=vm.runInNewContext(web.slice(web.indexOf('function offerCandidatesSummary'),web.indexOf('function issueSummary'))+'\nofferCandidatesSummary',{esc:x=>String(x??'').replaceAll('<','&lt;')});
 const rendered=fn({status:'SKIPPED',top3:[{title:'<script>',price:'18-20',confidence:.9,source_url:'javascript:alert(1)',status:'SKIPPED'}]});assert.ok(rendered.includes('&lt;script>'));assert.ok(!rendered.includes('href='));assert.match(rendered,/CNY/);
 console.log('slice8: four DOM fixtures, bounds, dedup, links, serialized interval/daily cap/rollover and UI passed');
})().catch(e=>{console.error(e);process.exitCode=1});
