const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('web/workbench_v3.js','utf8');
const ids=['content','inputRequestFile','inputRequestPreview','inputRequestResult','inputRequestConfirm','notice'];
const elements=Object.fromEntries(ids.map(id=>[id,{innerHTML:'',textContent:'',disabled:false,files:[]}]));
let calls=[],invalid=false;
const row={request_id:'ir_abc',draft:'a-draft.json',product:'<img onerror=evil()>',sku_id:'s1',field:'weight_g',reason:'缺重量',source:'draft.input_requests[0]',current_value:null,fill_value:'',editable:true,validation:'g/kg'};
const ctx={document:{getElementById:id=>elements[id],querySelectorAll:()=>[]},console,URL,
fetch:async(path,options)=>{const body=options?.body?JSON.parse(options.body):undefined;calls.push({path,body});
 let data=path==='/api/v3/input-requests'?{count:1,rows:[row],warnings:[]}:
 body.mode==='dry_run'?{valid:!invalid,count:1,updates:[{...row,value:250}],errors:invalid?[{row:1,error:'单位错误'}]:[],preview_hash:'preview-hash'}:
 {valid:true,written:[{draft:row.draft}],errors:[]};return{ok:true,json:async()=>({ok:true,...data})};}};
vm.createContext(ctx);vm.runInContext(source.slice(0,source.indexOf('async function showOverview(')),ctx);
(async()=>{
 await ctx.showInputRequests();
 assert(elements.content.innerHTML.includes('导出 CSV'));
 assert(elements.content.innerHTML.includes('aria-label="统一待补表"'));
 assert(elements.content.innerHTML.includes('&lt;img onerror=evil()&gt;'));
 assert(!elements.content.innerHTML.includes('<img onerror=evil()>'));
 assert.equal(calls.filter(x=>x.body).length,0);
 elements.inputRequestFile.files=[{name:'fill.csv',size:50,text:async()=> 'request_id,fill_value\nir_abc,250 g'}];
 elements.inputRequestFile.onchange();
 assert.equal(elements.inputRequestPreview.disabled,false);
 await elements.inputRequestPreview.onclick();
 assert.equal(calls.at(-1).body.mode,'dry_run');
 assert.equal(calls.filter(x=>x.body?.mode==='confirm').length,0);
 assert(elements.inputRequestResult.innerHTML.includes('确认保存'));
 await elements.inputRequestConfirm.onclick();
 const confirmed=calls.find(x=>x.body?.mode==='confirm').body;
 assert.equal(confirmed.confirm,true);assert.equal(confirmed.preview_hash,'preview-hash');
 assert.equal(confirmed.content,'request_id,fill_value\nir_abc,250 g');
 invalid=true;
 await elements.inputRequestPreview.onclick();
 assert(elements.inputRequestResult.innerHTML.includes('单位错误'));
 assert(elements.inputRequestResult.innerHTML.includes('id="inputRequestConfirm" disabled'));
 assert(calls.every(x=>!x.path.includes('publish')));
 console.log('Input table: escaped values, labelled scroll table, export/upload, dry-run before explicit confirm, hash binding and invalid-preview disabling passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
