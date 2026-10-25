
import copy,json,tempfile,time,unittest
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch
from src import autopricing_v3 as a, pricing_tools_v3 as p, market_prices_v3 as m
from src.mercadolibre_client import MercadoLibreAPIError

def draft():
 return {"payload":{"title":"Five-in-One 10-Inch Telescopic Adjustable Wrench","sites_to_sell":[{"site_id":"MLM","net_proceeds":14,"price":None}],"attributes":[]},"pricing_plan":{"mode":"net_proceeds","domestic_shipping_cny":6},"evidence":{"current_pricing_sku_id":"one","sku_details":[{"sku_id":"one","variant":"box","purchase_cost_cny":50,"target_net_proceeds_usd":14,"package":{"weight_g":650,"length_cm":26,"width_cm":8,"height_cm":4},"site_pricing":[{"site_id":"MLM","net_proceeds":14,"price":None,"logistic_type":"remote"},{"site_id":"MLB","net_proceeds":20,"price":None,"logistic_type":"remote"}]},{"sku_id":"two","purchase_cost_cny":48,"target_net_proceeds_usd":13.5,"package":{"weight_g":650,"length_cm":26,"width_cm":8,"height_cm":4},"site_pricing":[{"site_id":"MLM","net_proceeds":13.5,"price":None,"logistic_type":"remote"}]}]}}
def comparison():
 return {"state":"READY","price_bands":[{"min":"470.00","currency":"MXN"}],"items":[{"title":"Llave Telescopica 5 En 1 De 10 Pulgadas","price":"470.00","currency":"MXN","condition":"unknown","free_shipping":True,"shipping_origin":"international"}]}
def card(price="490",cents="57",link="https://www.mercadolibre.com.mx/w/p/MLM12345678#wid=MLM1234567890"):
 return f'<div class="poly-card"><h3><a class="poly-component__title" href="{link}">Llave telescopica 5 en 1 de 10 pulgadas</a></h3><span class="andes-money-amount__fraction">900</span><div class="poly-price__current"><span class="andes-money-amount__fraction">{price}</span><span class="andes-money-amount__cents">{cents}</span></div><span class="poly-price__installments">3 pagos <span class="andes-money-amount__fraction">163</span></span><span>Envío gratis Internacional China</span></div>'

class PublicParsing(unittest.TestCase):
 def test_current_price_not_installments_or_old(self):
  r=m.parse_search(card(),"MLM","url");self.assertEqual(r[0]["price"],"490.57");self.assertEqual(r[0]["id"],"MLM1234567890")
 def test_thousands_and_no_cents(self):
  html=card("1,087").replace('<span class="andes-money-amount__cents">57</span>',"");self.assertEqual(m.parse_search(html,"MLM","url")[0]["price"],"1087.00")
 def test_not_catalog_id(self):
  with self.assertRaises(ValueError):m.parse_search(card(link="https://www.mercadolibre.com.mx/w/p/MLM12345678"),"MLM","url")
 def test_external_domain(self):
  with self.assertRaises(ValueError):m.parse_search(card(link="https://evil.example/MLM1234567890"),"MLM","url")
 def test_other_site_excluded(self):
  with self.assertRaises(ValueError):m.parse_search(card(link="https://www.mercadolibre.com.mx/w/p/MLM12345678#wid=MLB1234567890"),"MLM","url")
 def test_challenge_not_zero_results(self):
  with self.assertRaises(ValueError):m.parse_search("<h1>Verify you are human</h1>","MLM","url")
 def test_duplicate(self):
  self.assertEqual(len(m.parse_search(card()+card(),"MLM","url")),1)
 def test_zero_or_bad_cents(self):
  for html in (card("0","00"),card(cents="5"),card("-20","00")):
   with self.subTest(html=html),self.assertRaises(ValueError):m.parse_search(html,"MLM","url")
 def test_source(self):
  r=m.parse_search(card(),"MLM","url")[0];self.assertEqual(r["source_type"],"PUBLIC_SEARCH_HTML");self.assertEqual(r["spec_match"],"NOT_VERIFIED")
 def test_api_forbidden_fallback(self):
  client=unittest.mock.Mock();client.search_competitors.side_effect=MercadoLibreAPIError("forbidden",http_status=403)
  with tempfile.TemporaryDirectory() as temp,patch.object(p.operations,"store",return_value={"id":"s"}),patch.object(p.operations,"client_for",return_value=(client,{})),patch.object(m,"public_search",return_value=m.parse_search(card(),"MLM","url")):
   r=p.compare(Path(temp),dict(store_id="s",query="wrench"));self.assertEqual(r["state"],"READY");self.assertEqual(r["attempts"][0]["http_status"],403);self.assertEqual(r["items"][0]["price"],"490.57")
 def test_both_sources_fail(self):
  client=unittest.mock.Mock();client.search_competitors.side_effect=MercadoLibreAPIError("forbidden",http_status=403)
  with tempfile.TemporaryDirectory() as temp,patch.object(p.operations,"store",return_value={"id":"s"}),patch.object(p.operations,"client_for",return_value=(client,{})),patch.object(m,"public_search",side_effect=ValueError("verification")):
   r=p.compare(Path(temp),dict(store_id="s",query="wrench"));self.assertEqual(r["state"],"UNAVAILABLE");self.assertFalse(r["items"])

