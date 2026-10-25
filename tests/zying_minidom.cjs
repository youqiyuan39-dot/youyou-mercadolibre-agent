// Offline fixture adapter. Deliberately implements only the DOM surface read by the parser.
// data-live mutates the property independently of the HTML value attribute.
const voids=new Set(['input','img','meta','br','hr','link']);
class Element {
 constructor(tag,attrs={},parent=null){this.tagName=tag.toUpperCase();this.attrs=attrs;this.parentElement=parent;this.nodes=[];this.value=attrs['data-live']??attrs.value??'';this.type=attrs.type||'text';this.style={backgroundImage:(attrs.style||'').replace(/^background-image:/,'')};this.naturalWidth=0;this.naturalHeight=0;}
 get textContent(){return this.nodes.map(n=>typeof n==='string'?n:n.textContent).join('');}
 set textContent(v){this.nodes=[v];}
 get innerText(){return this.nodes.map(n=>typeof n==='string'?n:n.innerText+(['P','DIV','SECTION'].includes(n.tagName)?'\n':'')).join('');}
 get children(){return this.nodes.filter(n=>typeof n!=='string');}
 get nextElementSibling(){const c=this.parentElement?.children||[];return c[c.indexOf(this)+1]||null;}
 get selectedOptions(){return this.children.filter(c=>c.tagName==='OPTION' && 'selected' in c.attrs);}
 get currentSrc(){return this._currentSrc||'';}
 get src(){return this.attrs.src||'';}
 get content(){return this.attrs.content||'';}
 getAttribute(k){return this.attrs[k]??null;}
 matches(selector){
  if(selector==='[hidden]')return 'hidden' in this.attrs;
  if(selector.startsWith('.'))return (this.attrs.class||'').split(/\s+/).includes(selector.slice(1));
  if(selector.startsWith('#'))return this.attrs.id===selector.slice(1);
  const m=selector.match(/^(\w+)(?:\[([\w-]+)="([^"]*)"\])?$/);
  return !!m && this.tagName===m[1].toUpperCase() && (!m[2]||this.attrs[m[2]]===m[3]);
 }
 closest(selector){for(let n=this;n;n=n.parentElement)if(n.matches(selector))return n;return null;}
 querySelectorAll(selector){const result=[];for(const c of this.children){if(selector.split(',').some(s=>c.matches(s.trim())))result.push(c);result.push(...c.querySelectorAll(selector));}return result;}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 getBoundingClientRect(){return {width:+this.attrs.width||100,height:+this.attrs.height||30,top:this.closest('.calculator')?700:100,left:this.closest('.info')?700:0};}
}
module.exports=function parseHTML(html){
 const root=new Element('document'), stack=[root];
 for(const token of html.match(/<!--[\s\S]*?-->|<[^>]+>|[^<]+/g)||[]){
  if(token.startsWith('<!'))continue;
  if(token.startsWith('</')){stack.pop();continue;}
  if(token.startsWith('<')){
   const tag=token.match(/^<([\w-]+)/)[1],attrs={};
   for(const m of token.slice(tag.length+1,-1).matchAll(/([\w-]+)(?:="([^"]*)")?/g))attrs[m[1]]=m[2]??'';
   const parent=stack.at(-1),el=new Element(tag,attrs,parent);parent.nodes.push(el);if(!voids.has(tag))stack.push(el);
  }else stack.at(-1).nodes.push(token);
 }
 return root;
};
