"""Persisted stage and image recovery; never replay completed paid image calls."""
import copy
import hashlib
import json
import threading
import time
from pathlib import Path

from .product_core_v3 import product_from_draft, review_issues


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def text_signature(draft, public):
    from .prompt_policy_v3 import PROMPT_VERSION
    return digest({'prompt_version': PROMPT_VERSION, 'category_id': (draft.get('payload') or {}).get('category_id'), 'facts': product_from_draft(draft)['facts_version'], 'template': (draft.get('evidence') or {}).get('prompt_template'), 'models': {key: {k: (public.get('roles') or {}).get(key, {}).get(k) for k in ['model', 'base_url']} for key in ['vision', 'attributes']}})


def image_signature(prompts, paths, public_role):
    return digest({'prompts': prompts, 'references': [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths], 'model': public_role.get('model'), 'base_url': public_role.get('base_url')})


def merge_completed(old, generated, root, signature):
    result = [{'index': i, 'kind': row['kind'], 'label': row['label'], 'status': 'queued'} for i, row in enumerate(generated)]
    if old.get('input_signature') != signature: return result
    for i, row in enumerate(result):
        previous = next((x for x in old.get('items') or [] if x.get('index') == i and x.get('kind') == row['kind']), {})
        local = str(previous.get('local_file') or '').removeprefix('assets/')
        path = (root / 'assets' / local).resolve()
        if previous.get('status') == 'done' and local and (root / 'assets').resolve() in path.parents and path.is_file():
            row.update(copy.deepcopy(previous)); row['reused'] = True
    return result


def install(legacy):
    if getattr(legacy, '_v3_resume_installed', False): return
    legacy._v3_resume_installed = True
    from . import image_generation
    from .generation_strategy_v3 import load
    from .workbench_v3 import write, stamp, audit
    original_apply = legacy._apply_ai_content_and_attributes
    def apply(path, root, full_path, job):
        draft = legacy.read_json(path)
        signature = text_signature(draft, legacy.get_public_settings(root))
        checkpoint = root / 'data' / 'v3' / 'checkpoints' / (path.stem + '.json')
        previous = legacy.read_json(checkpoint) if checkpoint.exists() else {}
        proposal_path = root / 'ai_outputs' / (path.stem + '-proposal.json')
        conflicts = [i for i in review_issues(draft) if '容量' in i or '当前 SKU 不在' in i]
        if previous.get('text_signature') == signature and proposal_path.is_file() and not conflicts:
            audit(root, '复用文案与属性', {'name': path.name, 'signature': signature})
            return
        original_apply(path, root, full_path, job)
        draft = legacy.read_json(path)
        previous.update({'text_signature': text_signature(draft, legacy.get_public_settings(root)), 'text_completed_at': stamp()})
        write(checkpoint, previous)
    legacy._apply_ai_content_and_attributes = apply
    def start_images(name, language, prompts=None, drafts_dir=legacy.DRAFTS_DIR, root=legacy.ROOT):
        path = legacy._draft_path(name, drafts_dir); draft = legacy.read_json(path)
        job_path = root / 'jobs' / (path.stem + '-image-job.json')
        old = legacy.read_json(job_path) if job_path.exists() else {}
        if old.get('status') in {'queued', 'running'} and any(i.get('status') in {'queued', 'generating'} for i in old.get('items') or []):
            raise legacy.EditorError('已有图片任务在运行，不能重复提交')
        strategy = load(root)
        locks = (draft.get('edit_metadata') or {}).get('field_locks') or {}
        if strategy['image_count'] == 0 or locks.get('images'):
            job = {'status': 'done', 'draft': path.name, 'items': [], 'prompts': [], 'skipped': 'HUMAN_IMAGE_LOCK' if locks.get('images') else 'ZERO_IMAGE_STRATEGY', 'publishing_performed': False, 'updated_at': stamp()}
            write(job_path, job); return job
        safe_language = legacy.resolve_market_language(draft, language)
        generated = legacy.build_image_prompts(draft, safe_language)
        if prompts:
            overrides = {str(r.get('kind')): str(r.get('prompt') or '').strip()[:8000] for r in prompts if isinstance(r, dict)}
            for row in generated:
                if overrides.get(row['kind']): row['prompt'] = overrides[row['kind']]
        evidence = draft.get('evidence') or {}; review = evidence.get('image_review') or {}
        selected = product_from_draft(draft)['facts']['selected_sku']
        gallery = review.get('source_gallery') or []
        if selected:
            group = next((g for g in review.get('sku_source_galleries') or [] if str(g.get('sku_id')) == str(selected.get('sku_id'))), None)
            if group: gallery = group.get('gallery') or []
            elif len(product_from_draft(draft)['facts']['skus']) > 1:
                raise legacy.EditorError('当前 SKU 未绑定原图，不能使用其他规格的公共图')
        paths = []
        count = int(legacy.get_public_settings(root)['strategy'].get('reference_image_count') or 3)
        for row in gallery[:count]:
            local = str(row.get('local_file') or '').removeprefix('assets/')
            if local:
                candidate = (root / 'assets' / local).resolve()
                if (root / 'assets').resolve() in candidate.parents and candidate.is_file(): paths.append(candidate)
            elif row.get('remote_url'):
                cached, _ = legacy.cache_remote_image(row['remote_url'], root / 'assets' / 'remote-cache'); paths.append(cached)
        if not paths: raise legacy.EditorError('没有当前 SKU 可用原图，不能启动生图')
        public = legacy.get_public_settings(root)
        signature = image_signature(generated, paths, public['roles']['images'])
        items = merge_completed(old, generated, root, signature)
        job = {'status': 'queued', 'draft': path.name, 'language': safe_language, 'prompts': generated, 'items': items, 'input_signature': signature, 'reference_count': len(paths), 'reused_images': sum(i.get('reused') is True for i in items), 'created_at': stamp(), 'updated_at': stamp(), 'publishing_performed': False}
        write(job_path, job)
        def run():
            image_generation.run_job(root, path.name, generated, paths, job_path)
            final = legacy.read_json(job_path)
            if final.get('status') == 'failed' and any(i.get('status') == 'done' for i in final.get('items') or []):
                image_generation._persist_common_gallery(root, path.name, final)
                audit(root, '部分生图失败，已保留成功图片', {'name': path.name, 'completed': sum(i.get('status') == 'done' for i in final['items'])})
        threading.Thread(target=run, daemon=True).start()
        return job
    legacy.start_image_generation = start_images
