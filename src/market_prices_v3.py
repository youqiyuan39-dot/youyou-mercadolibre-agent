"""Read public marketplace HTML, without credentials, captcha solving, or hidden API calls."""
from __future__ import annotations
import re, urllib.request
from html.parser import HTMLParser
from urllib.parse import quote, urlparse, parse_qs
from .pricing_tools_v3 import money, number, stamp

MARKETS={"MLM":("mercadolibre.com.mx","MXN"),"MLB":("mercadolivre.com.br","BRL"),"MLC":("mercadolibre.cl","CLP"),"MCO":("mercadolibre.com.co","COP"),"MLA":("mercadolibre.com.ar","ARS")}
VOID={"area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"}
class Node:
    def __init__(self,tag="",attrs=()):
        self.tag=tag;self.attrs=dict(attrs);self.parts=[]
    def has(self,cls): return cls in self.attrs.get("class","").split()
    def find(self,cls):
        result=[self] if self.has(cls) else []
        for n in self.parts:
            if isinstance(n,Node): result.extend(n.find(cls))
        return result
    def text(self): return " ".join(n.text() if isinstance(n,Node) else n for n in self.parts)
class Tree(HTMLParser):
    def __init__(self): super().__init__(convert_charrefs=True);self.root=Node();self.stack=[self.root]
    def handle_starttag(self,tag,attrs):
        n=Node(tag,attrs);self.stack[-1].parts.append(n)
        if tag not in VOID:self.stack.append(n)
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in VOID:self.handle_endtag(tag)
    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:del self.stack[i:];break
    def handle_data(self,text):
        if not any(n.tag in {"script","style"} for n in self.stack): self.stack[-1].parts.append(text)
def parse_search(html,site,url):
    host,cur=MARKETS[site]
    tree=Tree();tree.feed(html);cards=tree.root.find("poly-card");rows=[];seen=set()
    for card in cards:
        title=card.find("poly-component__title");current=card.find("poly-price__current")
        if not title or not current:continue
        link=title[0].attrs.get("href","");u=urlparse(link)
        if u.scheme!="https" or not (u.hostname==host or (u.hostname or "").endswith("."+host)):continue
        match=re.search(site+r"-?(\d{6,15})",u.path) if "/p/" not in u.path and "/up/" not in u.path else None
        if not match:
            match=re.fullmatch(site+r"(\d{6,15})",parse_qs(u.fragment).get("wid",[""])[0])
        if not match:continue
        item_id=site+match.group(1)
        if item_id in seen:continue
        fraction=current[0].find("andes-money-amount__fraction");cents=current[0].find("andes-money-amount__cents")
        if not fraction:continue
        major=re.sub(r"[,. \u00a0]","",fraction[0].text().strip())
        minor=cents[0].text().strip() if cents else "00"
        if not re.fullmatch(r"\d+",major) or not re.fullmatch(r"\d{2}",minor):continue
        price=number(major+"."+minor,"公开刊登价")
        if not price:continue
        text=card.text().lower();seen.add(item_id)
        rows.append(dict(id=item_id,title=title[0].text().strip()[:250],price=money(price),currency=cur,
          condition="unknown",status="observed_public_offer",category_id=None,
          free_shipping=True if "envío gratis" in text or "envio grátis" in text else None,
          permalink=link.split("#")[0]+"#wid="+item_id,endpoint=url,spec_match="NOT_VERIFIED",
          shipping_origin="international" if "internacional" in text else "unknown",
          source_type="PUBLIC_SEARCH_HTML",observed_at=stamp(),price_basis="PUBLIC_LISTING_NOT_CHECKOUT"))
    if not rows:
        raise ValueError("公开页面未取得可识别的刊登价格；可能需要验证或页面结构变化，已停止，不绕过限制")
    return rows[:48]
class OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        u=urlparse(newurl)
        if u.scheme!="https" or not any(u.hostname==h or (u.hostname or "").endswith("."+h) for h,_ in MARKETS.values()):
            raise ValueError("公开页面跳转到非美客多域名，已停止")
        return super().redirect_request(req,fp,code,msg,headers,newurl)
def public_search(site,query):
    host,_=MARKETS[site]
    url="https://listado."+host+"/"+quote(re.sub(r"\s+","-",query.strip()),safe="")
    if site=="MLB":url="https://lista."+host+"/"+quote(re.sub(r"\s+","-",query.strip()),safe="")
    req=urllib.request.Request(url,headers={"User-Agent":"YoyoWorkbench/0.3 public-price-reader","Accept":"text/html"},method="GET")
    with urllib.request.build_opener(OfficialRedirects()).open(req,timeout=25) as response:
        raw=response.read(6_000_001)
        if len(raw)>6_000_000:raise ValueError("公开页面超出读取大小限制")
        final=response.url
        if "/login" in final or "/auth" in final:raise ValueError("公开页面要求登录，本次未取得价格")
    return parse_search(raw.decode("utf-8",errors="replace"),site,url)
