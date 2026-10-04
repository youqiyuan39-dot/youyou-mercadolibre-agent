"""Additive workbench: keeps v0.2 source/data, serves the v0.3 UI separately."""
from __future__ import annotations

import argparse
import base64
import copy
import csv
import io
import json
import re
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

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.3.0-evidence"
LOCK = threading.RLock()
PROCESSING = {"BLOCKED_EVIDENCE", "CAPTURE_READY", "READY_FOR_AI", "AI_PROCESSING", "AI_PAUSED", "AI_FAILED", "READ_ERROR"}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


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


def draft_rows(root):
    rows = legacy.list_drafts(root / "drafts", root)
    for row in rows:
        draft = legacy.read_json(root / "drafts" / row["name"])
        row["evidence_issues"] = review_issues(draft)
        row["review_ready"] = row.get("review_ready") and not row["evidence_issues"]
        row["publish_ready"] = row.get("publish_ready") and not row["evidence_issues"]
        row["source"] = urlparse(row.get("source_url") or "").hostname or "人工建品"
    return rows


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
    return {"ok": True, "version": VERSION, "total": len(rows), "processing": sum(r["stage"] in PROCESSING for r in rows), "review": counts.get("READY_FOR_HUMAN_REVIEW", 0), "published": counts.get("PUBLISHED", 0), "partial": counts.get("PUBLISH_PARTIAL", 0), "counts": counts, "sources": sources, "issues": issues, "events": events}


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
            sku_details.append({"sku_id": f"{product_id}-{n+1}", "variant": sku["variant"], "seller_sku": sku.get("seller_sku") or f"MANUAL-{product_id[:8]}-{n+1}", "available_quantity": sku.get("available_quantity"), "purchase_cost_cny": sku.get("purchase_cost_cny"), "package": sku.get("package") or {}, "image_indices": indices})
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
    payload = {"title": title, "family_name": title, "description": str(values.get("description") or ""), "category_id": "", "available_quantity": values.get("available_quantity"), "currency_id": "USD", "attributes": [], "sites_to_sell": [], "pictures": [], "catalog_listing": False}
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


def make_handler(root=ROOT):
    base = legacy.make_handler(root)
    class Handler(base):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/":
                return self._file(root / "web" / "workbench_v3.html", "text/html; charset=utf-8")
            if parsed.path in {"/workbench_v3.js", "/workbench_v3.css", "/operations_v3.js"}:
                return self._file(root / "web" / parsed.path.lstrip("/"), "text/javascript; charset=utf-8" if parsed.path.endswith(".js") else "text/css; charset=utf-8")
            if parsed.path.startswith("/legacy/"):
                self.path = self.path.removeprefix("/legacy")
                if parsed.path == "/legacy/dashboard": self.path = "/"
                return super().do_GET()
            if parsed.path == "/api/health":
                return self._json(200, {"ok": True, "version": VERSION, "operations_version":"2026-10-03", "workspace": str(root), "mode": "LOCAL_REVIEW", "real_publish_enabled": False})
            try:
                if parsed.path == "/api/v3/operations": return self._json(200, operations.snapshot(root))
                if parsed.path == "/api/v3/overview": return self._json(200, overview(root))
                if parsed.path == "/api/v3/products": return self._json(200, {"ok": True, "items": draft_rows(root)})
                if parsed.path == "/api/v3/templates": return self._json(200, {"ok": True, "items": templates(root)})
                if parsed.path == "/api/v3/generation": return self._json(200, {"ok": True, "strategy": load_generation(root)})
                if parsed.path == "/api/v3/export.xlsx":
                    rows = draft_rows(root)
                    if parse_qs(parsed.query).get("scope") == ["pending"]:
                        rows = [r for r in rows if r["stage"] == "READY_TO_PUBLISH"]
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
            # Publishing requires a later, explicit target-bound integration; never silently
            # inherit v0.2's global active-store behavior in this new version.
            if parsed.path == "/api/publish/batch" or re.fullmatch(r"/api/drafts/[^/]+/(publish|publish-retry)", parsed.path):
                return self._json(409, {"ok": False, "error": "新版本正式发布尚未验收：先核对商品证据及绑定目标店铺；本次未提交"})
            if not parsed.path.startswith("/api/v3/"):
                return super().do_POST()
            try:
                origin = urlparse(str(self.headers.get("Origin") or ""))
                if origin.netloc and origin.netloc != self.headers.get("Host"):
                    raise ValueError("只接受当前工作台页面的请求")
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 30_000_000: raise ValueError("请求大小不正确")
                value = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(value, dict): raise ValueError("请求需为对象")
                if parsed.path == '/api/v3/operations/query':
                    kind = str(value.get('kind') or '')
                    if kind not in operations.KINDS: raise ValueError('查询栏目无效')
                    return self._json(200, {'ok':True,'snapshot':operations.refresh(root,str(value.get('store_id') or ''),kind,value.get('options') or {})})
                if parsed.path == '/api/v3/operations/discount-preview':
                    return self._json(200, operations.discount_preview(root,value))
                if parsed.path == '/api/v3/operations/shipment':
                    return self._json(200, operations.shipment(root,value))
                with LOCK:
                    if parsed.path == "/api/v3/create": result = create_product(root, value)
                    elif parsed.path == "/api/v3/import": result = import_csv(root, value.get("csv"))
                    elif parsed.path == "/api/v3/templates": result = {"ok": True, "template": save_template(root, value)}
                    elif parsed.path == "/api/v3/generation":
                        settings = validate_generation(value)
                        write(root / "data" / "v3" / "generation.json", settings)
                        audit(root, "保存生图策略", settings)
                        result = {"ok": True, "strategy": settings}
                    elif parsed.path == "/api/v3/batch-ai":
                        if value.get("confirmed") is not True: raise ValueError("需明确确认调用付费模型")
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


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8790); args = parser.parse_args()
    install_guards()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler())
    print(json.dumps({"ok": True, "version": VERSION, "url": f"http://127.0.0.1:{args.port}", "publishing_performed": False}), flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