class CostSolver(unittest.TestCase):
 def solve(self,**kw):
  policy={**a.DEFAULT_POLICY,**kw};return a.solve(50,6,.6,0,"6.6921",policy)
 def test_target_and_stress(self):
  r=self.solve();self.assertEqual(r["net_proceeds_usd"],"14.52");self.assertGreaterEqual(Decimal(r["margin_pct"]),30);self.assertLess(Decimal(r["stress_margin_pct"]),Decimal(r["margin_pct"]))
 def test_budget_increase_raises_price(self):
  self.assertGreater(Decimal(self.solve(ads_net_pct="10")["net_proceeds_usd"]),Decimal(self.solve()["net_proceeds_usd"]))
 def test_invalid_percent(self):
  for policy in [dict(target_margin_pct="95"),dict(minimum_margin_pct="40"),dict(tax_reserve_net_pct="-1"),dict(ads_net_pct="NaN")]:
   with self.subTest(policy=policy),self.assertRaises(ValueError):self.solve(**policy)
 def test_zero_rate_or_cost(self):
  for args in [(50,6,.6,0,0),(0,0,0,0,7)]:
   with self.assertRaises(ValueError):a.solve(*args,a.DEFAULT_POLICY)
 def test_missing_cost(self):
  with self.assertRaises(ValueError):a.solve(None,6,.6,0,7,a.DEFAULT_POLICY)
 def test_ordinary_forwarder(self):
  d=draft();self.assertEqual(a.forwarder(d["evidence"]["sku_details"][0],d,a.DEFAULT_POLICY),Decimal(".6"))
 def test_overweight_without_oversize(self):
  d=draft();s=d["evidence"]["sku_details"][0];s["package"]["weight_g"]=6000;self.assertEqual(a.forwarder(s,d,a.DEFAULT_POLICY),Decimal("1.6"))
 def test_volumetric_rule(self):
  d=draft();s=d["evidence"]["sku_details"][0];s["package"].update(length_cm=60,width_cm=40,height_cm=40)
  self.assertEqual(a.forwarder(s,d,a.DEFAULT_POLICY),Decimal("3.6"))
 def test_unknown_dimensions(self):
  d=draft();s=d["evidence"]["sku_details"][0];s["package"]["length_cm"]=None
  with self.assertRaises(ValueError):a.forwarder(s,d,a.DEFAULT_POLICY)
 def test_peers_exclude_normal_and_different_sizes(self):
  c=comparison();c["items"] += [{**c["items"][0],"title":"Llave normal 10 pulgadas","price":"76"},{**c["items"][0],"title":"Llave telescopica 5 en 1 12 pulgadas","price":"80"}]
  r=a.market_reference(draft(),c);self.assertEqual(len(r["items"]),1);self.assertEqual(r["bands"][0]["min"],"470.00")

