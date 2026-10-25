const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const parser=require('../collector_extension/zying_detail_parser.js'),dom=require('./zying_minidom.cjs');
const html=fs.readFileSync(path.join(__dirname,'fixtures/zying_detail_page.html'),'utf8');
const visible=el=>!el.closest('[hidden]');
function snapshot(markup){
 const doc=dom(markup),scope=doc.querySelector('main'),entries=parser.readDOM(scope,visible);
 const p=parser.parse({skuId:'MLM6281697608',visibleIds:['MLM6281697608'],entries,sourceUrl:'https://meli.zying.net/#/goods/MLM6281697608',capturedAt:'2026-10-21T12:00:00Z',mainImage:parser.mainImage(scope,doc,visible)});
 p.extension_version='0.8.5';p.diagnostics=parser.diagnostics(scope,entries,p,visible);return p;
}
(async()=>{
 let reads=0,delays=0;
 const result=await parser.waitForSnapshot(()=>{reads++;return snapshot(reads<3?html.replace('data-live="550"','data-live=""'):html);},async ms=>{assert.equal(ms,500);delays++;});
 assert.equal(reads,3);assert.equal(delays,2);assert.equal(result.diagnostics.attempts,3);
 let notReady=0;await parser.waitForSnapshot(()=>{if(++notReady<3)throw Error('详情区域未渲染');return snapshot(html);},async()=>{});assert.equal(notReady,3);
 assert.equal(result.fields.weight,'550 g');assert.equal(result.parser_version,parser.VERSION);
 const missing=snapshot(html.replace('data-live="550"','data-live=""'));
 assert.match(missing.diagnostics.fields.weight.reason,/计算器未预填/);
 assert.match(missing.diagnostics.fields.category_path.reason,/类目未选择/);
 assert.match(missing.diagnostics.fields.profit.reason,/利润未试算/);
 assert.ok(missing.diagnostics.snippets.every(s=>s.text.length<=200));
 let timeout=0;const expired=await parser.waitForSnapshot(()=>{timeout++;return snapshot('<main><p><span>卖家:</span><a>TEST</a></p></main>');},async()=>{});
 assert.equal(timeout,10);assert.equal(expired.fields.seller,'TEST');
 assert.match(expired.diagnostics.fields.dimensions.reason,/未找到标签/);
 assert.match(expired.diagnostics.fields.main_image.reason,/图片未加载/);
 // Wrapper variant, calculator outside the information section.
 const wrapped=snapshot(html.replace('class="el-form-item"','class="field-row"').replace('商品重量(g)','商品重量（g）'));
 assert.equal(wrapped.fields.weight,'550 g');
 assert.equal(snapshot(html.replace('data-live="18.5%"','data-live="18.5"')).fields.commission,'18.5%');
 assert.ok(Buffer.byteLength(JSON.stringify({operation:'zying_detail',detail:result}))<=32768);
 const web=fs.readFileSync(path.join(__dirname,'../web/workbench_v3.js'),'utf8');
 const render=require('node:vm').runInNewContext(web.slice(0,web.indexOf('function issueSummary'))+'\ncontributionSummary',{esc:x=>String(x??'').replace(/</g,'&lt;')});
 assert.match(render({decision:'PENDING',input_requests:[{field:'fx_source'}]}),/PENDING.*缺项：fx_source/);
 assert.match(render({decision:'PASS',unit_contribution:20,sales_margin_pct:20}),/PASS.*20 MXN/);
 assert.ok(web.includes('${contributionSummary(r.project_contribution)}${zyingProfitSummary(r.zying_profit_reference)}'));
 const source=fs.readFileSync(path.join(__dirname,'../collector_extension/zying_detail.js'),'utf8');
 assert.doesNotMatch(source,/\.click\s*\(|dispatchEvent\s*\(/);
 assert.match(source,/waitForSnapshot/);assert.match(source,/extension_version/);
 if(process.env.ZYING_SLICE7_DOM_OUT)fs.writeFileSync(process.env.ZYING_SLICE7_DOM_OUT,JSON.stringify(result));
 console.log('slice7: SPA retry/timeout, diagnostics, wrapper DOM, version, bounds, separate profit UI passed');
})().catch(e=>{console.error(e);process.exitCode=1});
