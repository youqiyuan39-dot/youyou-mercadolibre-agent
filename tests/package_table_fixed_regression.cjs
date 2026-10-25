const assert=require('node:assert/strict'),P=require('../collector_extension/packaging.js');
function table(headers,rows,label='包装信息 商品件重尺'){return {textContent:label,querySelectorAll:()=>[headers,...rows].map(r=>({querySelectorAll:()=>r.map(textContent=>({textContent}))}))}}
const headers=['长(cm)','宽(cm)','高(cm)','体积(cm³)','重量(g)'];
let result=P.domTables([table(headers,[['21.50','17.50','21.50','8089.375','400']])],[]);
assert.equal(result.records.length,1);assert.deepEqual(result.records[0].headers,headers);assert.equal(result.records[0].package.weight_g,400);assert.equal(result.records[0].package.width_cm,17.5);
assert.equal(P.singlePackage(result.records).height_cm,21.5);
result=P.domTables([table(['重量(kg)','高(mm)','宽(mm)','长(mm)'],[['0.4','215','175','215']])],[]);
assert.equal(result.records[0].package.weight_g,400);assert.equal(result.records[0].package.length_cm,21.5);
assert.equal(P.rows({pieceWeightScaleInfo:{weight:400,length:21.5,width:17.5,height:21.5}}).length,1);
assert.equal(P.rows({pieceWeightScaleInfo:{'123456789012':{weight:400}}})[0].skuId,'123456789012');
assert.equal(P.pack({weight_g:400,weightUnit:'kg',length_cm:21.5,dimensionUnit:'mm'}).weight_g,400);
assert.equal(P.pack({weight_g:400,weightUnit:'kg',length_cm:21.5,dimensionUnit:'mm'}).length_cm,21.5);
assert.equal(P.singlePackage([{package:{weight_g:400}},{package:{weight_g:401}}]).weight_g,null);
const rawRows=[headers.map(textContent=>({textContent})),['21.5','17.5','21.5','8089.375','400'].map(textContent=>({textContent}))].map(cells=>({querySelectorAll:()=>cells}));
assert.deepEqual(P.attributeRows(rawRows),{});
console.log('Fixed packaging table: anonymous offer, headers, column order, units, flat/map rows, conflicts and attribute isolation passed.');