class LocalPricing(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.path=self.root/"drafts/demo-draft.json";self.path.parent.mkdir();self.path.write_text(json.dumps(draft()),encoding="utf-8")
  # This fixture explicitly authorizes net-return scenario writes; production
  # buyer-price authorization must not be inherited by a different denominator.
  a.save(self.root/'config/autopricing_policy.json',{**a.DEFAULT_POLICY,'margin_policy_status':'CONFIRMED','confirmed_margin_basis':'NET_PROCEEDS'})
  self.store=patch.object(p.operations,"store",return_value={"id":"s"});self.store.start()
  self.comp=patch.object(p,"compare",return_value=comparison());self.comp_mock=self.comp.start()
  self.fx=patch.object(p,"rate_quote",return_value={"cny_per_currency":"6.6921","source":{"date":"2026-10-09"}});self.fx_mock=self.fx.start()
 def tearDown(self):
  self.fx.stop();self.comp.stop();self.store.stop();self.temp.cleanup()
 def prepare(self,**kw):return a.prepare(self.root,dict(name="demo-draft.json",store_id="s",query="wrench",**kw))
 def test_preview_never_changes_draft(self):
  original=self.path.read_bytes();r=self.prepare();self.assertEqual(r["state"],"READY_ESTIMATED");self.assertEqual(original,self.path.read_bytes())
 def test_apply_and_undo(self):
  original=json.loads(self.path.read_text(encoding="utf-8"));r=self.prepare();applied=a.apply(self.root,r["id"],True);d=json.loads(self.path.read_text(encoding="utf-8"))
  self.assertEqual(applied["state"],"APPLIED_LOCAL");self.assertEqual(d["evidence"]["sku_details"][0]["site_pricing"][0]["net_proceeds"],14.52)
  self.assertEqual(d["evidence"]["sku_details"][0]["site_pricing"][1]["net_proceeds"],20)
  self.assertEqual(d["payload"]["sites_to_sell"][0]["net_proceeds"],14.52)
  self.assertEqual(d["pricing_plan"]["profit_guidance"]["status"],"BUDGET_ESTIMATE")
  self.assertEqual(a.undo(self.root,r["id"],True)["state"],"UNDONE");self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),original)
 def test_confirmation_required(self):
  r=self.prepare()
  for confirmed in (False,None,"true"):
   with self.assertRaises(ValueError):a.apply(self.root,r["id"],confirmed)
 def test_stale_product(self):
  r=self.prepare();self.path.write_text(json.dumps({**draft(),"edited":True}),encoding="utf-8")
  with self.assertRaises(ValueError):a.apply(self.root,r["id"],True)
 def test_stale_policy(self):
  r=self.prepare();a.save(self.root/"config/autopricing_policy.json",{**a.DEFAULT_POLICY,"ads_net_pct":"8"})
  with self.assertRaises(ValueError):a.apply(self.root,r["id"],True)
 def test_expired(self):
  r=self.prepare()
  with patch.object(a.time,"time",return_value=time.time()+901),self.assertRaises(ValueError):a.apply(self.root,r["id"],True)
 def test_missing_fx_blocks(self):
  self.fx_mock.return_value={"error":"missing"};r=self.prepare();self.assertEqual(r["state"],"BLOCKED")
  with self.assertRaises(ValueError):a.apply(self.root,r["id"],True)
 def test_missing_market_blocks(self):
  self.comp_mock.return_value={"price_bands":[],"items":[]};self.assertEqual(self.prepare()["state"],"BLOCKED")
 def test_one_bad_sku_blocks_whole_batch(self):
  d=draft();d["evidence"]["sku_details"][1]["purchase_cost_cny"]=None;self.path.write_text(json.dumps(d),encoding="utf-8");r=self.prepare();self.assertEqual(r["state"],"BLOCKED")
  with self.assertRaises(ValueError):a.apply(self.root,r["id"],True)
 def test_scoped_sku(self):
  r=self.prepare(sku_id="two");a.apply(self.root,r["id"],True);d=json.loads(self.path.read_text(encoding="utf-8"))
  self.assertEqual(d["evidence"]["sku_details"][0]["target_net_proceeds_usd"],14)
  self.assertEqual(d["evidence"]["sku_details"][1]["target_net_proceeds_usd"],14.01)
 def test_duplicate_apply_idempotent(self):
  r=self.prepare();a.apply(self.root,r["id"],True);self.assertEqual(a.apply(self.root,r["id"],True)["state"],"APPLIED_LOCAL")
 def test_undo_does_not_clobber_later_edit(self):
  r=self.prepare();a.apply(self.root,r["id"],True);self.path.write_text(json.dumps(draft()),encoding="utf-8")
  with self.assertRaises(ValueError):a.undo(self.root,r["id"],True)
 def test_budget_override(self):
  r=self.prepare(budget={"ads_net_pct":"10"});self.assertEqual(r["state"],"READY_ESTIMATED");a.apply(self.root,r["id"],True)
 def test_traversal(self):
  with self.assertRaises(ValueError):a.draft_path(self.root,"../demo-draft.json")

