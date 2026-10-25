const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('web/editor.js','utf8');
function extract(name,next){return source.slice(source.indexOf(`function ${name}(`),source.indexOf(`function ${next}(`));}
const controls={};
const context={byId:id=>controls[id],text:v=>v==null?'':String(v),renderSitePricing:()=>{},
  skuDetails:[{sku_id:'s',autopricing_by_site:{MLM:{budget:{forwarder_cny:7},input_package:{weight_g:200,length_cm:11,width_cm:14,height_cm:3}}}}],currentSkuId:'s',currentDraft:{pricing_plan:{}}};
vm.createContext(context);vm.runInContext(extract('updatePricing','renderPricingIntelligence'),context);
for(const [id,value] of Object.entries({purchaseCost:'8',domesticShipping:'6',exchangeRate:'7',packagingCost:'1',otherCost:'2',targetNet:'10',weight:'200',length:'11',width:'14',height:'3',estimatedRemainder:''}))controls[id]={value};
context.updatePricing();assert.equal(controls.estimatedRemainder.value,'4.00');
controls.otherCost.value='';context.updatePricing();assert.match(controls.estimatedRemainder.value,/资料未齐/);
controls.otherCost.value='2';controls.weight.value='800';context.updatePricing();assert.match(controls.estimatedRemainder.value,/资料未齐/);
vm.runInContext(extract('retainSourceSelection','renderSourceGallery'),context);
const q=[{source:'bad',usable_candidate:false},{source:'good',usable_candidate:true}];
assert.equal(context.retainSourceSelection({remote_url:'bad'},q,false),false);
assert.equal(context.retainSourceSelection({remote_url:'good'},q,false),true);
assert.equal(context.retainSourceSelection({remote_url:'bad',review_status:'HUMAN_SELECTED'},q,false),true);
assert.equal(context.retainSourceSelection({remote_url:'bad'},q,true),true);
console.log('UI behavior verified: unknown cost, forwarder, stale package, rejected defaults, manual retention.');
