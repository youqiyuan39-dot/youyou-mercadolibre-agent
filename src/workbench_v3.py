"""Additive workbench: keeps v0.2 source/data, serves the v0.3 UI separately."""
from __future__ import annotations
from . import selection_research_v3 as research
from . import selection_discovery_v3 as discovery

import argparse
import base64
import copy
import csv
import io
import os
import json
import re
import socket
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from xml.sax.saxutils import escape

from . import draft_editor as legacy
from .product_core_v3 import clean_description, product_from_draft, review_issues, target_identity
from .generation_strategy_v3 import load as load_generation, validate as validate_generation
from . import store_operations_v3 as operations
from . import target_publish_v3 as publishing
from . import pricing_tools_v3 as pricing
from . import autopricing_v3 as autopricing
from . import selection_v3 as selection
from . import product_audit_agent as product_audit
from . import business_brief_v3 as business_brief
from . import workbench_assistant as assistant
from . import workbench_assistant_actions as assistant_actions
from .collector_receiver import ensure_receiver
from . import sku_processing_v3, project_knowledge_v3, task_control_v3, task_evidence_v3, attribute_context_v3
from . import supplier1688_routes, supplier1688_runtime, source_intake, public_read_v3, category_resolution_v3, market_follow_v3, listing_brand_v3, model_resolution_v3

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.3.0-evidence"
LOCK = threading.RLock()
PROCESSING = {"BLOCKED_EVIDENCE", "CAPTURE_READY", "READY_FOR_AI", "AI_PROCESSING", "AI_PAUSED", "AI_FAILED", "READ_ERROR"}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    from .atomic_json_v3 import write as atomic_write
    atomic_write(path,value)


def audit(root, event, detail):
    # Only explicitly selected public fields are accepted by callers.
    path = root / "data" / "v3" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with LOCK, path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time": stamp(), "event": event, "detail": detail}, ensure_ascii=False) + "\n")


def safe_name(name):
    if Path(str(name)).name != name or not str(name).endswith("-draft.json"):
        raise ValueError("草稿文件名无效")
    return str(name)


def publish_center_row(row):
    """Route completed products without creating or inferring human approval."""
    row = dict(row)
    status = row.get("publish_status")
    terminal = {"success": "PUBLISHED", "partial": "PUBLISH_PARTIAL",
                "pending_confirmation": "PUBLISH_PENDING_CONFIRMATION"}
    full = row.get("full_ai_status")
    if status in terminal:
        row["stage"] = terminal[status]
        row["in_publish_center"] = True
        return row
    if full in {"queued", "running", "paused", "failed"}:
        row["stage"] = {"queued": "AI_PROCESSING", "running": "AI_PROCESSING",
                        "paused": "AI_PAUSED", "failed": "AI_FAILED"}[full]
        row["in_publish_center"] = False
        row["publish_ready"] = False
        row["next_action"] = {"paused": "AI 已暂停，可在商品处理继续",
                              "failed": "AI 失败，请在商品处理查看日志后重试"}.get(full, "AI 处理中，完成后自动进入发布中心")
        return row
    completed = full == "done" or row.get("stage") in {
        "READY_FOR_HUMAN_REVIEW", "READY_TO_PUBLISH", "PUBLISH_BLOCKED"}
    row["in_publish_center"] = completed
    if completed:
        issues = list(dict.fromkeys((row.get("errors") or []) +
                      (row.get("publish_issues") or []) + (row.get("evidence_issues") or [])))
        row["publish_issues"] = issues
        row["publish_ready"] = bool(row.get("publish_ready")) and not issues
        row["stage"] = "READY_TO_PUBLISH" if row["publish_ready"] else "PUBLISH_BLOCKED"
        row["next_action"] = f"在发布中心查看 {len(issues)} 项待补资料" if issues else "已进入发布中心；发布前校验并确认"
        if full == "done":
            row["full_ai_stage"] = "本地处理结束，仍有缺项" if issues else "处理完成，已进入发布中心"
            row["full_ai_message"] = f"AI 产物已保存；{len(issues)} 项资料需补齐" if issues else "AI 产物已保存；发布资料检查在发布中心完成"
    return row


