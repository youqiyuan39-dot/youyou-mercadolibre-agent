// Public packaging facts only; never infer a package from product dimensions.
(() => {
  const clean = x => String(x ?? '').replace(/\s+/g, ' ').trim();
  const positive = x => {const m=clean(x).replace(/,/g,'').match(/^([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z千克公斤克厘米毫米米]*)$/);return m && +m[1]>0 ? [+m[1],m[2].toLowerCase()] : null;};
  function quantity(raw, unit, kind) {
    const v=positive(raw); if(!v)return null;
    const units=kind==='weight'?{g:1,'克':1,kg:1000,'千克':1000,'公斤':1000}:{cm:1,'厘米':1,mm:.1,'毫米':.1,m:100,'米':100};
    const chosen=v[1]||clean(unit).toLowerCase()||(kind==='weight'?'g':'cm');
    return units[chosen] ? Math.round(v[0]*units[chosen]*1000000)/1000000 : null;
  }
  function pack(row, units={}) {
    if(!row || typeof row!=='object')return null;
    const result={weight_g:quantity(row.weight_g??row.weight??row.grossWeight, row.weight_g!=null?'g':row.weightUnit||units.weight,'weight'),
      length_cm:quantity(row.length_cm??row.length,row.length_cm!=null?'cm':row.lengthUnit||row.dimensionUnit||units.length,'length'),
      width_cm:quantity(row.width_cm??row.width,row.width_cm!=null?'cm':row.widthUnit||row.dimensionUnit||units.width,'length'),
      height_cm:quantity(row.height_cm??row.height,row.height_cm!=null?'cm':row.heightUnit||row.dimensionUnit||units.height,'length'),
      evidence:'1688_packaging_table',status:'SUPPLIER_PACKAGING_INFO'};
    return Object.entries(result).some(([k,v])=>k.endsWith('_g')||k.endsWith('_cm') ? v!==null:false) ? result:null;
  }
  function rows(info) {
    try {if(typeof info==='string')info=JSON.parse(info);}catch{return [];}
    const data=info?.pieceWeightScaleInfo??info?.skuWeightData??info;
    if(Array.isArray(data))return data;
    if(data&&typeof data==='object'&&pack(data))return [data];
    return data&&typeof data==='object'?Object.entries(data).filter(([,x])=>x&&typeof x==='object').map(([id,x])=>/^\d{6,}$/.test(id)&&!x.skuId&&!x.sku_id?{...x,skuId:id}:x):[];
  }
  function match(row, candidates, count) {
    const id=clean(row.sku_id??row.skuId),spec=clean(row.spec_id??row.specId);
    let found=candidates.filter(p=>(id && clean(p.skuId??p.sku_id)===id)||(spec && clean(p.specId??p.spec_id)===spec));
    if(!found.length && count===1 && candidates.length===1 && !clean(candidates[0].skuId??candidates[0].sku_id) && !clean(candidates[0].specId??candidates[0].spec_id))found=candidates;
    return found.length===1 ? pack(found[0]):null;
  }
  function singlePackage(records) {
    const anonymous=records.filter(r=>!clean(r.sku_id||r.skuId)&&!clean(r.spec_id||r.specId));
    if(!anonymous.length)return null;
    const result={evidence:'1688_packaging_table',status:'SUPPLIER_PACKAGING_INFO'};
    for(const key of ['weight_g','length_cm','width_cm','height_cm']){
      const values=[...new Set(anonymous.map(r=>(r.package||pack(r)||{})[key]).filter(v=>v>0))];result[key]=values.length===1?values[0]:null;
    }
    return result;
  }
  function domTables(tables, skus) {
    const packages=new Map();const unassigned=[];const records=[];
    for(const [tableIndex,table] of [...tables].entries()) {
      let region='';
      for(let node=table,level=0;node&&level<4;node=node.parentElement,level++){
        const text=clean(node.textContent);const heading=clean(node.previousElementSibling?.textContent);
        if(/包装|商品件重尺|包裹|装箱|shipping|package|毛重/i.test(heading+' '+text)&&text.length<18000){region=(heading+' '+text).slice(0,700);break;}
      }
      if(!region)continue;
      const rows=[...table.querySelectorAll('tr')].map(r=>[...r.querySelectorAll('th,td')].map(c=>clean(c.textContent)));
      const hi=rows.findIndex(r=>r.some(h=>/^(?:包装)?长(?:度)?(?:[（(\s]|$)|^(?:length|重量|件重|毛重)/i.test(h)));
      if(hi<0)continue;
      const headers=rows[hi];const ix={length:headers.findIndex(h=>/^长|^包装长|^length/i.test(h)),width:headers.findIndex(h=>/^宽|^包装宽|^width/i.test(h)),height:headers.findIndex(h=>/^高|^包装高|^height/i.test(h)),weight:headers.findIndex(h=>/重量|件重|毛重|^(?:gross\s*)?weight/i.test(h)&&!/净重|net/i.test(h))};
      const units=Object.fromEntries(Object.entries(ix).map(([k,i])=>[k,(headers[i]?.match(/\b(?:kg|mm|cm|g|m)\b|千克|公斤|毫米|厘米|克/gi)||[])[0]]));
      const data=rows.slice(hi+1).filter(r=>r.length>=headers.length);
      for(const [rowIndex,values] of data.entries()) {
        const p=pack(Object.fromEntries(Object.entries(ix).filter(([,i])=>i>=0).map(([k,i])=>[k,values[i]])),units);if(!p)continue;
        const parts=s=>clean(s.variant).split(/\s*\/\s*/).filter(Boolean);
        const matching=skus.filter(s=>values.some(v=>v&&[s.sku_id,s.spec_id,s.variant].some(k=>clean(k)&&v===clean(k))) || parts(s).filter(t=>skus.filter(other=>parts(other).includes(t)).length===1).some(t=>values.includes(t))).filter(s=>clean(s.sku_id));
        const owner=matching.length===1?matching[0]:skus.length===1&&data.length===1?skus[0]:null;
        const record={sku_id:clean(owner?.sku_id),spec_id:clean(owner?.spec_id),variant:clean(owner?.variant),package:p,headers,values,
          table_title:'包装信息 / 商品件重尺',source_method:'VISIBLE_PACKAGING_TABLE',table_index:tableIndex,row_index:rowIndex,
          dimension_type:'PACKAGING',weight_type:'PACKED_WEIGHT',quoted_text:headers.map((h,i)=>h+'='+values[i]).join('; ')};
        p.source_label=record.quoted_text;records.push(record);
        if(owner&&!packages.has(clean(owner.sku_id)))packages.set(clean(owner.sku_id),p);
        else if(!owner)unassigned.push(record);
      }
    }
    return {packages,unassigned,records};
  }
  function bindImages(skus,sets){
    const parts=value=>clean(value).split(/\s*\/\s*/).filter(Boolean);
    return skus.map(sku=>{
      const mine=parts(sku.variant);
      const exclusive=mine.filter(t=>skus.filter(s=>parts(s.variant).includes(t)).length===1);
      const matches=sets.filter(s=>s.sku_id?clean(s.sku_id)===clean(sku.sku_id):clean(s.variant)===clean(sku.variant)||parts(s.variant).some(t=>t===clean(sku.sku_id)||t===clean(sku.spec_id))||exclusive.some(t=>parts(s.variant).includes(t)));
      const images=[...new Set(matches.flatMap(s=>s.images||[]))].slice(0,8);
      return images.length?{sku_id:clean(sku.sku_id),variant:clean(sku.variant),images}:null;
    }).filter(Boolean);
  }
  function attributeRows(rows){
    const result={};
    for(const row of rows){
      const direct=[...row.querySelectorAll(':scope > th, :scope > td')];
      const cells=(direct.length?direct:[...row.querySelectorAll('span')].filter(n=>!n.querySelector('span'))).map(n=>clean(n.textContent)).filter(Boolean);
      if(cells.some(c=>/^(?:包装)?[长宽高](?:度)?\s*[（(]|^体积\s*[（(]/.test(c)) || cells.length&&/^\d+(?:\.\d+)?$/.test(cells[0]))continue;
      // Ordinary 1688 attribute rows contain label/value pairs, sometimes two pairs per row.
      if(cells.length<2||cells.length>8)continue;
      for(let i=0;i+1<cells.length;i+=2){
        const label=cells[i].replace(/[:：]$/,'');const value=cells[i+1];
        if(label.length>40||label===value||/^(?:颜色|款式\d+|重量\(?g\)?|尺寸|SKU)$/i.test(label)&&/重量|尺寸|颜色/.test(value))continue;
        if(!(label in result)||result[label]===value)result[label]=value.slice(0,1000);
      }
      if(Object.keys(result).length>=80)break;
    }
    return result;
  }
  function domVariantLabel(element,skus){
    const parts=s=>clean(s.variant).split(/\s*\/\s*/).filter(Boolean);
    const tokens=[...new Set(skus.flatMap(parts))].filter(t=>skus.filter(s=>parts(s).includes(t)).length===1);
    for(let node=element,depth=0;node&&depth<5;node=node.parentElement,depth++){
      const text=clean(node.getAttribute?.('title')||node.getAttribute?.('aria-label')||node.textContent);
      if(text.length>500)continue;
      const hits=tokens.filter(t=>{const at=text.indexOf(t);return at>=0&&!/[A-Za-z0-9]/.test(text[at-1]||'')&&!/[A-Za-z0-9]/.test(text[at+t.length]||'');});
      if(hits.length===1)return hits[0];
      if(hits.length>1)return '';
    }
    return '';
  }
  globalThis.YouyouPackaging=globalThis.YouyouPackaging || {quantity,pack,rows,match,singlePackage,domTables,bindImages,attributeRows,domVariantLabel};
  if(typeof module!=='undefined')module.exports=globalThis.YouyouPackaging;
})();