class PricingHTTP(unittest.TestCase):
 setUp=LocalPricing.setUp
 tearDown=LocalPricing.tearDown
 def test_page_write_undo_and_port_ownership(self):
  import threading,urllib.request
  from src import workbench_v3 as w
  server=w.WorkbenchServer(("127.0.0.1",0),w.make_handler(self.root))
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  def post(route,value):
   req=urllib.request.Request("http://127.0.0.1:"+str(server.server_port)+route,json.dumps(value).encode(),{"Content-Type":"application/json"})
   return json.load(urllib.request.urlopen(req,timeout=5))
  original=json.loads(self.path.read_text(encoding="utf-8"))
  try:
   with self.assertRaises(OSError):w.WorkbenchServer(server.server_address,w.make_handler(self.root))
   r=post("/api/v3/pricing/plan",dict(name="demo-draft.json",store_id="s",query="wrench"))
   self.assertEqual(r["state"],"READY_ESTIMATED")
   self.assertEqual(post("/api/v3/pricing/apply",dict(plan_id=r["id"],confirmed=True))["state"],"APPLIED_LOCAL")
   self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["payload"]["sites_to_sell"][0]["net_proceeds"],14.52)
   self.assertEqual(post("/api/v3/pricing/undo",dict(plan_id=r["id"],confirmed=True))["state"],"UNDONE")
   self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),original)
  finally:server.shutdown();server.server_close();thread.join()
 def test_stress_doubles_large_budget_without_hidden_cap(self):
  r=a.solve(50,6,.6,0,"6.6921",{**a.DEFAULT_POLICY,"ads_net_pct":"30"})
  net=Decimal(r["net_proceeds_usd"]);base=Decimal(r["base_cost_cny"])
  expected=(net*(1-Decimal(".6")-Decimal(".1"))-base*Decimal("1.06")/Decimal("6.6921"))/net*100
  self.assertEqual(r["stress_margin_pct"],p.money(expected))

class MarketConversion(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.d=draft();self.d["payload"]["category_id"]="CBT437802";self.s=self.d["evidence"]["sku_details"][0];self.s["site_pricing"][0]["listing_type_id"]="gold_special"
  self.q={"observed_epoch":time.time(),"max_age_seconds":21600,"store_id":"s","site_id":"MLM","category_id":"CBT437802","logistic_type":"remote","listing_type_id":"gold_special","weight_g":650,"commission_pct":"14","international_shipping_usd":"9.20","local_shipping_usd":"0","price_low_usd":"26.99","price_high_usd":"30","usd_mxn":{"rate":"18.2001"}}
  self.path=self.root/"data/v3/pricing/official-fee-reference.json";a.save(self.path,self.q)
 def tearDown(self):self.temp.cleanup()
 def run_quote(self,net="14.52"):return a.market_assessment(self.root,self.d,self.s,{"net_proceeds_usd":net},{"bands":[{"currency":"MXN","min":"468.33","max":"496.95"}]},"MLM","s")
 def test_valid_conversion_and_above_market_flag(self):
  r=self.run_quote();self.assertEqual(r["gross_usd"],"27.59");self.assertEqual(r["gross_mxn_reference"],"502.14");self.assertEqual(r["peer_position"],"ABOVE_OBSERVED_RANGE")
 def test_other_sku_in_market_band(self):self.assertEqual(self.run_quote("14.01")["peer_position"],"WITHIN_OBSERVED_RANGE")
 def test_expired_quote_not_reused(self):
  self.q["observed_epoch"]=time.time()-21601;a.save(self.path,self.q);self.assertEqual(self.run_quote()["state"],"UNAVAILABLE")
 def test_changed_weight_or_category_or_route_rejected(self):
  for field,value in [("weight_g",700),("category_id","CBT999"),("store_id","another"),("listing_type_id","gold_pro")]:
   with self.subTest(field=field):
    a.save(self.path,{**self.q,field:value});self.assertEqual(self.run_quote()["state"],"UNAVAILABLE")
 def test_no_extrapolation_beyond_sample_prices(self):self.assertEqual(self.run_quote("30")["state"],"UNAVAILABLE")
 def test_bad_quote_is_not_a_valid_zero_cost(self):
  a.save(self.path,{"invalid":True});self.assertEqual(self.run_quote()["state"],"UNAVAILABLE")
