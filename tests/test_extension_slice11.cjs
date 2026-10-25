const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),vm=require('vm');
const E=path.resolve(__dirname,'../collector_extension'),manifest=JSON.parse(fs.readFileSync(path.join(E,'manifest.json')));
function environment(){
 const elements=[],listeners=[];
 class Element{
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.style={};this.attrs={};this.value='';elements.push(this);}
 append(...xs){this.children.push(...xs)}appendChild(x){this.append(x);return x}setAttribute(k,v){this.attrs[k]=v}getAttribute(k){return this.attrs[k]??null}addEventListener(...args){listeners.push(args)}
 querySelectorAll(){return []}querySelector(){return new Element('div')}getClientRects(){return [1]}contains(x){return this.children.includes(x)}
 }
 const body=new Element('body'),documentElement=new Element('html');
 const document={body,documentElement,createElement:t=>new Element(t),getElementById:id=>elements.find(e=>e.id===id),querySelectorAll:()=>[],querySelector:()=>null,addEventListener:(...a)=>listeners.push(a)};
 const c=vm.createContext({document,URL,TextEncoder,setTimeout,clearTimeout,console,location:{href:'https://meli.zying.net/#/compete/shop-list',origin:'https://meli.zying.net',pathname:'/'},chrome:{runtime:{getURL:x=>'chrome-extension://test/'+x,getManifest:()=>manifest,sendMessage:()=>{throw Error('No messaging authorized')}},storage:{local:{get:async()=>({}),set:async()=>{}}}},getComputedStyle:()=>({visibility:'visible'})});c.window=c;return {c,elements,listeners};
}
const run=(c,f)=>vm.runInContext(fs.readFileSync(path.join(E,f),'utf8'),c,{filename:f});
for(const g of manifest.content_scripts){const {c,elements,listeners}=environment();for(const f of g.js)run(c,f);const before=listeners.length,count=elements.length;for(const f of g.js)run(c,f);assert.equal(listeners.length,before,'duplicate listeners '+g.js);assert.equal(elements.length,count,'duplicate DOM '+g.js);}
// Overlapping ISOLATED groups run in the same frame, not only each group alone.
for(const groups of [manifest.content_scripts.filter(g=>g.matches.includes('https://meli.zying.net/*')),manifest.content_scripts.filter(g=>g.world!=='MAIN'&&g.matches.some(x=>x.includes('1688.com'))) ]){
 const {c,elements,listeners}=environment();for(const g of groups)for(const f of g.js)run(c,f);const n=listeners.length;for(const g of groups)for(const f of g.js)run(c,f);assert.equal(listeners.length,n);assert.ok(elements.filter(x=>x.id==='youyou-capture-panel').length<=1);
}
for(const f of ['zying_detail.js','offer_candidates.js']){const {c,elements}=environment();run(c,f);run(c,f);const notices=elements.filter(x=>x.id==='youyou-component-missing');assert.equal(notices.length,1);assert.equal(notices[0].textContent,'扩展组件未加载，请重载扩展并刷新页面');assert.equal(c.__youyouZyingDetail,undefined);assert.equal(c.__youyouOfferCandidates,undefined);run(c,'capture_panel.js');run(c,f);assert.equal(elements.filter(x=>x.id==='youyou-capture-panel').length,1);}
console.log('slice11: all content groups twice, overlapping worlds, missing-panel hint and recovery PASS');
