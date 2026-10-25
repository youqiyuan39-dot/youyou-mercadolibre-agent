const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('web/editor.js','utf8');
class Node {
 constructor(tag='div'){this.tag=tag;this.nodes={};this.children=[];this.listeners={};this.classList={add(){}};}
 set innerHTML(value){this.html=value;this.nodes={};this.children=[];}
 querySelector(selector){return this.nodes[selector]||(this.nodes[selector]=new Node(selector));}
 append(...nodes){this.children.push(...nodes);}
 appendChild(node){this.append(node);}
 add(option){this.append(option);}
 setAttribute(key,value){this[key]=value;}
 addEventListener(type,fn){this.listeners[type]=fn;}
}
const root=new Node(),summary=new Node(),a={remote_url:'a',review_status:'AUTO_SCREENED'},b={remote_url:'b'},blocked={remote_url:'blocked',reject:true};
const ctx={document:{createElement:tag=>new Node(tag)},byId:id=>id==='sourceGallery'?root:summary,
 sourceGallery:[blocked,a,b],gallery:[],skuDetails:[],skuListingGalleries:[],skuSourceAssignments:[],currentDraft:{evidence:{image_review:{}}},
 imageKey:i=>i.remote_url||i.local_file,text:x=>String(x||''),displayedImageUrl:i=>i.remote_url,
 blockedSourceImage:i=>!!i.reject,renderGallery(){},renderSkuListingEditor(){},downloadImage(){},showMessage(){},Option:function(){}};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function retainSourceSelection'),source.indexOf('function renderDetailGallery')),ctx);
ctx.renderSourceGallery();assert.equal(root.children.length,2);
root.children[0].querySelector('[data-right]').listeners.click();
assert.deepEqual(ctx.sourceGallery.map(i=>i.remote_url),['blocked','b','a'],'Visible reorder must use identity, not a filtered index');
const box=root.children[1].querySelector('input');box.checked=true;box.listeners.change();
assert.equal(ctx.gallery[0].remote_url,'a');assert.equal(root.children[1].querySelector('input').checked,true);
box.checked=false;box.listeners.change();assert.equal(ctx.gallery.length,0);
const state=(...args)=>JSON.parse(JSON.stringify(ctx.sourceImageState(...args)));
assert.equal(state({usable_candidate:true},false,false,true).selection,'未选 · 先绑定款式');
assert.equal(state({usable_candidate:false},true,true,true).selection,'人工保留 · 需复核');
assert.equal(state({error:'download failed'},false,false,false).quality,'初筛执行失败');
assert.equal(state(null,false,false,false).quality,'尚未初筛');
ctx.gallery=[{...a,review_status:'HUMAN_SELECTED'}];ctx.currentDraft.evidence.image_review.source_quality_review={items:[{source:'a',usable_candidate:false,reason:'text'}]};
ctx.renderSourceGallery();assert.equal(ctx.gallery.length,1,'A manual selection must not silently disappear');
assert(root.children.some(card=>card.children.some(node=>node.textContent==='人工保留 · 需复核')));
ctx.currentSkuId='A';ctx.skuDetails=[{sku_id:'A'},{sku_id:'B'}];ctx.sourceGallery=[{...a,sku_id:'A'},b];
ctx.skuListingGalleries=[{sku_id:'A',gallery:[{...a,review_status:'AUTO_SCREENED'}]},{sku_id:'B',gallery:[{...b,review_status:'HUMAN_SELECTED'}]}];
ctx.currentDraft.evidence.image_review.source_screening={skus:[{sku_id:'A',items:[{source:'a',usable:true}]},{sku_id:'B',items:[{source:'a',usable:false}]}]};
const savedGroups=JSON.stringify(ctx.skuListingGalleries);
ctx.renderSourceGallery();
assert.equal(JSON.stringify(ctx.skuListingGalleries),savedGroups,'Reading an editor must not silently delete a saved SKU image');
assert.equal(ctx.sourceQualityRows('A').find(r=>r.source==='a').usable_candidate,true);
assert.equal(ctx.sourceQualityRows('B').find(r=>r.source==='a').usable_candidate,false,'Another SKU verdict must not replace this SKU quality');
assert.match(summary.textContent,/1 张质量候选/);
const html=fs.readFileSync('web/product_editor.html','utf8');
assert.equal((html.match(/id="currentSku"/g)||[]).length,1);assert(html.indexOf('id="currentSku"')<html.indexOf('id="basic"'));
assert(/await queryCategory\(true\);\s+if \(location\.hash/.test(source),'Deep link must wait for the category layout to finish');
console.log('Image quality/selection separation, manual retention, identity-based reorder, live checkbox state and one top SKU selector passed.');
