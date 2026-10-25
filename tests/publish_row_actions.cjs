const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('web/publish.js','utf8');
const app={innerHTML:''},events={},calls=[],alerts=[],dialog={open:false,showModal(){this.open=true},close(){this.open=false}},deleteTitle={textContent:''};
const name='sample-draft.json';
const ctx={document:{querySelector:s=>({'#app':app,'#deleteDialog':dialog,'#deleteTitle':deleteTitle})[s]||null,addEventListener:(type,fn)=>events[type]=fn},URL,console,
 alert:s=>alerts.push(s),fetch:async(url,options)=>{
  calls.push({url,body:options?.body&&JSON.parse(options.body)});
  return {ok:true,json:async()=>url==='/api/drafts/batch'?{ok:true,results:[{status:'recycled'}]}:{ok:true,items:[]}};
 }};
vm.createContext(ctx);vm.runInContext(source.slice(0,source.indexOf('api("/api/v3/publish/stores")')),ctx);
vm.runInContext(`rows=[{name:${JSON.stringify(name)},title:'Example',in_publish_center:true}];storeId='store';render()`,ctx);
assert(app.innerHTML.includes('AI 生图处理'));assert(app.innerHTML.includes('/editor?draft=sample-draft.json#aiImages'));
assert(app.innerHTML.includes('data-delete="sample-draft.json"'));assert(!app.innerHTML.includes('复制为新商品'));
assert.equal(calls.length,0,'Rendering the image entry must not start generation');
(async()=>{
 const button={dataset:{delete:name},disabled:false,hasAttribute:()=>false};
 await events.click({target:{closest:()=>button}});assert(dialog.open);assert.equal(deleteTitle.textContent,'Example');
 const control=id=>({id,dataset:{},disabled:false,hasAttribute:()=>false});
 await events.click({target:{closest:()=>control('cancelDelete')}});assert(!dialog.open);assert.equal(calls.length,0,'Cancel must leave the product untouched');
 await events.click({target:{closest:()=>button}});await events.click({target:{closest:()=>control('confirmDelete')}});
 assert.equal(calls[0].url,'/api/drafts/batch');assert.deepEqual(calls[0].body,{action:'recycle',names:[name]});
 assert(!calls.some(c=>/generate|publish\/commit/.test(c.url)));assert.equal(alerts.length,0);assert(app.innerHTML.includes('已移入本机回收站，可恢复'));
 const wb=fs.readFileSync('web/workbench_v3.js','utf8'),ed=fs.readFileSync('web/editor.js','utf8');
 assert(wb.includes('openLegacy(a.pathname+a.search+a.hash)'));assert(ed.includes('byId("aiImages").scrollIntoView'));
 console.log('Publish image navigation, delete/cancel/reload behavior, and zero generation/publication requests passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
