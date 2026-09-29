"""悠悠商品图生成队列。

密钥只从环境变量读取，任务文件和页面日志都不保存密钥。
支持 OpenAI 兼容的 images/edits 同步响应，以及返回 task_id/id 的异步响应。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen


class ImageGenerationError(RuntimeError):
    pass


LANGUAGE_COPY = {
    "none": {"label": "无文字", "details": "", "included": ""},
    "es": {"label": "西班牙语", "details": "Detalles del producto", "included": "Contenido del paquete", "accessories": "Producto y accesorios"},
    "pt": {"label": "葡萄牙语", "details": "Detalhes do produto", "included": "Conteúdo da embalagem", "accessories": "Produto e acessórios"},
}


def resolve_market_language(draft: dict[str, Any], preferred: str = "auto") -> str:
    """Resolve marketing-image language while keeping the main image text free."""
    if preferred in {"none", "es", "pt"}:
        return preferred
    sites = {
        str(site.get("site_id") if isinstance(site, dict) else site).upper()
        for site in ((draft.get("payload") or {}).get("sites_to_sell") or [])
    }
    if sites == {"MLB"}:
        return "pt"
    if "MLB" in sites and len(sites) > 1:
        return "none"
    return "es"


def build_image_prompts(draft: dict[str, Any], language: str = "none") -> list[dict[str, str]]:
    """生成三张图的可编辑提示词：白底主图、场景卖点图、配件说明图。"""
    payload = draft.get("payload") or {}
    evidence = draft.get("evidence") or {}
    title = str(payload.get("title") or payload.get("family_name") or "商品").strip()
    model = next((str(row.get("value_name") or "") for row in payload.get("attributes") or [] if row.get("id") == "MODEL"), "")
    compatibility = str(evidence.get("compatibility_evidence") or model or "").strip()
    copy = LANGUAGE_COPY.get(language, LANGUAGE_COPY["none"])
    language_rule = (
        "画面内不得出现任何文字、字母、数字、品牌标识或水印。"
        if language == "none"
        else f"画面文字只允许使用{copy['label']}，不得出现中文、英文或其他语言；只允许使用已经给定的商品标题以及栏目词“{copy['details']}”“{copy['included']}”“{copy['accessories']}”，不要生成新的性能、兼容或数量文案。文字必须拼写正确、短而清晰。"
    )
    common = (
        f"任务：制作 Mercado Libre 商品图。以输入图片为唯一视觉事实，商品识别参考名：{title}。"
        f"{f'已确认兼容信息：{compatibility}。' if compatibility else ''}"
        "先逐件核对输入图中的主体和配件，再构图。必须保持外形、材质、颜色、接口、孔位、软管走向、配件种类、数量与比例；看不清的结构保持原样，不补画。"
        "不得替换产品，不得合并不同 SKU，不得增加工具或配件，不得虚构品牌、认证、性能、适配型号、尺寸或促销信息。"
        "输出 1:1 正方形，商业摄影级清晰度，完整展示商品，边缘锐利，光线和透视一致。"
        "失败排除：主体变形、部件漂浮、重复配件、缺件、接口或孔位变化、比例错误、伪文字、乱码、水印、价格、平台标志。"
    )
    return [
        {"kind": "main", "label": "白底主图", "language": "none", "prompt": common + "构图：纯白 RGB(255,255,255) 背景，柔和棚拍光，极轻自然落影；完整套装居中，占画面约 82%，每件配件清楚可数，四周留安全边距。禁止文字、字母、数字、图标、边框和水印。"},
        {"kind": "scene", "label": "场景广告图", "language": language, "prompt": common + f"构图：把完整商品和全部真实配件陈列在整洁明亮的维修工作台，背景只作轻度虚化，不展示无法证明的安装效果。主体占画面约 65%，上方或右上方保留约 30% 的规则文案区。若允许文字，以准确商品标题作为大标题，以“{copy.get('included','')}”作为唯一小标题，配三个只表达套装、维护、配件的简洁图形符号；使用橙黑或蓝白高对比电商广告版式，文字不得压住商品。必须与白底主图使用不同机位。" + language_rule},
        {"kind": "infographic", "label": "配件结构信息图", "language": language, "prompt": common + f"构图必须与白底主图和场景图明显不同，不得复用同一机位或陈列方式。采用高完成度三分区电商信息图：左侧占 55% 展示完整商品正面，右侧纵向排列两个真实细节放大框，底部用独立横条逐件展示输入图中真实存在的配件；使用细引导线、圆角边框和统一强调色。若允许文字，只使用准确商品标题、“{copy.get('details','')}”和“{copy.get('included','')}”作为标题，不写未经证实的性能结论、型号、数量或适配承诺。所有分区只能展示输入图中确实存在的部件。" + language_rule},
    ]


def build_sku_image_prompts(draft: dict[str, Any]) -> list[dict[str, str]]:
    """Build one factual, text-free hero image prompt for every collected SKU."""
    evidence = draft.get("evidence") or {}
    supplier = evidence.get("supplier_page") or {}
    sku_rows = evidence.get("sku_details") or supplier.get("skus") or []
    prompts: list[dict[str, str]] = []
    for index, sku in enumerate(sku_rows[:100]):
        if not isinstance(sku, dict):
            continue
        variant = str(sku.get("variant") or "").strip()
        sku_id = str(sku.get("sku_id") or "").strip()
        if not variant or not sku_id:
            continue
        prompt = (
            f"为同一商品的单独规格制作专属白底首图。当前唯一目标规格：{variant}；来源 SKU：{sku_id}。"
            "先从输入参考图中找到与该规格名称、部件组合和数量相符的实物；只保留该规格真实包含的部件。"
            "若参考图无法明确证明某个部件或数量，保持可见事实，不得从其他规格借用、拼接或猜测。"
            "纯白 RGB(255,255,255) 背景，完整套装居中，占画面约 82%，所有部件分离清楚、可数、无遮挡，柔和棚拍光和极轻自然落影。"
            "1:1 正方形，高清电商摄影。禁止文字、数字标签、品牌、logo、水印、边框、场景道具和促销元素。"
            "失败排除：混入其他 SKU、错误套装数量、重复或缺少配件、部件变形、接口变化、颜色变化、伪文字、乱码。"
        )
        prompts.append({"kind": f"sku_{index + 1}", "label": f"{variant} · 专属 AI 图", "language": "none", "sku_id": sku_id, "variant": variant, "prompt": prompt})
    return prompts


def load_config(root: Path) -> dict[str, Any]:
    path = root / "config" / "model_gateway.images.json"
    if not path.is_file():
        raise ImageGenerationError("未配置悠悠生图网关，请先复制 model_gateway.images.example.json。")
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in ("endpoint", "model", "api_key_env"):
        if not str(config.get(key) or "").strip():
            raise ImageGenerationError(f"生图配置缺少 {key}。")
    try:
        from .model_settings import get_runtime_role
    except ImportError:
        from model_settings import get_runtime_role
    runtime = get_runtime_role(root, "images")
    config["endpoint"] = runtime["base_url"]
    config["model"] = runtime["model"]
    config["_api_key"] = runtime["api_key"]
    return config


def _multipart(fields: dict[str, str], images: list[Path]) -> tuple[bytes, str]:
    boundary = f"----Youyou{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.extend([f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), value.encode("utf-8"), b"\r\n"])
    for path in images:
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        parts.extend([f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="image"; filename="{path.name}"\r\n'.encode(), f"Content-Type: {mime}\r\n\r\n".encode(), path.read_bytes(), b"\r\n"])
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def _json_request(url: str, key: str) -> dict[str, Any]:
    request = Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_image(value: Any) -> tuple[bytes | None, str | None]:
    if isinstance(value, dict):
        for key in ("b64_json", "base64", "image_base64"):
            if value.get(key):
                return base64.b64decode(value[key]), None
        for key in ("url", "image_url", "output_url"):
            if value.get(key):
                return None, str(value[key])
        for key in ("data", "images", "output", "result"):
            body, url = _extract_image(value.get(key))
            if body or url:
                return body, url
    elif isinstance(value, list):
        for item in value:
            body, url = _extract_image(item)
            if body or url:
                return body, url
    return None, None


def call_image_api(config: dict[str, Any], prompt: str, images: list[Path]) -> tuple[bytes, dict[str, Any]]:
    env_name = str(config["api_key_env"])
    key = str(config.get("_api_key") or os.environ.get(env_name, "")).strip()
    if not key:
        raise ImageGenerationError(f"环境变量 {env_name} 未设置，本次没有调用生图 API。")
    endpoint = str(config["endpoint"]).strip()
    body, boundary = _multipart({"model": str(config["model"]), "prompt": prompt, "size": str(config.get("size") or "1024x1024")}, images)
    request = Request(endpoint, data=body, method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36"})
    with urlopen(request, timeout=int(config.get("submit_timeout_seconds") or 90)) as response:
        result = json.loads(response.read().decode("utf-8"))
    image_body, image_url = _extract_image(result)
    task_id = str(result.get("task_id") or result.get("id") or (result.get("data") or {}).get("task_id") or "") if isinstance(result, dict) else ""
    if not image_body and not image_url and task_id:
        poll_url = str(result.get("poll_url") or config.get("poll_endpoint_template") or "").replace("{task_id}", task_id)
        if not poll_url:
            poll_url = f"{endpoint.rstrip('/')}/{task_id}"
        poll_url = urljoin(endpoint, poll_url)
        deadline = time.time() + int(config.get("poll_timeout_seconds") or 240)
        while time.time() < deadline:
            time.sleep(float(config.get("poll_interval_seconds") or 3))
            result = _json_request(poll_url, key)
            image_body, image_url = _extract_image(result)
            if image_body or image_url:
                break
            if str(result.get("status") or "").lower() in {"failed", "error", "cancelled"}:
                raise ImageGenerationError(str(result.get("error") or result.get("message") or "生图任务失败"))
    if image_url and not image_body:
        with urlopen(Request(image_url, headers={"Accept": "image/*", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36"}), timeout=60) as response:
            image_body = response.read(20_000_001)
    if not image_body or len(image_body) > 20_000_000:
        raise ImageGenerationError("生图服务未返回可用图片。")
    return image_body, {"task_id": task_id or None, "model": str(config["model"]), "endpoint": endpoint}


def _persist_sku_galleries(root: Path, draft_name: str, job: dict[str, Any]) -> None:
    """Compose each SKU gallery from its hero, collected SKU gallery, then shared images."""
    draft_path = root / "drafts" / Path(draft_name).name
    if not draft_path.is_file():
        raise ImageGenerationError("SKU 图片已生成，但没有找到对应草稿。")
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    review = draft.setdefault("evidence", {}).setdefault("image_review", {})
    shared = [dict(row) for row in (review.get("gallery") or []) if isinstance(row, dict)][:7]
    source_by_sku: dict[str, list[dict[str, Any]]] = {}
    source_by_variant: dict[str, list[dict[str, Any]]] = {}
    for group in review.get("sku_source_galleries") or []:
        if not isinstance(group, dict):
            continue
        rows = [dict(row) for row in group.get("gallery") or [] if isinstance(row, dict)]
        if group.get("sku_id"):
            source_by_sku[str(group["sku_id"])] = rows
        if group.get("variant"):
            source_by_variant[str(group["variant"])] = rows
    generated: list[dict[str, Any]] = []
    galleries: list[dict[str, Any]] = []
    for item in job.get("items") or []:
        if item.get("status") != "done" or not item.get("local_file"):
            continue
        hero = {
            "role": "main",
            "local_file": str(item["local_file"]).removeprefix("assets/"),
            "source": "sku_ai_generated",
            "review_status": "UNREVIEWED",
            "sku_id": str(item.get("sku_id") or ""),
            "variant": str(item.get("variant") or ""),
        }
        generated.append(dict(hero))
        final_gallery = [dict(hero)]
        seen = {hero["local_file"]}
        own_source = source_by_sku.get(hero["sku_id"]) or source_by_variant.get(hero["variant"]) or []
        for row in [*own_source, *shared]:
            key = str(row.get("local_file") or row.get("remote_url") or "")
            if not key or key in seen:
                continue
            appended = dict(row)
            appended["role"] = f"listing_{len(final_gallery) + 1}"
            final_gallery.append(appended)
            seen.add(key)
            if len(final_gallery) >= 8:
                break
        galleries.append({
            "sku_id": hero["sku_id"],
            "variant": hero["variant"],
            "gallery": final_gallery,
            "review_status": "UNREVIEWED",
        })
    review["sku_generated_gallery"] = generated
    review["sku_listing_galleries"] = galleries
    review["sku_gallery_updated_at"] = datetime.now(timezone.utc).isoformat()
    draft.setdefault("edit_metadata", {})["sku_images_updated_at"] = review["sku_gallery_updated_at"]
    draft["human_approved"] = False
    versions = root / "draft_versions"
    versions.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    (versions / f"{draft_path.stem}-before-sku-images-{stamp}.json").write_text(
        draft_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    temporary = draft_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(draft_path)


def _persist_common_gallery(root: Path, draft_name: str, job: dict[str, Any]) -> None:
    """Place generated common images before retained source images for review."""
    draft_path = root / "drafts" / Path(draft_name).name
    if not draft_path.is_file():
        raise ImageGenerationError("公共图片已生成，但没有找到对应草稿。")
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    review = draft.setdefault("evidence", {}).setdefault("image_review", {})
    generated = [
        {"role": "main" if not index else f"ai_{index + 1}", "local_file": str(item["local_file"]).removeprefix("assets/"), "source": "ai_generated", "review_status": "UNREVIEWED"}
        for index, item in enumerate(job.get("items") or [])
        if item.get("status") == "done" and item.get("local_file")
    ]
    review["generated_gallery"] = generated
    locks = (draft.get("edit_metadata") or {}).get("field_locks") or {}
    if not locks.get("images") and generated:
        existing = [dict(row) for row in (review.get("gallery") or []) if isinstance(row, dict)]
        combined = generated + [row for row in existing if str(row.get("local_file") or row.get("remote_url") or "") not in {str(x.get("local_file") or x.get("remote_url") or "") for x in generated}]
        review["gallery"] = combined[:8]
    review["common_gallery_updated_at"] = datetime.now(timezone.utc).isoformat()
    draft["human_approved"] = False
    versions = root / "draft_versions"
    versions.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    (versions / f"{draft_path.stem}-before-common-images-{stamp}.json").write_text(draft_path.read_text(encoding="utf-8"), encoding="utf-8")
    temporary = draft_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(draft_path)


def run_job(root: Path, draft_name: str, prompts: list[dict[str, str]], source_images: list[Path], job_path: Path, source_images_by_sku: dict[str, list[Path]] | None = None) -> None:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    try:
        config = load_config(root)
        output_dir = root / "assets" / "ai-generated" / Path(draft_name).stem
        output_dir.mkdir(parents=True, exist_ok=True)
        for index, item in enumerate(prompts):
            latest = json.loads(job_path.read_text(encoding="utf-8"))
            if latest.get("status") == "paused":
                job = latest
                job["updated_at"] = datetime.now(timezone.utc).isoformat()
                job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
                return
            if index < len(job.get("items") or []) and job["items"][index].get("status") == "done" and job["items"][index].get("local_file"):
                continue
            job["items"][index]["status"] = "generating"
            job["items"][index].pop("error", None)
            job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            per_sku = (source_images_by_sku or {}).get(str(item.get("sku_id") or ""))
            image, meta = call_image_api(config, item["prompt"], per_sku or source_images)
            suffix = ".png" if image.startswith(b"\x89PNG") else ".jpg"
            destination = output_dir / f"{index + 1:02d}-{uuid.uuid4().hex[:8]}{suffix}"
            destination.write_bytes(image)
            job["items"][index].update({"status": "done", "local_file": destination.relative_to(root / "assets").as_posix(), "provider": meta})
            job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        job["status"] = "done"
        if job.get("job_type") == "sku_images":
            _persist_sku_galleries(root, draft_name, job)
        else:
            _persist_common_gallery(root, draft_name, job)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)[:500]
        for item in job.get("items") or []:
            if item.get("status") in {"queued", "generating"}:
                item["status"] = "failed"
                item["error"] = str(exc)[:500]
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