def draft_rows(root, publish_store=""):
    rows = legacy.list_drafts(root / "drafts", root)
    for row in rows:
        draft = legacy.read_json(root / "drafts" / row["name"])
        row["evidence_issues"] = review_issues(draft)
        row["review_ready"] = row.get("review_ready") and not row["evidence_issues"]
        row["publish_ready"] = row.get("publish_ready") and not row["evidence_issues"]
        from .integrated_pricing_v3 import summary as pricing_summary
        row["ai_processing_pricing"] = pricing_summary(draft)
        row["sku_processing"] = (draft.get('evidence') or {}).get('processing_automation') or {}
        row["source"] = urlparse(row.get("source_url") or "").hostname or "人工建品"
    return [publish_center_row(publishing.decorate(root, row, publish_store)) for row in rows]


def overview(root):
    rows = draft_rows(root)
    sources, counts = {}, {}
    for row in rows:
        sources[row["source"]] = sources.get(row["source"], 0) + 1
        counts[row["stage"]] = counts.get(row["stage"], 0) + 1
    issues = [{"name": r["name"], "title": r["title"], "issues": r["evidence_issues"] + (r.get("errors") or [])} for r in rows if r["evidence_issues"] or r.get("errors")]
    events_file = root / "data" / "v3" / "events.jsonl"
    events = []
    if events_file.is_file():
        events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()[-20:] if line.strip()][::-1]
    return {"ok": True, "version": VERSION, "total": len(rows), "processing": sum(r["stage"] in PROCESSING for r in rows), "review": 0, "pending_publish": sum(r.get("in_publish_center") and r.get("publish_status") in {"pending", "failed"} for r in rows), "published": counts.get("PUBLISHED", 0), "partial": counts.get("PUBLISH_PARTIAL", 0), "counts": counts, "sources": sources, "issues": issues, "events": events, "business_brief": business_brief.build(rows)}


