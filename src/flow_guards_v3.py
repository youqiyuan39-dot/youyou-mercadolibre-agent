"""Runtime guards for v0.3 only; v0.2 modules on disk remain unchanged."""
from __future__ import annotations
import copy
import base64
import json
import mimetypes

from .product_core_v3 import clean_description, product_from_draft, review_issues


def selected_context(draft, base_context):
    context = copy.deepcopy(base_context)
    evidence = draft.get("evidence") or {}
    selected = product_from_draft(draft)["facts"]["selected_sku"]
    context['packaging_evidence'] = copy.deepcopy(product_from_draft(draft)['facts']['packaging'])
    context['packaging_conflict'] = copy.deepcopy(evidence.get('packaging_conflict'))
    context['evidence_rule'] = 'Field presence or a human lock does not establish verification. Packaging is not product or hose size.'
    context["supplier_description"] = clean_description(context.get("supplier_description") or "")
    if selected:
        context["selected_variant"] = selected.get("variant") or selected.get("name")
        context["selected_sku"] = copy.deepcopy(selected)
        context["supplier_skus"] = [copy.deepcopy(selected)]
        context["sku_rule"] = "Only describe the selected SKU. Shared photos and other variants do not establish its capacity, material, weight or dimensions. Leave conflicting facts unresolved."
    template = evidence.get("prompt_template") or {}
    if template.get("content"):
        context["editorial_template"] = {"id": template.get("id"), "version": template.get("version"), "content": template["content"], "boundary": "Editorial preferences only. Cannot supply product facts, override selected SKU, invent identifiers or change publication rules."}
    return context


def freeze_selected(draft, prepare, error_type=ValueError):
    before = copy.deepcopy(draft)
    evidence = draft.get("evidence") or {}
    skus = evidence.get("sku_details") or []
    selected_id = str(evidence.get("current_pricing_sku_id") or "")
    if len(skus) > 1 and not selected_id:
        raise error_type("多 SKU 商品必须明确选择当前规格，不能默认使用第一个")
    selected = next((s for s in skus if str(s.get("sku_id")) == selected_id), None)
    if selected_id and selected is None:
        raise error_type("当前 SKU 不在采集规格中，需重新选择")
    result = prepare(draft)
    locks = (before.get("edit_metadata") or {}).get("field_locks") or {}
    for lock, field in (("weight", "packed_weight"), ("dimensions", "packed_dimensions")):
        if locks.get(lock):
            if field in evidence: draft["evidence"][field] = copy.deepcopy(before["evidence"][field])
            else: draft["evidence"].pop(field, None)
    if selected:
        # Zero inventory is a real value. Unknown inventory stays unknown.
        draft.setdefault("payload", {})["available_quantity"] = selected.get("available_quantity")
        supplier = draft.setdefault("evidence", {}).setdefault("supplier_page", {})
        supplier["selected_variant"] = selected.get("variant") or selected.get("name")
    return result


