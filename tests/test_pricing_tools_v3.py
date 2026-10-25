
import json, tempfile, unittest, threading
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from src import pricing_tools_v3 as p, workbench_v3 as w
from src.workbench_assistant import DataSource
from src.mercadolibre_client import MercadoLibreAPIError

class Client:
 def __init__(self,rows=None,error=None): self.rows=rows or [];self.error=error;self.gets=[]
 def search_competitors(self,site,query,limit):
  if self.error: raise self.error
  return {"results":self.rows}
 def get(self,path):
  self.gets.append(path)
  if self.error: raise self.error
  return self.rows[0]

def item(**kw):
 return dict(id="MLM1234567890",site_id="MLM",title="test",price=100,currency_id="MXN",condition="new",status="active",shipping={"free_shipping":True},**kw) if not kw else {**item(),**kw}

class Calculator(unittest.TestCase):
 def data(self,**kw):
  return {**dict(currency="USD",mode="net_proceeds",income="20",cny_per_currency="7",costs_cny={k:("70" if k=="purchase" else "0") for k in p.COSTS}),**kw}
 def test_net_proceeds(self):
  r=p.calculate(self.data());self.assertEqual((r["profit"],r["income_margin_pct"],r["cost_return_pct"]),("10.00","50.00","100.00"));self.assertIn("净回款",r["income_margin_label"])
 def test_sales(self):
  r=p.calculate(self.data(mode="sale_price",platform_fee=2,platform_shipping=3));self.assertEqual((r["profit"],r["sales_margin_pct"]),("5.00","25.00"))
 def test_no_double_platform_cost(self):
  with self.assertRaises(ValueError): p.calculate(self.data(platform_fee=1))
 def test_optional_sales_denominator(self):
  r=p.calculate(self.data(selling_price=25));self.assertEqual(r["sales_margin_pct"],"40.00")
 def test_invalid_sales(self):
  with self.assertRaises(ValueError): p.calculate(self.data(selling_price=10))
 def test_missing_not_zero(self):
  d=self.data();d["costs_cny"]["ads"]=None;r=p.calculate(d);self.assertEqual(r["status"],"MISSING_INPUTS");self.assertIsNone(r["profit"]);self.assertIn("广告分摊",r["missing_inputs"])
 def test_explicit_zero(self):
  r=p.calculate(self.data(income=0));self.assertEqual(r["profit"],"-10.00");self.assertIsNone(r["income_margin_pct"])
 def test_zero_cost(self):
  r=p.calculate(self.data(costs_cny={k:0 for k in p.COSTS}));self.assertIsNone(r["cost_return_pct"]);self.assertEqual(r["profit"],"20.00")
 def test_missing_exchange(self):
  r=p.calculate(self.data(cny_per_currency=None));self.assertEqual(r["status"],"MISSING_INPUTS")
 def test_invalid_numbers(self):
  for v in [True,False,"NaN","Infinity","-1","1e100","1e-100",{},[]]:
   with self.subTest(v=v),self.assertRaises(ValueError):p.calculate(self.data(income=v))
 def test_zero_rate(self):
  with self.assertRaises(ValueError): p.calculate(self.data(cny_per_currency=0))
 def test_cny_identity(self):
  r=p.calculate(self.data(currency="CNY",income=100,cny_per_currency=None));self.assertEqual(r["profit"],"30.00")
 def test_sales_missing_fees(self):
  r=p.calculate(self.data(mode="sale_price"));self.assertIn("平台佣金",r["missing_inputs"]);self.assertIsNone(r["profit"])
 def test_decimal_rounding(self):
  r=p.calculate(self.data(income="20.005"));self.assertEqual(r["income"],"20.01");self.assertEqual(r["profit"],"10.01")

