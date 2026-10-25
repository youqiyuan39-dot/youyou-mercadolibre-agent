import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from src import target_publish_v3 as p
from src import workbench_v3 as w
from src.mercadolibre_client import MercadoLibreAPIError
from tests.test_publish_service import sample_draft, StubClient

class Client(StubClient):
    def __init__(self, root, partial=False):
        super().__init__(partial)
        self.root=root; self.gets=[]; self.mode="normal"; self.quota=100; self.allowed=True
    def me(self):
        return {"id":101,"tags":["user_product_seller"],"status":{"sell":{"allow":self.allowed}}}
    def marketplace_mapping(self, merchant):
        return {"marketplaces":[{"site_id":s,"logistic_type":"remote","user_id":i} for s,i in [("MLM",102),("MLB",103)]]}
    def get(self,path):
        self.gets.append(path)
        if path=="/marketplace/users/cap":
            return [{"site_id":s,"logistic_type":"remote","user_id":i,"quota":self.quota,"total_items":1} for s,i in [("MLM",102),("MLB",103)]]
        if path.startswith("/marketplace/items/"):
            item_id=path.split("/")[-1]
            return {"id":item_id,"site_id":item_id[:3],"status":"active",
                    "title":"Arrancador manual de retroceso","price":8.17,"currency_id":"USD",
                    "permalink":"https://articulo.mercadolibre.com.mx/MLM-123-_JM"}
        if path.startswith("/items/") and path.endswith("/description"):
            return {"plain_text":"Descripción en español del arrancador manual."}
        if path.startswith("/marketplace/user-products/"):
            return [{"siteless_user_product_id":path.split("/")[-2],"site_items":[{"site_id":"MLM","item_id":"MLM1","logistic_type":"remote"},{"site_id":"MLB","item_id":"MLB1","logistic_type":"remote"}]}]
        raise AssertionError(path)
    def post_json(self,path,payload):
        # Verify journal was persisted before the remote creation call.
        current=p.record(self.root,"test-draft.json","A")
        assert current["request_in_flight"] is True
        if self.mode=="timeout":
            self.posts.append((path,payload))
            raise TimeoutError("response timed out")
        if self.mode=="reject":
            raise MercadoLibreAPIError("validation rejected",http_status=400)
        return super().post_json(path,payload)

class TargetPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root=Path(self.temp.name); (self.root/"drafts").mkdir(); (self.root/"assets").mkdir()
        self.name="test-draft.json"; self.draft=sample_draft()
        p.save(self.root/"drafts"/self.name,self.draft)
        (self.root/"assets/product.png").write_bytes(b"test image bytes")
        self.row={"id":"A","alias":"store A","merchant_id":101,"auth_type":"CBT"}
        self.client=Client(self.root)
        self.patches=[patch.object(p.operations,"store",return_value=self.row),
                      patch.object(p.operations,"client_for",side_effect=lambda root,row:(self.client,self.client.me())),
                      patch.object(p,"_local_issues",return_value=[])]
        for x in self.patches:x.start()
    def tearDown(self):
        for x in reversed(self.patches):x.stop()
        self.temp.cleanup()
    def prepare(self):
        return p.prepare(self.root,self.name,"A")["preview"]
    def test_preview_is_read_only_and_freezes_images_without_approval(self):
        preview=self.prepare()
        self.assertTrue(preview["valid"])
        self.assertFalse((self.root/"reviews").exists())
        self.assertFalse(self.client.uploads);self.assertFalse(self.client.posts)
        self.assertEqual(len(preview["image_hashes"]),1)
        self.assertTrue(all("UPLOAD_REQUIRED" in str(r["pictures"]) for r in preview["request"]))
    def test_supplier_stock_cannot_be_used_as_sellable_stock(self):
        self.draft["evidence"]["sku_details"] = [{"sku_id":"A", "inventory_source":"EXISTING_SUPPLIER_CAPTURE", "available_quantity":110983}]
        self.assertTrue(p._supplier_inventory_issues(self.draft))
        self.draft["evidence"]["sku_details"][0].update(inventory_source="DEFAULT_200_USER_POLICY", available_quantity=200)
        self.assertEqual(p._supplier_inventory_issues(self.draft), [])
    def test_explicit_confirm_required_before_any_write(self):
        preview=self.prepare()
        with self.assertRaises(p.Error):p.commit(self.root,preview["id"],False)

    def test_changed_margin_policy_invalidates_prepared_plan_before_upload(self):
        preview=self.prepare()
        p.save(self.root/'config/autopricing_policy.json', {'minimum_margin_pct':'10','margin_policy_status':'PENDING_CONFIRMATION'})
        with self.assertRaisesRegex(p.Error,'政策已变化'):
            p.commit(self.root,preview['id'],True)
        self.assertFalse(self.client.uploads);self.assertFalse(self.client.posts)

    def test_pending_policy_cannot_prepare_a_release_even_without_integrated_plan(self):
        p.save(self.root/'config/autopricing_policy.json', {'minimum_margin_pct':'10','margin_policy_status':'PENDING_CONFIRMATION'})
        preview=self.prepare()
        self.assertFalse(preview['valid'])
        self.assertIn('待确认',str(preview['issues']))
        self.assertFalse(self.client.uploads);self.assertFalse(self.client.posts)
    def test_unreviewed_images_are_frozen_for_final_confirmation_with_exact_count(self):
        from src.draft_editor import publish_preflight_issues
        self.draft["payload"]["attributes"][1]["value_name"] = "Verified model"
        self.draft["evidence"]["images_human_reviewed"] = False
        self.draft["evidence"]["image_review"]["gallery"] = []
        for index in range(3):
            filename = f"image-{index}.png"
            (self.root / "assets" / filename).write_bytes(f"image {index}".encode())
            self.draft["evidence"]["image_review"]["gallery"].append(
                {"local_file": filename, "review_status": "UNREVIEWED"})
        p.save(self.root / "drafts" / self.name, self.draft)
        with patch.object(p, "_local_issues", side_effect=lambda root, name: publish_preflight_issues(p.read(root / "drafts" / name))):
            preview = self.prepare()
        self.assertTrue(preview["valid"], preview["issues"])
        self.assertEqual(preview["image_sets"][0]["seller_sku"], "AUTO-123")
        self.assertEqual(preview["image_sets"][0]["count"], 3)
        self.assertEqual(set(preview["image_sets"][0]["files"]), set(preview["image_hashes"]))
        with self.assertRaisesRegex(p.Error, "勾选确认"):
            p.commit(self.root, preview["id"], False)
        self.assertFalse(self.client.uploads);self.assertFalse(self.client.posts)
    def test_commit_bound_to_receipt_and_reconciles_active_items(self):
        preview=self.prepare()
        result=p.commit(self.root,preview["id"],True)
        self.assertEqual(result["record"]["status"],"success")
        self.assertEqual(result["record"]["store"]["id"],"A")
        self.assertEqual(len(self.client.posts),1)
        self.assertIn("/marketplace/items/MLM1",self.client.gets)
        with self.assertRaises(p.Error):p.commit(self.root,preview["id"],True)
        self.assertEqual(len(self.client.posts),1)
    def test_platform_content_readback_does_not_confuse_local_suggestion_with_actual_title(self):
        preview=self.prepare()
        self.assertEqual(preview["site_title_suggestions"],[])
        result=p.commit(self.root,preview["id"],True)["record"]
        site=result["variants"][0]["sites"][0]
        self.assertEqual(site["marketplace_title"],"Arrancador manual de retroceso")
        self.assertIn("Descripción en español",site["marketplace_description"])
        self.assertEqual(site["marketplace_price"],8.17)
    def test_description_readback_failure_does_not_mark_active_listing_failed(self):
        original=self.client.get
        def get(path):
            if path.endswith("/description"):
                raise TimeoutError("description unavailable")
            return original(path)
        with patch.object(self.client,"get",side_effect=get):
            result=p.commit(self.root,self.prepare()["id"],True)["record"]
        self.assertEqual(result["status"],"success")
        self.assertIn("description unavailable",result["variants"][0]["sites"][0]["content_check_error"])
    def test_auto_refresh_reconciles_once_without_reposting(self):
        current={"store":p.public_store(self.row),"status":"pending_confirmation",
                 "updated_at":time.time()-100,"auto_refresh_next_at":time.time()-1,
                 "variants":[{"seller_sku":"SKU","siteless_user_product_id":"U123",
                              "sites":[{"site_id":"MLM","logistic_type":"remote","status":"pending_confirmation"}]}]}
        p.save(p.record_path(self.root,self.name,"A"),current)
        checked=p.auto_refresh(self.root,self.name,"A")
        self.assertTrue(checked["auto_refresh_performed"])
        self.assertEqual(checked["record"]["status"],"success")
        self.assertEqual(checked["record"]["auto_refresh_attempts"],1)
        self.assertFalse(p.auto_refresh(self.root,self.name,"A")["auto_refresh_performed"])
        self.assertEqual(self.client.posts,[])
    def test_unknown_result_clears_only_after_all_known_items_are_verified_active(self):
        current={"store":p.public_store(self.row),"status":"pending_confirmation",
                 "outcome_unknown":True,"request_in_flight":True,"updated_at":time.time()-100,
                 "auto_refresh_next_at":time.time()-1,
                 "variants":[{"seller_sku":"SKU","sites":[{"site_id":"MLM","item_id":"MLM1",
                                                    "status":"pending_confirmation"}]}]}
        p.save(p.record_path(self.root,self.name,"A"),current)
        checked=p.auto_refresh(self.root,self.name,"A")["record"]
        self.assertEqual(checked["status"],"success")
        self.assertFalse(checked["outcome_unknown"])
        self.assertFalse(checked["request_in_flight"])
        self.assertEqual(self.client.posts,[])
    def test_auto_refresh_stops_after_three_reads_even_when_platform_stays_paused(self):
        current={"store":p.public_store(self.row),"status":"pending_confirmation",
                 "updated_at":time.time()-100,"auto_refresh_next_at":time.time()-1,
                 "variants":[{"seller_sku":"SKU","siteless_user_product_id":"U123",
                              "sites":[{"site_id":"MLM","item_id":"MLM1","status":"pending_confirmation"}]}]}
        p.save(p.record_path(self.root,self.name,"A"),current)
        original=self.client.get
        def get(path):
            result=original(path)
            if path.startswith("/marketplace/items/"):
                return {**result,"status":"paused"}
            return result
        with patch.object(self.client,"get",side_effect=get):
            for attempt in range(3):
                row=p.read(p.record_path(self.root,self.name,"A"))
                row["auto_refresh_next_at"]=time.time()-1
                p.save(p.record_path(self.root,self.name,"A"),row)
                self.assertTrue(p.auto_refresh(self.root,self.name,"A")["auto_refresh_performed"])
            before=len(self.client.gets)
            self.assertFalse(p.auto_refresh(self.root,self.name,"A")["auto_refresh_performed"])
        self.assertEqual(len(self.client.gets),before)
        self.assertEqual(p.record(self.root,self.name,"A")["auto_refresh_attempts"],3)
        self.assertEqual(self.client.posts,[])
    def test_background_sweep_checks_bound_pending_without_browser_or_reposting(self):
        current={"store":p.public_store(self.row),"draft":self.name,
                 "status":"pending_confirmation","updated_at":time.time()-100,
                 "auto_refresh_next_at":time.time()-1,
                 "variants":[{"seller_sku":"SKU","sites":[{"site_id":"MLM",
                             "item_id":"MLM1","status":"pending_confirmation"}]}]}
        p.save(p.record_path(self.root,self.name,"A"),current)
        p.save(p.record_path(self.root,self.name,"B"),current)  # Wrong bound store.
        self.assertEqual(p.sweep_pending(self.root),1)
        self.assertEqual(p.record(self.root,self.name,"A")["status"],"success")
        self.assertEqual(p.record(self.root,self.name,"B")["status"],"pending_confirmation")
        self.assertEqual(self.client.posts,[])
    def test_other_active_store_cannot_change_receipt_target(self):
        seen=[]
        with patch.object(p.operations,"client_for",side_effect=lambda root,row:(seen.append(row["id"]) or self.client,self.client.me())):
            preview=self.prepare()
            p.commit(self.root,preview["id"],True)
        self.assertEqual(set(seen),{"A"})
    def test_draft_change_invalidates_receipt(self):
        preview=self.prepare();d=copy.deepcopy(self.draft);d["payload"]["available_quantity"]=4
        p.save(self.root/"drafts"/self.name,d)
        with self.assertRaises(p.Error):p.commit(self.root,preview["id"],True)
        self.assertFalse(self.client.uploads)
    def test_frozen_image_change_invalidates_receipt(self):
        preview=self.prepare()
        (self.root/"assets"/next(iter(preview["image_hashes"]))).write_bytes(b"changed")
        with self.assertRaises(p.Error):p.commit(self.root,preview["id"],True)
        self.assertFalse(self.client.posts)
    def test_record_change_invalidates_receipt(self):
        preview=self.prepare();p.save(p.record_path(self.root,self.name,"A"),{"status":"pending_confirmation"})
        with self.assertRaises(p.Error):p.commit(self.root,preview["id"],True)
    def test_expired_receipt_is_not_consumed(self):
        preview=self.prepare()
        file=self.root/"data/v3/publishing/plans"/(preview["id"]+".json")
        plan=p.read(file);plan["expires_at"]=0;p.save(file,plan)
        with self.assertRaises(p.Error):p.commit(self.root,preview["id"],True)
        self.assertFalse(self.client.posts)
    def test_store_identity_changed_stops_commit(self):
        preview=self.prepare();self.row["merchant_id"]=999
        with self.assertRaises(p.Error):p.commit(self.root,preview["id"],True)
        self.assertFalse(self.client.posts)
    def test_timeout_is_unknown_and_cannot_be_recreated(self):
        preview=self.prepare();self.client.mode="timeout"
        result=p.commit(self.root,preview["id"],True)
        self.assertEqual(result["record"]["status"],"pending_confirmation")
        self.assertTrue(result["record"]["outcome_unknown"])
        self.assertFalse(self.prepare()["valid"])
        refreshed=p.refresh(self.root,self.name,"A")["record"]
        self.assertEqual(refreshed["status"],"pending_confirmation")
        self.assertEqual(len(self.client.posts),1)
    def test_definite_rejection_allows_new_human_confirmed_attempt(self):
        preview=self.prepare();self.client.mode="reject"
        result=p.commit(self.root,preview["id"],True)
        self.assertEqual(result["record"]["status"],"failed")
        self.assertTrue(result["record"]["creation_rejected"])
        self.client.mode="normal";second=self.prepare()
        self.assertTrue(second["valid"]);self.assertFalse(second["retry_failed_only"])
        self.assertEqual(p.commit(self.root,second["id"],True)["record"]["status"],"success")
    def test_partial_retry_excludes_successful_sites(self):
        self.client.partial=True
        result=p.commit(self.root,self.prepare()["id"],True)["record"]
        self.assertEqual(result["status"],"success") # Mapping recovers the simulated delayed MLM site.
        # Explicit platform rejection should remain failed until a user chooses retry.
        result["status"]="partial"
        result["variants"][0]["sites"][1].update(status="failed",item_id=None,error="unsupported")
        p.save(p.record_path(self.root,self.name,"A"),result)
        with patch.object(self.client,"get",side_effect=self.client.get):
            preview=self.prepare()
        self.assertTrue(preview["retry_failed_only"])
        self.assertEqual([s["site_id"] for x in preview["request"] for s in x["sites_to_sell"]],["MLM"])
        self.client.partial=False
        result=p.commit(self.root,preview["id"],True)["record"]
        self.assertEqual(result["status"],"success")
        self.assertNotEqual(self.client.posts[-1][0],"/global/user-products/families")
        self.assertEqual([s["site_id"] for s in self.client.posts[-1][1]["sites_to_sell"]],["MLM"])
    def test_unknown_sale_permission_and_insufficient_quota_block(self):
        self.client.allowed=None
        self.assertFalse(self.prepare()["valid"])
        self.client.allowed=True;self.client.quota=1
        self.assertFalse(self.prepare()["valid"])
    def test_unlimited_quota_zero_is_supported(self):
        self.client.quota=0;self.assertTrue(self.prepare()["valid"])
    def test_missing_site_permission_blocks_no_uploads(self):
        with patch.object(p.operations,"mappings",return_value=[]):
            self.assertFalse(self.prepare()["valid"])
        self.assertFalse(self.client.uploads)
    def test_unbound_legacy_record_blocks_duplicate(self):
        p.save(self.root/"data/publish_records/test-draft.json",{"status":"success"})
        self.assertFalse(self.prepare()["valid"]);self.assertFalse(self.client.posts)
    def test_refresh_checks_store_ownership(self):
        p.save(p.record_path(self.root,self.name,"A"),{"store":{"merchant_id":999},"status":"success"})
        with self.assertRaises(p.Error):p.refresh(self.root,self.name,"A")
    def test_ui_requires_unchecked_explicit_confirmation(self):
        source=(Path(__file__).parents[1]/"web/publish.js").read_text(encoding="utf-8")
        self.assertIn('id="confirmPublish">',source)
        self.assertIn('id="commitPublish" disabled',source)
        self.assertNotIn("正式发布待接通",source)
    def test_embedded_adapter_cannot_override_explicit_store_selector(self):
        source=(Path(__file__).parents[1]/"assets/ui/embedded-v3.js").read_text(encoding="utf-8")
        self.assertIn("!store||document.querySelector('#publishStore')",source)
    def test_path_traversal_rejected(self):
        for name in ["../test-draft.json",r"..\test-draft.json","C:test-draft.json"]:
            with self.assertRaises(p.Error):p.prepare(self.root,name,"A")
    def test_row_projection_preserves_bound_store(self):
        self.client.mode="timeout";p.commit(self.root,self.prepare()["id"],True)
        r=p.decorate(self.root,{"name":self.name,"publish_status":"pending"},"A")
        self.assertTrue(r["publish_bound"]);self.assertEqual(r["publish_status"],"pending_confirmation")
        other=p.decorate(self.root,{"name":self.name,"publish_status":"pending"},"B")
        self.assertNotIn("publish_bound",other)