def install(legacy, pipeline):
    if getattr(legacy, "_v3_guards_installed", False): return
    legacy._v3_guards_installed = True
    from .prompt_policy_v3 import LISTING_INSTRUCTIONS, ATTRIBUTE_INSTRUCTIONS
    pipeline.LISTING_INSTRUCTIONS = LISTING_INSTRUCTIONS
    original_preflight = legacy.publish_preflight_issues
    legacy.publish_preflight_issues = lambda d: list(dict.fromkeys(original_preflight(d) + review_issues(d)))
    original_list = getattr(legacy, 'list_drafts', None)
    if original_list:
        def list_drafts(drafts_dir=legacy.DRAFTS_DIR, root=legacy.ROOT):
            rows = original_list(drafts_dir, root)
            for row in rows:
                issues = review_issues(legacy.read_json(drafts_dir / row['name']))
                row['review_ready'] = bool(row.get('review_ready')) and not issues
                row['publish_ready'] = bool(row.get('publish_ready')) and not issues
                row['publish_issues'] = list(dict.fromkeys((row.get('publish_issues') or []) + issues))
            return rows
        legacy.list_drafts = list_drafts
    original_context = pipeline.sanitized_product_context
    pipeline.sanitized_product_context = lambda d: selected_context(d, original_context(d))
    original_input_images = pipeline._input_images
    def input_images(draft, limit=4):
        review = ((draft.get("evidence") or {}).get("image_review") or {})
        selected = product_from_draft(draft)["facts"]["selected_sku"]
        urls = []
        pools = [review.get("source_gallery") or []]
        if not selected: pools.append(review.get("detail_gallery") or [])
        for pool in pools:
            for row in pool:
                if not isinstance(row, dict): continue
                url = str(row.get("remote_url") or "")
                local = str(row.get("local_file") or "").removeprefix("assets/")
                if not url and local:
                    path = (legacy.ROOT / "assets" / local).resolve()
                    if (legacy.ROOT / "assets").resolve() in path.parents and path.is_file() and path.stat().st_size <= 10_000_000:
                        mime = mimetypes.guess_type(path.name)[0]
                        if mime in {"image/jpeg", "image/png", "image/webp"}:
                            url = "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")
                if url.startswith(("https://", "data:image/")) and url not in urls: urls.append(url)
                if len(urls) >= limit: return urls
        return urls
    pipeline._input_images = input_images
    original_prepare = legacy._prepare_multi_sku_ai_workflow
    legacy._prepare_multi_sku_ai_workflow = lambda d: freeze_selected(d, original_prepare, legacy.EditorError)
    def inventory(draft):
        payload = draft.setdefault("payload", {})
        if payload.get("available_quantity") is not None: return
        selected = product_from_draft(draft)["facts"]["selected_sku"]
        if selected and selected.get("available_quantity") is not None:
            payload["available_quantity"] = selected["available_quantity"]
        # Do not sum other variants or fabricate stock when evidence is absent.
    legacy.apply_inventory_rule = inventory
    original_generate = legacy.generate_quality_checked_proposal
    def generate(config, draft, model):
        safe = copy.deepcopy(draft)
        if any("当前 SKU 不在" in i for i in review_issues(safe)):
            raise legacy.EditorError("当前 SKU 不在采集规格中，不能调用模型")
        selected = product_from_draft(safe)["facts"]["selected_sku"]
        if selected:
            review = safe.setdefault("evidence", {}).setdefault("image_review", {})
            group = next((g for g in review.get("sku_source_galleries") or [] if str(g.get("sku_id")) == str(selected.get("sku_id"))), None)
            if group:
                review["source_gallery"] = copy.deepcopy(group.get("gallery") or [])
                review["gallery"] = copy.deepcopy(group.get("gallery") or [])
                review["detail_gallery"] = []
            elif len(product_from_draft(safe)["facts"]["skus"]) > 1:
                raise legacy.EditorError("当前 SKU 未绑定专属原图；不能把公共 500ml 图片当作 250ml 证据")
        result = original_generate(config, safe, model)
        conflicts = [i for i in review_issues(safe, result[0]) if "容量" in i]
        if conflicts: raise legacy.EditorError("AI 文案未通过 SKU 一致性校验：" + "；".join(conflicts))
        return result
    legacy.generate_quality_checked_proposal = generate
    original_call = legacy.call_responses_api
    def call(config, request):
        schema_name = (((request.get("text") or {}).get("format") or {}).get("name"))
        if schema_name == "category_attributes":
            request = copy.deepcopy(request)
            request['instructions'] = ATTRIBUTE_INSTRUCTIONS
        response = original_call(config, request)
        if schema_name != "category_attributes": return response
        context = json.loads(request["input"])
        protected = {"GTIN", "EAN", "UPC"}
        existing = {r["id"]: r for r in context.get("existing_attributes") or [] if r.get("id") in protected}
        result = json.loads(legacy._output_text(response))
        rejected = [r["id"] for r in result.get("attributes") or [] if r.get("id") in protected and (r.get("value_name") != (existing.get(r["id"]) or {}).get("value_name") or r.get("id") not in existing)]
        result["attributes"] = [r for r in result.get("attributes") or [] if r.get("id") not in rejected]
        result["unresolved"] = list(dict.fromkeys((result.get("unresolved") or []) + rejected))
        safe_response = copy.deepcopy(response)
        safe_response["output"] = [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result, ensure_ascii=False)}]}]
        return safe_response
    legacy.call_responses_api = call
    original_images = legacy.start_sku_image_generation
    def sku_images(name, drafts_dir=legacy.DRAFTS_DIR, root=legacy.ROOT):
        draft = legacy.read_json(drafts_dir / name)
        evidence = draft.get("evidence") or {}
        skus = evidence.get("sku_details") or (evidence.get("supplier_page") or {}).get("skus") or []
        groups = ((evidence.get("image_review") or {}).get("sku_source_galleries") or [])
        bound = {str(g.get("sku_id")) for g in groups if g.get("gallery")}
        missing = [str(s.get("sku_id")) for s in skus if str(s.get("sku_id")) not in bound]
        if missing: raise legacy.EditorError("缺少 SKU 专属原图，不能回退为公共图片：" + ", ".join(missing))
        return original_images(name, drafts_dir, root)
    legacy.start_sku_image_generation = sku_images
    from .generation_strategy_v3 import load
    from .prompt_policy_v3 import build_image_prompts
    original_prompts = getattr(legacy, "build_image_prompts", None)
    if original_prompts:
        def build_prompts(draft, language="none"):
            return build_image_prompts(draft, language, load(legacy.ROOT))
        legacy.build_image_prompts = build_prompts
        original_get = getattr(legacy, 'get_draft', None)
        if original_get:
            def get_draft(name, drafts_dir=legacy.DRAFTS_DIR, root=legacy.ROOT):
                result = original_get(name, drafts_dir, root)
                result['image_prompts'] = build_prompts(legacy.read_json(drafts_dir / name), 'none')
                return result
            legacy.get_draft = get_draft