class Comparison(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
 def tearDown(self): self.temp.cleanup()
 def run_compare(self,c,**kw):
  with patch.object(p.operations,"store",return_value={"id":"s"}),patch.object(p.operations,"client_for",return_value=(c,{})):
   return p.compare(self.root,{"store_id":"s","query":"wrench","allow_public_fallback":False,**kw})
 def test_keyword_success_readonly(self):
  c=Client([item()]);r=self.run_compare(c);self.assertEqual(r["state"],"READY");self.assertEqual(r["price_bands"][0]["median"],"100.00");self.assertEqual(r["items"][0]["spec_match"],"NOT_VERIFIED")
 def test_forbidden_is_not_empty(self):
  r=self.run_compare(Client(error=MercadoLibreAPIError("forbidden",http_status=403)));self.assertEqual(r["state"],"UNAVAILABLE");self.assertEqual(r["errors"][0]["http_status"],403);self.assertTrue(r["platform_queried_at"])
 def test_empty_response(self):
  self.assertEqual(self.run_compare(Client())["state"],"NO_RESULTS")
 def test_currency_and_condition_groups(self):
  r=self.run_compare(Client([item(),item(currency_id="USD",price=10),item(condition="used",price=50)]));self.assertEqual(len(r["price_bands"]),3)
 def test_inactive_price_excluded(self):
  r=self.run_compare(Client([item(status="paused",price=1),item(price=100)]));self.assertEqual(r["price_bands"][0]["min"],"100.00")
 def test_baseline_same_currency_only(self):
  r=self.run_compare(Client([item(),item(currency_id="USD")]),reference_price=120,reference_currency="MXN");self.assertEqual(r["items"][0]["reference_gap_pct"],"20.00");self.assertIsNone(r["items"][1]["reference_gap_pct"])
 def test_specific_item_lookup(self):
  c=Client([item()]);r=self.run_compare(c,item_ids="MLM1234567890");self.assertEqual(c.gets,["/items/MLM1234567890"]);self.assertEqual(r["state"],"READY")
 def test_fallback(self):
  c=Client([item()]);original=c.get
  def get(path):
   if path.startswith("/items/"):raise MercadoLibreAPIError("forbidden",http_status=403)
   return original(path)
  c.get=get;r=self.run_compare(c,item_ids="MLM1234567890");self.assertEqual(r["state"],"READY");self.assertEqual(c.gets,["/marketplace/items/MLM1234567890"])
 def test_wrong_response_id(self):
  self.assertEqual(self.run_compare(Client([item(id="MLM9999999999")]),item_ids="MLM1234567890")["state"],"UNAVAILABLE")
 def test_item_id_validation(self):
  self.assertEqual(p.item_ids("https://articulo.mercadolibre.com.mx/MLM-1234567890-wrench-_JM","MLM"),["MLM1234567890"])
  for value in ["https://evil.com/MLM-1234567890-test","MLB1234567890","../../foo","https://www.mercadolibre.com.mx/x/p/MLM1234567890"]:
   with self.subTest(value=value),self.assertRaises(ValueError):p.item_ids(value,"MLM")
 def test_limit(self):
  with self.assertRaises(ValueError):p.item_ids(["MLM"+str(1234567890+i) for i in range(11)],"MLM")
 def test_untrusted_link_removed(self):
  r=self.run_compare(Client([item(permalink="javascript:alert(1)")]));self.assertIsNone(r["items"][0]["permalink"])
 def test_cache_scoped(self):
  self.run_compare(Client([item()]));self.assertTrue((self.root/"data/v3/pricing/s/MLM-latest.json").is_file())
 def test_bridge(self):
  d=Calculator().data()
  r=DataSource(self.root).calculate_profit(json.dumps(d));self.assertEqual(r["data"]["profit"],"10.00");self.assertIn("source",r)

class HttpBoundary(unittest.TestCase):
 def test_http_calculator_and_origin(self):
  with tempfile.TemporaryDirectory() as f:
   server=ThreadingHTTPServer(("127.0.0.1",0),w.make_handler(Path(f)));threading.Thread(target=server.serve_forever,daemon=True).start()
   url=f"http://127.0.0.1:{server.server_port}/api/v3/pricing/calculate";body=json.dumps(Calculator().data()).encode()
   try:
    with urlopen(Request(url,data=body,headers={"Content-Type":"application/json"})) as r:self.assertEqual(json.load(r)["profit"],"10.00")
    with self.assertRaises(HTTPError) as exc:urlopen(Request(url,data=body,headers={"Origin":"https://evil.example"}))
    self.assertEqual(exc.exception.code,400)
   finally:server.shutdown();server.server_close()


class AssistantNumericGuard(unittest.TestCase):
 def test_wrong_model_math_removed(self):
  from src.workbench_assistant_worker import checked_pricing_answer
  answer=checked_pricing_answer("10美元/70人民币=14.3%",[("calculate_profit",p.calculate(Calculator().data()))])
  self.assertNotIn("14.3",answer);self.assertIn("100.00%",answer);self.assertIn("10.00 USD",answer)
 def test_missing_no_profit_assertion(self):
  from src.workbench_assistant_worker import checked_pricing_answer
  d=Calculator().data();d["costs_cny"]["ads"]=None
  answer=checked_pricing_answer("利润10美元",[("calculate_profit",p.calculate(d))]);self.assertNotIn("利润10美元",answer);self.assertIn("广告分摊",answer)
 def test_rejected_query_no_fabricated_price(self):
  from src.workbench_assistant_worker import checked_pricing_answer
  r=dict(state="UNAVAILABLE",site_id="MLM",platform_queried_at="test",errors=[{"error":"本次未取得：403"}],items=[],limitations="同款未核验")
  answer=checked_pricing_answer("没有竞品，最低价1元",[("compare_market_prices",r)])
  self.assertNotIn("最低价1元",answer);self.assertIn("本次未取得",answer)


class RateAndAvailability(unittest.TestCase):
 def test_rate_source_and_direction(self):
  with patch.object(p,"fetch_exchange_rate",return_value={"rate":7,"provider":"fixture","date":"2026-10-11"}) as get:
   r=p.rate_quote({"currency":"USD"});self.assertEqual(r["cny_per_currency"],"7");get.assert_called_once_with("USD","CNY")
 def test_rate_unavailable_not_assumed(self):
  with patch.object(p,"fetch_exchange_rate",side_effect=p.ExchangeRateError("offline")):
   r=p.rate_quote({"currency":"USD"});self.assertEqual(r["status"],"UNAVAILABLE");self.assertNotIn("cny_per_currency",r)
 def test_missing_price_is_partial(self):
  with tempfile.TemporaryDirectory() as f,patch.object(p.operations,"store",return_value={"id":"s"}),patch.object(p.operations,"client_for",return_value=(Client([item(price=None)]),{})):
   r=p.compare(Path(f),{"store_id":"s","query":"x"});self.assertEqual(r["state"],"PARTIAL");self.assertFalse(r["price_bands"])
