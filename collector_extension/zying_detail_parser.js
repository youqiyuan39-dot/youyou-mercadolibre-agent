// Visible DOM only; calculator values are explicitly distinguished from page text.
(function(root) {
  'use strict';
  const VERSION = 'ZYING_DOM_20261022_SLICE9';
  const aliases = {
    dimensions: ['商品尺寸','产品尺寸','尺寸','product dimensions','dimensions','size'],
    weight: ['商品重量(g)','商品重量','产品重量','重量','product weight','weight'],
    volumetric_weight: ['计泡重','泡重','体积重','volumetric weight','dimensional weight'],
    price: ['售价(当地货币)','售价(MXN)','竞品售价','售价','价格','price','selling price'],
    net_proceeds: ['净收益价','净收益','net proceeds','net proceeds price'],
    seller: ['卖家','卖家名称','seller','seller name'],
    fulfillment: ['仓储','库存','发货方式','履约方式','fulfillment','warehouse'],
    category_path: ['类目路径','类目','子类目','category path','category'],
    commission: ['平台佣金比','佣金','佣金率','佣金%','commission','commission rate'],
    listed_at: ['上架日期','上架时间','listing date','listed at'],
    sales: ['总销量','销量','销售量','sales','units sold'],
    brand: ['品牌','brand'],
    title: ['产品标题','商品标题','商品名称','标题','title','product title']
  };
  const clean = s => String(s || '').replace(/\s+/g,' ').replace(/[:：]\s*$/,'').trim();
  const labelKey = s => clean(s).toLowerCase().replace(/（/g,'(').replace(/）/g,')').replace(/\s/g,'');
  const keyFor = label => Object.keys(aliases).find(k => aliases[k].some(a=>labelKey(a)===labelKey(label)));
  const referenceKey = label => ({'采购成本(¥)':'purchase_cost_cny','运送费(¥)':'shipping_cost_cny'})[labelKey(label).replace(/￥/g,'¥')];
  const resultKey = label => ({'净利润':'net_profit','利润率':'margin_pct'})[labelKey(label)];
  const textOf = el => el ? (el.innerText ?? el.textContent ?? '') : '';
  function controlValue(el) {
    if(el.tagName?.toLowerCase()==='select') return textOf(el.selectedOptions?.[0]).trim();
    return String(el.value ?? '').trim(); // live property, never getAttribute('value')
  }
  function readRow(label, row, fallback='') {
    const key = keyFor(label), controls = [...row.querySelectorAll('input,select,textarea')]
      .filter(el=>!['hidden','radio','checkbox','password'].includes(el.type));
    let value = fallback, field_origin = 'text';
    if(controls.length) {
      field_origin = 'zying_calculator_prefill';
      if(key==='dimensions') {
        const inputs=controls.filter(el=>el.tagName?.toLowerCase()==='input');
        const inner=inputs.filter(el=>(el.getAttribute('class')||'').split(/\s+/).includes('el-input__inner'));
        const nums = (inner.length>=3?inner:inputs).map(el=>[el.value,el.getAttribute('value'),el.getAttribute('aria-valuenow'),el.getAttribute('data-value')].map(x=>String(x??'').trim()).find(x=>x!=='')||'').filter(n=>/^\d+(\.\d+)?$/.test(n) && +n>0).slice(0,3);
        value = nums.length===3 && nums.every(n=>/^\d+(\.\d+)?$/.test(n) && +n>0) ? nums.join(' × ')+' cm' : '';
      } else {
        value = controlValue(controls[0]);
        if(key==='weight' && /^\d+(\.\d+)?$/.test(value)) value += ' g';
        if(key==='commission' && /^\d+(\.\d+)?$/.test(value)) value += '%';
      }
    }
    return {label, value, field_origin, control_count:controls.length, input_debug:key==='dimensions'?controls.filter(el=>el.tagName?.toLowerCase()==='input').slice(0,12).map(el=>({value:String(el.value??'').slice(0,20),attribute:el.getAttribute('value'),aria:el.getAttribute('aria-valuenow'),data:el.getAttribute('data-value')})):undefined};
  }
  function readDOM(scope, visible=()=>true) {
    const entries = [];
    for(const el of scope.querySelectorAll('dt,label,span,div,p,th,td')) {
      if(!visible(el)) continue;
      const label = textOf(el);
      if(!keyFor(label) && !referenceKey(label) && !resultKey(label)) continue;
      const form = el.closest('.el-form-item') || el.closest('.ant-form-item') || el.closest('.arco-form-item');
      const parent = el.parentElement;
      let row = form || parent;
      // Component wrappers may separate label and live input by several levels.
      if(!form && keyFor(label) && row && !row.querySelector('input,select,textarea')) {
        for(let n=row.parentElement, depth=0;n && n!==scope && depth<3;n=n.parentElement,depth++) {
          if([...n.querySelectorAll('label,span,dt')].filter(x=>keyFor(textOf(x))).length>1) break;
          if(n.querySelector('input,select,textarea')) {row=n;break;}
        }
      }
      if(!row || !visible(row)) continue;
      let fallback = '';
      if(el.nextElementSibling && visible(el.nextElementSibling)) fallback = textOf(el.nextElementSibling);
      if(!fallback && textOf(row).startsWith(label)) fallback = textOf(row).slice(label.length).replace(/^\s*[:：]/,'');
      if(resultKey(label) && textOf(row).startsWith(label)) fallback=textOf(row).slice(label.length).replace(/^\s*[:：]/,'');
      if(keyFor(label)==='dimensions') {
        const foreign = node => [...node.querySelectorAll('label,span,dt,div,p')].some(x=>keyFor(textOf(x)) && keyFor(textOf(x))!=='dimensions' && keyFor(textOf(x))!=='volumetric_weight');
        if(foreign(row)) row=el.parentElement;
        if(!row.querySelector('input')) {
          for(let n=row.parentElement,depth=0;n && n!==scope && depth<4;n=n.parentElement,depth++) {
            if(foreign(n)) break;
            row=n;
            if(n.querySelector('input')) break;
          }
        }
        if(!row.querySelector('input') && row.nextElementSibling && !foreign(row.nextElementSibling)) row=row.nextElementSibling;
      }
      const item = readRow(label,row,fallback);
      if(resultKey(label)) item.field_origin='zying_calculator_result';
      entries.push(item);
      const volume = textOf(row).match(/计泡重\s*[:：]?\s*(\d+(?:\.\d+)?)\s*克/);
      if(volume) entries.push({label:'计泡重',value:volume[1]+' g',field_origin:form?'zying_calculator_prefill':'text'});
      const cny = textOf(row).match(/[=＝]\s*[¥￥]\s*(\d+(?:\.\d+)?)/);
      if(keyFor(label)==='price' && cny) entries.push({label:'converted_price_cny',value:cny[1],field_origin:'zying_calculator_prefill'});
    }
    // Inline adjacent spans need no standalone label node. Read calculator regions only.
    const calculators=[...scope.querySelectorAll('.calculator,.el-form,.profit-calculator,.el-dialog')];
    for(const calculator of calculators.length?calculators:[scope]) {
      if(!visible(calculator)) continue;
      const volume=textOf(calculator).match(/计泡重\s*[:：]?\s*([\d.]+)\s*(克|g)/i);
      if(volume) entries.push({label:'计泡重',value:volume[1]+' g',field_origin:'zying_calculator_prefill'});
    }
    for(const line of textOf(scope).split('\n')) {
      const m=line.match(/^\s*([^:：]{1,40})[:：]\s*(.{1,600})$/);
      if(m && !keyFor(m[2])) entries.push({label:m[1],value:m[2],field_origin:resultKey(m[1])?'zying_calculator_result':'text'});
    }
    const headings=[...scope.querySelectorAll('h1')].filter(visible);
    if(headings.length===1 && !entries.some(row=>keyFor(row.label)==='title' && clean(row.value)))
      entries.push({label:'title',value:textOf(headings[0]),field_origin:'text'});
    return entries;
  }
  function imageURL(value) {
    try {
      const u=new URL(String(value || ''), 'https://meli.zying.net/');
      if(!value || u.protocol!=='https:' || u.username || u.password || /(?:placeholder|loading|spacer|transparent|favicon|\/icons?\/)/i.test(u.pathname)) return '';
      u.search='';u.hash='';
      return u.href.length<=1000?u.href:'';
    } catch { return ''; }
  }
  function lazyImage(el, styleOf=el=>globalThis.getComputedStyle?.(el)) {
    const srcset=(el.getAttribute('srcset') || '').split(',').map(s=>s.trim().split(/\s+/)).sort((a,b)=>parseFloat(b[1] || 0)-parseFloat(a[1] || 0));
    const urls=[el.currentSrc,el.getAttribute('src'),el.getAttribute('data-src'),el.getAttribute('data-original'),el.getAttribute('data-lazy'),...srcset.map(r=>r[0])];
    for(const node of [el,el.parentElement].filter(Boolean)) {
      const css=styleOf(node)?.backgroundImage || node.style?.backgroundImage || '';
      urls.push(css.match(/url\(["']?(.*?)["']?\)/)?.[1]);
    }
    return urls.map(imageURL).find(Boolean) || '';
  }
  function mainImage(scope, doc, visible=()=>true) {
    const title = [...scope.querySelectorAll('label,span,div,p,dt')].find(el=>keyFor(textOf(el))==='title');
    const candidates = [...scope.querySelectorAll('img')].filter(visible).filter(el=> {
      const r=el.getBoundingClientRect();
      const area=Math.max(el.naturalWidth*el.naturalHeight, r.width*r.height);
      return area>=4096 && (!title || (r.top < title.getBoundingClientRect().top+350 && r.left <= title.getBoundingClientRect().left));
    });
    candidates.sort((a,b)=> {
      const area=el=> (el.naturalWidth*el.naturalHeight) || (el.getBoundingClientRect().width*el.getBoundingClientRect().height);
      return area(b)-area(a);
    });
    return candidates.map(el=>lazyImage(el)).find(Boolean) || imageURL(doc.querySelector('meta[property="og:image"]')?.content);
  }
  function parse({skuId, visibleIds = [], entries = [], sourceUrl, capturedAt, mainImage = ''}) {
    const id = clean(skuId).toUpperCase();
    if(!/^MLM\d{6,20}$/.test(id)) throw Error('仅支持墨西哥 MLM SKU ID');
    const ids = [...new Set(visibleIds.map(x=>clean(x).toUpperCase()))];
    if(ids.length !== 1 || ids[0] !== id) throw Error('详情区域须只出现一个且与输入一致的 SKU ID；请打开单商品详情');
    const values = {}, reference = {}, field_origin = {}, price_observations = [];
    const profit = {inputs:{},results:{net_profit:null,margin_pct:null},captured_at:capturedAt};
    for(const row of entries) {
      const key = keyFor(row.label), ref = referenceKey(row.label) || (row.label==='converted_price_cny'?'converted_price_cny':null);
      let value = clean(row.value);
      const origin=row.field_origin==='zying_calculator_prefill'?'zying_calculator_prefill':'text';
      const result=resultKey(row.label);
      if(result) {
        profit.results[result]={raw:value.slice(0,200),field_origin:'zying_calculator_result',captured_at:capturedAt};
        continue;
      }
      const inputKey=ref || (key==='commission' && origin==='zying_calculator_prefill'?'commission_pct':key==='price' && origin==='zying_calculator_prefill'?'selling_price_mxn':null);
      if(inputKey) profit.inputs[inputKey]={raw:value.slice(0,80),currency:inputKey.endsWith('_cny')?'CNY':inputKey==='selling_price_mxn'?'MXN':null,unit:inputKey==='commission_pct'?'%':null,field_origin:'zying_calculator_prefill',captured_at:capturedAt};
      if(key==='volumetric_weight')value=value.replace(/([\d.]+)\s*克/, '$1 g');
      if(!value || value==='-') continue;
      if(ref) { reference[ref]={value:value.slice(0,80),field_origin:origin}; continue; }
      if(!key) continue;
      if(['dimensions','weight','volumetric_weight'].includes(key) && !/[1-9]/.test(value)) continue;
      if(key==='price' && ['售价(当地货币)','售价(mxn)'].includes(labelKey(row.label)) && /^\d+(\.\d+)?$/.test(value)) value='MXN '+value;
      if(key==='price') price_observations.push({value:value.slice(0,80),field_origin:origin,label:clean(row.label).slice(0,40)});
      (values[key] ||= []).push({value:value.slice(0,300),origin,top:key==='price' && labelKey(row.label)==='售价(当地货币)' && origin==='text'});
    }
    const fields = Object.fromEntries(Object.keys(aliases).map(k => {
      let rows=values[k] || [];
      const preferred=rows.filter(r=>r.top || (k!=='price' && r.origin==='text'));
      if(preferred.length) rows=preferred;
      else if(k==='price' && rows.some(r=>r.origin==='text')) rows=rows.filter(r=>r.origin==='text');
      const unique=[...new Set(rows.map(r=>r.value))];
      field_origin[k]=unique.length===1?rows[0].origin:null;
      return [k,unique.length===1?unique[0]:''];
    }));
    field_origin.main_image=imageURL(mainImage)?'text':null;
    // Conflicting duplicate labels are missing evidence, never first-match wins.
    return {parser_version:VERSION, payload_version:'ZYING_DETAIL_V2', sku_id:id, site_id:'MLM', source_url:String(sourceUrl).slice(0,1000), captured_at:capturedAt,
      main_image:imageURL(mainImage), fields, field_origin, reference, zying_profit_reference:profit,
      price_observations:[...new Map(price_observations.map(r=>[JSON.stringify(r),r])).values()].slice(0,4)};
  }
  function diagnostics(scope, entries, payload, visible=()=>true) {
    const fields={};
    for(const key of Object.keys(aliases)) {
      const rows=entries.filter(r=>keyFor(r.label)===key);
      const hit=!!payload.fields[key];
      const reason=hit?'命中':key==='net_proceeds'?'待补：净收益未试算':key==='category_path' && rows.length && !rows.some(r=>clean(r.value) && r.value!=='-')?'类目未选择':!rows.length?'未找到标签':rows.some(r=>r.field_origin==='zying_calculator_prefill' && !clean(r.value))?'计算器未预填：请先在利润计算器确认尺寸/重量（input 值为空）':rows.some(r=>clean(r.value) && r.value!=='-')?'值冲突或格式未识别':rows.every(r=>!r.control_count)?'找到标签但未找到输入框/文本值':'找到标签但无有效值';
      fields[key]={hit,reason};
    }
    fields.main_image={hit:!!payload.main_image,reason:payload.main_image?'命中':'图片未加载/无 src'};
    const trial=Object.values(payload.zying_profit_reference.results).some(r=>r && /\d/.test(clean(r.raw)));
    fields.profit={hit:trial,reason:trial?'已读取结果':'利润未试算：请填写采购成本并点击利润试算'};
    // Whitelisted labels and associated values only; never serialize an ancestor/body.
    const seen=new Set(),snippets=[];
    const ordered=[...entries].sort((a,b)=>Number(!!fields[keyFor(a.label)]?.hit)-Number(!!fields[keyFor(b.label)]?.hit) || Number(keyFor(b.label)==='dimensions')-Number(keyFor(a.label)==='dimensions'));
    for(const r of ordered) {
      if(!keyFor(r.label)&&!referenceKey(r.label)&&!resultKey(r.label))continue;
      const s={kind:'label_value',text:(clean(r.label)+(r.input_debug?' inputs='+r.control_count+' '+JSON.stringify(r.input_debug):r.field_origin==='zying_calculator_prefill'?' input/select = '+clean(r.value):'')).slice(0,200)};
      const id=s.kind+'|'+s.text;if(!seen.has(id)){seen.add(id);snippets.push(s);}
      if(snippets.length>=39)break;
    }
    if(payload.main_image) snippets.push({kind:'img_src',text:imageURL(payload.main_image).slice(0,200)});
    return {fields,snippets};
  }
  async function waitForSnapshot(read, delay=ms=>new Promise(resolve=>setTimeout(resolve,ms)), attempts=10) {
    let value;
    for(let n=1;n<=attempts;n++) {
      try {value=read();} catch(error) {
        if(n===attempts) throw error;
        await delay(500);continue;
      }
      value.diagnostics.attempts=n;
      if(['dimensions','weight','price','title'].every(k=>value.fields[k]) && value.main_image) return value;
      if(n<attempts) await delay(500);
    }
    return value;
  }
  function domSnapshot(scope,visible=()=>true){
    const entries=readDOM(scope,visible),rows=[];
    for(const entry of entries){
      if(!keyFor(entry.label)&&!referenceKey(entry.label)&&!resultKey(entry.label))continue;
      const label=[...scope.querySelectorAll('label,span,dt,div,p')].find(x=>visible(x)&&clean(textOf(x))===clean(entry.label));
      const row=label?.closest('.el-form-item')||label?.parentElement;
      if(!row||row===scope||row.tagName==='BODY'){rows.push({label:clean(entry.label).slice(0,40),nodes:[{tagName:'SPAN',className:'',text:clean(entry.value).slice(0,100),value:''}]});continue;}
      // Only matched product fields: no whole ancestor HTML, attributes or arbitrary controls.
      const nodes=[label,...row.querySelectorAll('input,select,span')].filter(Boolean).filter(x=>visible(x)&&!['password','hidden'].includes(x.type));
      rows.push({label:clean(entry.label).slice(0,40),nodes:nodes.slice(0,10).map(x=>({tagName:x.tagName,className:clean(x.getAttribute('class')).replace(/[^\w\s-]/g,'').slice(0,120),text:!['INPUT','SELECT'].includes(x.tagName)?clean(textOf(x)).slice(0,100):'',value:['INPUT','SELECT'].includes(x.tagName)?String(x.value??'').slice(0,100):''}))});
    }
    const seen=new Set();return rows.filter(r=>{const k=JSON.stringify(r);if(seen.has(k))return false;seen.add(k);return true;}).slice(0,24);
  }
  const api = {domSnapshot,VERSION, diagnostics, waitForSnapshot, parse, keyFor, aliases, readRow, readDOM, mainImage, lazyImage};
  if(typeof module === 'object' && module.exports) module.exports = api;
  else root.YouyouZyingDetail = root.YouyouZyingDetail || api;
})(globalThis);