def create_product(root, values):
    title = str(values.get("title") or "").strip()[:120]
    images = values.get("images") or []
    if not title or not isinstance(images, list) or not 1 <= len(images) <= 10:
        raise ValueError("需填写商品名称并提供 1–10 张图片")
    mode = values.get("mode") or "single"
    if mode not in {"single", "multi", "bundle"}:
        raise ValueError("建品模式无效")
    if mode == "bundle" and not values.get("components"):
        raise ValueError("组合商品需填写组成项与数量")
    product_id = uuid.uuid4().hex
    gallery = []
    for n, item in enumerate(images):
        if not isinstance(item, dict):
            raise ValueError("图片记录无效")
        if item.get("data_url"):
            source = legacy.save_uploaded_image(product_id + "-" + str(n) + "-" + str(item.get("name") or "image.png"), item["data_url"], root / "assets")
            row = {"local_file": source}
        else:
            url = str(item.get("url") or "").strip()
            parsed = urlparse(url)
            if url.startswith("/assets/"):
                local = (root / url.lstrip("/")).resolve()
                if (root / "assets").resolve() not in local.parents or not local.is_file() or local.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                    raise ValueError("本机图片需在当前工作区 assets 中且为有效图片文件")
                row = {"local_file": local.relative_to(root / "assets").as_posix()}
            elif parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("图片链接需为 HTTPS 地址")
            else:
                row = {"remote_url": url}
        gallery.append({**row, "role": "main" if n == 0 else f"listing_{n+1}", "source": "manual_images", "image_type": "product", "review_status": "UNREVIEWED"})
    sku_details = []
    if mode == "multi":
        for n, sku in enumerate(values.get("skus") or []):
            if not isinstance(sku, dict) or not str(sku.get("variant") or "").strip():
                raise ValueError("每个 SKU 需要规格名")
            indices = sku.get("image_indices") or []
            if not indices or any(not isinstance(i, int) or i < 0 or i >= len(gallery) for i in indices):
                raise ValueError("每个 SKU 需绑定有效图片序号")
            sku_details.append({"sku_id": f"{product_id}-{n+1}", "variant": sku["variant"], "seller_sku": sku.get("seller_sku") or f"MANUAL-{product_id[:8]}-{n+1}", "available_quantity": sku.get("available_quantity") if sku.get("available_quantity") is not None else 200, "inventory_source": "USER_INPUT" if sku.get("available_quantity") is not None else "DEFAULT_200_USER_POLICY", "purchase_cost_cny": sku.get("purchase_cost_cny"), "package": sku.get("package") or {}, "image_indices": indices})
        if len(sku_details) < 2:
            raise ValueError("多 SKU 建品至少需要两个规格")
    evidence = {"source_product_id": product_id, "source_kind": "manual_images", "supplier_url": values.get("source_url") or "", "supplier_page": {"title": title, "description": str(values.get("description") or ""), "skus": sku_details}, "sku_details": sku_details, "image_review": {"source_gallery": copy.deepcopy(gallery), "gallery": gallery, "sku_source_galleries": [{"sku_id": s["sku_id"], "variant": s["variant"], "gallery": [copy.deepcopy(gallery[i]) for i in s["image_indices"]]} for s in sku_details]}, "images_human_reviewed": False, "components": values.get("components") or []}
    if sku_details:
        evidence["current_pricing_sku_id"] = sku_details[0]["sku_id"]
    for field in ("purchase_cost", "packed_weight", "packed_dimensions"):
        if values.get(field) is not None:
            evidence[field] = copy.deepcopy(values[field])
            if isinstance(evidence[field], dict):
                evidence[field].setdefault("status", "USER_INPUT_UNVERIFIED")
    payload = {"title": title, "family_name": title, "description": str(values.get("description") or ""), "category_id": "", "available_quantity": values.get("available_quantity") if values.get("available_quantity") is not None else 200, "currency_id": "USD", "attributes": [], "sites_to_sell": [], "pictures": [], "catalog_listing": False}
    draft = {"schema_version": "0.3", "status": "DRAFT_LOCAL_ONLY", "human_approved": False, "payload": payload, "evidence": evidence, "edit_metadata": {"created_at": stamp(), "source": "image_builder", "field_locks": {}}, "requested_action": "prepare"}
    name = f"manual-{product_id}-draft.json"
    write(root / "drafts" / name, draft)
    write(root / "data" / "v3" / "products" / f"{product_id}.json", product_from_draft(draft))
    audit(root, "图片建品", {"name": name, "mode": mode, "images": len(images), "skus": len(sku_details)})
    return {"ok": True, "name": name, "publishing_performed": False, "paid_model_called": False}


def templates(root):
    return [legacy.read_json(p) for p in sorted((root / "data" / "v3" / "templates").glob("*.json")) if not legacy.read_json(p).get("deleted")]


def save_template(root, value):
    template_id = str(value.get("id") or uuid.uuid4().hex)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", template_id):
        raise ValueError("模板 ID 无效")
    path = root / "data" / "v3" / "templates" / f"{template_id}.json"
    old = legacy.read_json(path) if path.is_file() else {}
    record = {"id": template_id, "name": str(value.get("name") or "").strip()[:80], "content": str(value.get("content") or "").strip()[:12000], "version": int(old.get("version") or 0) + 1, "updated_at": stamp(), "deleted": bool(value.get("deleted")), "previous": [{k: old.get(k) for k in ("version", "name", "content", "updated_at")}]+(old.get("previous") or []) if old else []}
    if not record["name"] or not record["content"]:
        raise ValueError("模板名称和内容不能为空")
    write(path, record)
    audit(root, "保存模板", {"id": template_id, "version": record["version"], "deleted": record["deleted"]})
    return record