class PublishHttpTests(unittest.TestCase):
    def test_http_routes_require_receipt_and_preserve_selected_store(self):
        from http.server import ThreadingHTTPServer
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        import threading
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            server=ThreadingHTTPServer(("127.0.0.1",0),w.make_handler(Path(folder)))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base="http://127.0.0.1:"+str(server.server_port)
            def post(route,value,origin=None):
                headers={"Content-Type":"application/json"}
                if origin:headers["Origin"]=origin
                return urlopen(Request(base+route,data=json.dumps(value).encode(),headers=headers))
            try:
                with self.assertRaises(HTTPError) as caught:
                    post("/api/v3/publish/commit",{"confirmed":False,"plan_id":"a"*32})
                self.assertEqual(caught.exception.code,400)
                with patch.object(p,"prepare",return_value={"ok":True,"preview":{"valid":False}}) as prepare:
                    with post("/api/v3/publish/preview",{"name":"test-draft.json","store_id":"B"}) as response:
                        self.assertFalse(json.load(response)["preview"]["valid"])
                    prepare.assert_called_once_with(Path(folder),"test-draft.json","B")
                with patch.object(p,"prepare") as prepare:
                    with self.assertRaises(HTTPError):
                        post("/api/v3/publish/preview",{"name":"test-draft.json","store_id":"B"},"http://untrusted.example")
                    prepare.assert_not_called()
                for route in ["/api/drafts/test-draft.json/publish","/api/drafts/test-draft.json/publish-retry","/api/drafts/test-draft.json/publish-refresh","/api/publish/batch"]:
                    with self.assertRaises(HTTPError) as caught:post(route,{"confirmed":True})
                    self.assertEqual(caught.exception.code,409)
            finally:server.shutdown();server.server_close();thread.join()

if __name__=="__main__":unittest.main()
