const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const code=fs.readFileSync('web/editor.js','utf8');
const values={};const context={text:v=>v==null?'':String(v),numberOrBlank:v=>v??'',
 byId:id=>values[id]??(values[id]={value:'',replaceChildren(){},appendChild(){}}),
 skuDetails:[{sku_id:'a',ai_content:{category_id:'OLD',description:'Copy A',attributes:[]}},{sku_id:'b',ai_content:{category_id:'OLD',description:'Copy B',attributes:[]}}],
 collectedSkus:[],currentDraft:{payload:{category_id:'NEW'}},familyCategoryId:'NEW',currentCategory:null,skuContentEdits:{},currentSkuId:'',attributeRow:a=>a,
 updatePricing(){},updatePhysicalMetrics(){}};
vm.createContext(context);vm.runInContext(code.slice(code.indexOf('function applyCurrentSku('),code.indexOf('function syncCurrentSkuSitePricing(')),context);
context.applyCurrentSku('a',true);assert.equal(values.categoryId.value,'NEW');assert.equal(values.description.value,'Copy A');
context.applyCurrentSku('b',true);assert.equal(values.categoryId.value,'NEW');assert.equal(values.description.value,'Copy B');
assert(!code.slice(code.indexOf('const physical = {'),code.indexOf('const requiredIds =')).includes('\n    HEIGHT:'));
console.log('SKU switch keeps family category and per-SKU copy; packaging no longer fills product dimensions.');