def import_csv(root, text):
    text = str(text or "").lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"title", "image_urls"}.issubset(reader.fieldnames):
        raise ValueError("CSV 至少需要 title、image_urls 两列；图片用 | 分隔")
    records = list(reader)
    if len(records) > 100:
        raise ValueError("单次最多导入100行")
    results = []
    for n, item in enumerate(records, 2):
        try:
            values = {"title": item.get("title"), "source_url": item.get("source_url"), "description": item.get("description"), "images": [{"url": u.strip()} for u in str(item.get("image_urls") or "").split("|") if u.strip()]}
            for key in ("purchase_cost", "packed_weight", "packed_dimensions", "available_quantity"):
                if item.get(key):
                    values[key] = json.loads(item[key])
            results.append({"row": n, "status": "created", **create_product(root, values)})
        except (ValueError, legacy.EditorError, OSError) as exc:
            results.append({"row": n, "status": "failed", "error": str(exc)[:300]})
    return {"ok": True, "results": results, "publishing_performed": False}


def export_xlsx(rows):
    columns = ["name", "title", "source_url", "category_id", "stage", "sku_count", "weight_g", "target_net_proceeds_usd", "pricing_status", "publish_status", "evidence_issues"]
    grid = [columns] + [[json.dumps(r.get(c), ensure_ascii=False) if isinstance(r.get(c), (list, dict)) else str(r.get(c) if r.get(c) is not None else "") for c in columns] for r in rows]
    def letter(n):
        s = ""
        while n:
            n, digit = divmod(n-1, 26); s = chr(65+digit)+s
        return s
    data = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join('<row r="%s">%s</row>' % (n, ''.join('<c r="%s%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (letter(i), n, escape(cell)) for i, cell in enumerate(row, 1))) for n, row in enumerate(grid, 1)) + '</sheetData></worksheet>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="商品核对" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', data)
    return buf.getvalue()


def install_guards():
    from . import ai_listing_pipeline
    from .flow_guards_v3 import install
    install(legacy, ai_listing_pipeline)
    from .flow_resume_v3 import install as install_resume
    install_resume(legacy)
    from .integrated_pricing_v3 import install as install_pricing
    install_pricing(legacy)
    from .sku_processing_v3 import install as install_sku_processing
    install_sku_processing(legacy)
    from .auto_processing_v3 import install_edit_resume
    install_edit_resume(legacy)
    assistant_actions.install_task_tracking(legacy)


def make_handler(root=ROOT):
    base = legacy.make_handler(root)
    class Handler(base):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ('/api/v3/input-requests', '/api/v3/input-requests/export.csv'):
                from . import input_requests_v3
                if parsed.path.endswith('.csv'):
                    body = input_requests_v3.export_csv(root)
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/csv; charset=utf-8')
                    self.send_header('Content-Disposition', 'attachment; filename="input-requests.csv"')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                return self._json(200, input_requests_v3.aggregate(root))
            if supplier1688_routes.get(self,root,parsed.path):return
            if parsed.path.startswith("/api/v3/selection"):
                return self._json(410, {"ok":False,"error":"AI选品模块已退出当前项目；选品知识可询问工作助手。"})
            if parsed.path in {"/review", "/legacy/review", "/legacy/dashboard"}:
                self.send_response(302)
                self.send_header("Location", "/#publish" if "review" in parsed.path else "/#processing")
                self.end_headers()
                return
            if parsed.path == "/api/drafts":
                return self._json(200, {"ok": True, "items": draft_rows(root)})
            if parsed.path == "/":
                return self._file(ROOT / "web" / "workbench_v3.html", "text/html; charset=utf-8")
            if parsed.path in {"/workbench_v3.js", "/workbench_v3.css", "/operations_v3.js", "/pricing_v3.js", "/selection_v3.js", "/selection_discovery_v3.js", "/selection_v3.css", "/product_audit_v3.js", "/workbench_assistant.js", "/workbench_assistant.css"}:
                return self._file(ROOT / "web" / parsed.path.lstrip("/"), "text/javascript; charset=utf-8" if parsed.path.endswith(".js") else "text/css; charset=utf-8")
            if parsed.path.startswith("/legacy/"):
                self.path = self.path.removeprefix("/legacy")
                if parsed.path == "/legacy/dashboard": self.path = "/"
                return super().do_GET()
            if parsed.path == "/api/health":
                from . import source_image_screening_v3
                return self._json(200, {"ok": True, "version": VERSION, "operations_version":"2026-10-03", "product_audit_version":product_audit.VERSION, "workspace": str(root), "mode": "AI_TO_PUBLISH_CENTER", "real_publish_enabled": True, "live_publish_verified": False, "publish_version": publishing.VERSION, "pricing_version": pricing.VERSION, "selection_version": selection.VERSION, "research_version":research.VERSION, "discovery_version":discovery.VERSION, "assistant_version": assistant.VERSION, "desktop_lifecycle":"WINDOW_BOUND_V1", "local_optimization":"MATCH_EVIDENCE_FLOOR10_20261018_V1", "processing_version":sku_processing_v3.VERSION, "screening_version":source_image_screening_v3.VERSION, "screening_loaded_from":source_image_screening_v3.__file__, "listing_brand_version":listing_brand_v3.VERSION, "model_resolution_version":model_resolution_v3.VERSION, "category_resolution_version":category_resolution_v3.VERSION, "attribute_semantic_version":attribute_context_v3.SEMANTIC_VERSION, "market_follow_version":market_follow_v3.VERSION, "public_read_version":public_read_v3.VERSION, "source_intake_version":source_intake.VERSION, "supplier1688_version":supplier1688_runtime.VERSION, "supplier1688_loaded_from":supplier1688_runtime.__file__, "knowledge_version":project_knowledge_v3.VERSION, "task_control_version":task_control_v3.VERSION, "task_evidence_version":task_evidence_v3.VERSION, "attribute_policy_version":attribute_context_v3.POLICY_VERSION, "process_id":os.getpid(), "collector": getattr(self.server, "collector_status", {"ok": False, "state": "NOT_MANAGED"})})
            try:
                if parsed.path == '/api/v3/selection/discovery': return self._json(200, discovery.overview(root))
                if parsed.path == '/api/v3/selection/discovery/task': return self._json(200, {'ok':True,'task':discovery.expire(root,discovery.get(root,parse_qs(parsed.query).get('id',[''])[0]))})
                if parsed.path == '/api/v3/selection/research': return self._json(200, research.list_cases(root))
                if parsed.path == '/api/v3/selection/research/version': return self._json(200, {'ok':True,'case':research.get(root,parse_qs(parsed.query).get('id',[''])[0])})
                if parsed.path == '/api/v3/selection/source': return self._json(200, selection.get_source(root,parse_qs(parsed.query).get('name',[''])[0]))
                if parsed.path == '/api/v3/selection': return self._json(200, selection.list_batches(root))
                if parsed.path == '/api/v3/selection/batch': return self._json(200, {'ok':True,'batch':selection.get_batch(root,parse_qs(parsed.query).get('id',[''])[0])})
                if parsed.path == '/api/v3/pricing/policy': return self._json(200, {'ok':True,'policy':autopricing.policy(root)})
                if parsed.path == '/api/v3/pricing/inputs': return self._json(200, pricing.inputs(root))
                if parsed.path == '/api/v3/assistant/status': return self._json(200, assistant.status(root))
                assistant_match = re.fullmatch(r'/api/v3/assistant/jobs/([a-f0-9]{32})', parsed.path)
                if assistant_match: return self._json(200, assistant.job_status(root, assistant_match[1]))
                change_match = re.fullmatch(r'/api/v3/assistant/changes/([a-f0-9]{32})', parsed.path)
                if change_match: return self._json(200, assistant_actions.get(root, change_match[1]))
                if parsed.path == '/api/v3/product-audit':
                    return self._json(200, {'ok':True,'job':product_audit.status(root,parse_qs(parsed.query).get('name',[''])[0])})
                if parsed.path == "/api/v3/operations": return self._json(200, operations.snapshot(root))
                if parsed.path == "/api/v3/business-brief": return self._json(200, {"ok": True, **business_brief.build(draft_rows(root))})
                if parsed.path == "/api/v3/overview": return self._json(200, overview(root))
                if parsed.path == "/api/v3/products": return self._json(200, {"ok": True, "items": draft_rows(root, parse_qs(parsed.query).get("publish_store", [""])[0])})
                if parsed.path == "/api/v3/publish/stores": return self._json(200, {"ok":True, "stores":[publishing.public_store(r) for r in operations.registry(root) if r.get("auth_type") == "CBT"]})
                preview_match = re.fullmatch(r"/api/drafts/([^/]+)/publish-preview", parsed.path)
                if preview_match:
                    name = safe_name(unquote(preview_match[1]))
                    draft = legacy.read_json(root / "drafts" / name)
                    preview = legacy.build_publish_preview(draft)
                    row = next(r for r in draft_rows(root) if r["name"] == name)
                    preview["preflight_issues"] = list(dict.fromkeys(
                        (row.get("errors") or []) + row["publish_issues"] + row["evidence_issues"]))
                    preview["valid"] = preview["valid"] and not preview["preflight_issues"]
                    return self._json(200, {"ok": True, "preview": preview})
                if parsed.path == "/api/v3/templates": return self._json(200, {"ok": True, "items": templates(root)})
                if parsed.path == "/api/v3/generation": return self._json(200, {"ok": True, "strategy": load_generation(root)})
                if parsed.path == "/api/v3/export.xlsx":
                    rows = draft_rows(root)
                    if parse_qs(parsed.query).get("scope") == ["pending"]:
                        rows = [r for r in rows if r.get("in_publish_center") and r.get("publish_status") in {"pending", "failed"}]
                    body = export_xlsx(rows)
                    self.send_response(200); self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"); self.send_header("Content-Disposition", 'attachment; filename="youyou-products.xlsx"'); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                    return
                match = re.fullmatch(r"/api/v3/products/([^/]+)/evidence", parsed.path)
                if match:
                    draft = legacy.read_json(root / "drafts" / safe_name(unquote(match[1])))
                    return self._json(200, {"ok": True, "product": product_from_draft(draft), "issues": review_issues(draft)})
            except (ValueError, OSError) as exc:
                return self._json(400, {"ok": False, "error": str(exc)[:300]})
            return super().do_GET()

        def do_POST(self):
            parsed = urlparse(self.path)
            if supplier1688_routes.post(self,root,parsed.path):return
            if parsed.path.startswith("/api/v3/selection"):
                return self._json(410, {"ok":False,"error":"AI选品模块已退出当前项目；选品知识可询问工作助手。"})
            # Publishing requires a later, explicit target-bound integration; never silently
            # inherit v0.2's global active-store behavior in this new version.
            if parsed.path == "/api/publish/batch" or re.fullmatch(r"/api/drafts/[^/]+/(publish|publish-retry|publish-refresh)", parsed.path):
                return self._json(409, {"ok": False, "error": "旧发布入口未绑定店铺，已停用；请在发布中心选择店铺并预检确认，本次未提交"})
            if not parsed.path.startswith("/api/v3/"):
                return super().do_POST()
            try:
                origin = urlparse(str(self.headers.get("Origin") or ""))
                collector_origin = parsed.path in {"/api/v3/selection/discovery/claim", "/api/v3/selection/discovery/receive"} and origin.scheme == "chrome-extension" and re.fullmatch("[a-p]{32}", origin.netloc)
                if origin.netloc and origin.netloc != self.headers.get("Host") and not collector_origin:
                    raise ValueError("只接受当前工作台页面的请求")
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 30_000_000: raise ValueError("请求大小不正确")
                value = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(value, dict): raise ValueError("请求需为对象")
                if parsed.path == '/api/v3/input-requests/import':
                    from .input_requests_v3 import import_table
                    return self._json(200, import_table(root, value, legacy))
                if parsed.path == '/api/v3/selection/discovery/create': return self._json(200, discovery.create(root,value))
                if parsed.path == '/api/v3/selection/discovery/claim': return self._json(200, discovery.claim(root,value))
                if parsed.path == '/api/v3/selection/discovery/receive': return self._json(200, discovery.receive(root,value))
                if parsed.path == '/api/v3/selection/discovery/stop': return self._json(200, discovery.stop(root,value))
                if parsed.path == '/api/v3/selection/research/create': return self._json(200, research.create(root,value))
                if parsed.path == '/api/v3/selection/research/update': return self._json(200, research.update(root,value))
                if parsed.path == '/api/v3/selection/research/investigate': return self._json(200, research.investigate(root,value))
                if parsed.path == '/api/v3/selection/run': return self._json(200, selection.run(root,value))
                if parsed.path == '/api/v3/selection/analyze': return self._json(200, selection.analyze(root,value))
                if parsed.path == "/api/v3/pricing/calculate": return self._json(200, pricing.calculate(value))
                if parsed.path == "/api/v3/pricing/compare": return self._json(200, pricing.compare(root,value))
                if parsed.path == "/api/v3/pricing/plan": return self._json(200, autopricing.prepare(root,value))
                if parsed.path == "/api/v3/pricing/rate": return self._json(200, pricing.rate_quote(value))
                if parsed.path == "/api/v3/publish/preview":
                    return self._json(200, publishing.prepare(root,value.get("name"),value.get("store_id")))
                if parsed.path == "/api/v3/publish/commit":
                    return self._json(200, publishing.commit(root,value.get("plan_id"),value.get("confirmed"),value.get("budget_review_confirmed")))
                if parsed.path == "/api/v3/publish/refresh":
                    return self._json(200, publishing.refresh(root,value.get("name"),value.get("store_id")))
                if parsed.path == "/api/v3/publish/auto-refresh":
                    return self._json(200, publishing.auto_refresh(root,value.get("name"),value.get("store_id")))
                if parsed.path == '/api/v3/desktop/shutdown':
                    if value.get('confirmed') is not True:raise ValueError('关闭后台需明确确认')
                    if self.headers.get('Host') not in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}:raise ValueError('仅允许本机工作台关闭')
                    if not hasattr(self.server,'request_workbench_stop'):raise ValueError('此实例不支持桌面退出')
                    self._json(200,{'ok':True,'state':'STOPPING'})
                    self.server.request_workbench_stop()
                    return
                if parsed.path == '/api/v3/assistant/ask':
                    return self._json(200, assistant.ask(root, value))
                if parsed.path == '/api/v3/operations/query':
                    kind = str(value.get('kind') or '')
                    if kind not in operations.KINDS: raise ValueError('查询栏目无效')
                    return self._json(200, {'ok':True,'snapshot':operations.refresh(root,str(value.get('store_id') or ''),kind,value.get('options') or {})})
                if parsed.path == '/api/v3/operations/discount-preview':
                    return self._json(200, operations.discount_preview(root,value))
                if parsed.path == '/api/v3/operations/shipment':
                    return self._json(200, operations.shipment(root,value))
                with LOCK:
                    change_match = re.fullmatch(r'/api/v3/assistant/changes/([a-f0-9]{32})/(apply|cancel|undo)', parsed.path)
                    if parsed.path == "/api/v3/pricing/apply": result = autopricing.apply(root,value.get("plan_id"),value.get("confirmed"))
                    elif parsed.path == "/api/v3/pricing/undo": result = autopricing.undo(root,value.get("plan_id"),value.get("confirmed"))
                    elif change_match: result = assistant_actions.act(root,change_match[1],change_match[2],value)
                    elif parsed.path == '/api/v3/product-audit/start': result = product_audit.start(root,value.get('name'),value.get('confirmed'))
                    elif parsed.path == '/api/v3/product-audit/save-candidate': result = product_audit.save_candidate(root,value.get('name'))
                    elif parsed.path == "/api/v3/create": result = create_product(root, value)
                    elif parsed.path == "/api/v3/import": result = import_csv(root, value.get("csv"))
                    elif parsed.path == "/api/v3/templates": result = {"ok": True, "template": save_template(root, value)}
                    elif parsed.path == "/api/v3/generation":
                        settings = validate_generation(value)
                        write(root / "data" / "v3" / "generation.json", settings)
                        audit(root, "保存生图策略", settings)
                        result = {"ok": True, "strategy": settings}
                    elif parsed.path == "/api/v3/batch-ai":
                        if value.get("confirmed") is not True: raise ValueError("需确认开始本地 AI 处理；付费生图另行确认")
                        names = value.get("names") or []
                        if not isinstance(names, list) or not 1 <= len(names) <= 50: raise ValueError("需选择1–50件商品")
                        names = list(dict.fromkeys(safe_name(n) for n in names))
                        results = []
                        for name in names:
                            try: results.append({"name": name, "status": "accepted", "job": legacy.start_full_ai_flow(name, root / "drafts", root)})
                            except Exception as exc: results.append({"name": name, "status": "failed", "error": str(exc)[:300]})
                        audit(root, "批量AI提交", {"results": [{"name": r["name"], "status": r["status"]} for r in results]})
                        result = {"ok": True, "results": results, "publishing_performed": False}
                    elif parsed.path == "/api/v3/apply-template":
                        name = safe_name(value.get("name"))
                        template = next((t for t in templates(root) if t["id"] == value.get("template_id")), None)
                        if template is None: raise ValueError("模板未找到")
                        path = root / "drafts" / name
                        draft = legacy.read_json(path)
                        write(root / "draft_versions" / f"{path.stem}-before-template-{uuid.uuid4().hex}.json", draft)
                        draft.setdefault("evidence", {})["prompt_template"] = {"id": template["id"], "version": template["version"], "content": template["content"], "status": "BOUND_FOR_NEXT_AI_REQUEST"}
                        write(path, draft)
                        result = {"ok": True, "status": "模板版本已绑定；下次文案请求携带该版本，本次未调用模型"}
                    elif parsed.path == "/api/v3/target-preview":
                        name = safe_name(value.get("name")); platform = str(value.get("platform") or "mercadolibre")
                        draft = legacy.read_json(root / "drafts" / name); product = product_from_draft(draft)
                        target = {"platform": platform, "store_id": str(value.get("store_id") or ""), "market": str(value.get("market") or "")}
                        result = {"ok": True, "identity": target_identity(product, **target), "target": target, "facts_version": product["facts_version"], "issues": review_issues(draft), "publishing_performed": False}
                    else: raise ValueError("未找到功能")
                return self._json(200, result)
            except (ValueError, OSError, legacy.EditorError) as exc:
                return self._json(400, {"ok": False, "error": str(exc)[:500]})
    return Handler


class WorkbenchServer(ThreadingHTTPServer):
    """One backend owns the local port, including on Windows."""
    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        return super().server_bind()


def reconcile_pending_publishes(root, stopped):
    """Keep read-only publication checks running outside the publish page."""
    while not stopped.wait(10):
        try:
            publishing.sweep_pending(root)
        except Exception as exc:
            print(f"发布回查后台错误：{type(exc).__name__}", flush=True)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8790); args = parser.parse_args()
    install_guards()
    server = WorkbenchServer(("127.0.0.1", args.port), make_handler())
    from .workbench_lifecycle import bind
    stopped = bind(server,ROOT)
    from .auto_processing_v3 import start_worker
    migration_pending = (ROOT/'data/MIGRATION_REVIEW_REQUIRED.json').exists()
    if not migration_pending: start_worker(ROOT, legacy, stopped)
    from .autonomous_runtime_v3 import start as start_autonomous
    if not migration_pending: start_autonomous(ROOT, legacy, stopped)
    collector = None
    from . import rag_runtime_v3
    rag_process=rag_runtime_v3.start(ROOT)
    try:
        collector, server.collector_status = ensure_receiver(ROOT)
        threading.Thread(target=reconcile_pending_publishes, args=(ROOT, stopped),
                         name="publish-readback", daemon=True).start()
        print(json.dumps({"ok": True, "version": VERSION, "url": f"http://127.0.0.1:{args.port}", "collector": server.collector_status, "publishing_performed": False}), flush=True)
        server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        stopped.set()
        rag_runtime_v3.stop(rag_process)
        server.server_close()
        if collector is not None:
            collector.shutdown()
            collector.server_close()


if __name__ == "__main__": main()
