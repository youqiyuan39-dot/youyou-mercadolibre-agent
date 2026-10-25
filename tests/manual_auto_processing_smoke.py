"""Opt-in local model smoke check. Uses isolated copies, never publishes or generates images."""
import copy
import json
import shutil
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import workbench_v3, draft_editor as legacy
from src.auto_processing_v3 import write
from src.source_image_screening_v3 import inspect_image


def main():
    project = Path(__file__).resolve().parents[1]
    root = project.parent / '验证_20261015_自动处理'
    (root / 'config').mkdir(parents=True, exist_ok=True)
    shutil.copyfile(project / 'config/local_model_runtime.json', root / 'config/local_model_runtime.json')
    (root / 'assets').mkdir(exist_ok=True)
    draft = json.loads((project / 'drafts/1688-1058417690595-draft.json').read_text(encoding='utf-8'))
    evidence = draft['evidence']
    capture = project / str(evidence.get('source_capture') or '')
    if capture.is_file():
        data = json.loads(capture.read_text(encoding='utf-8'))
        evidence['source_title'] = data.get('title')
        evidence['supplier_page']['title'] = data.get('title')
    evidence.pop('source_capture', None)
    evidence.pop('processing_automation', None)
    evidence.pop('ai_processing_pricing', None)
    # Screens use ordinary original images; do not pretend they prove SKU packaging identity.
    report = {'original_drafts_changed': False, 'publishing_performed': False, 'paid_image_calls': 0, 'screens': []}
    for label, name in [('promotional_poster', 'fc581fb2528e5c4e4e521349ef1a7b906df6ec645a92faa64ce3c6fc71c1319b.jpg'),
                        ('product_closeup', 'f786b9b7a31620b4c02cdef3f29c2ed1821671c154feda6c647646d8b8f7764f.jpg')]:
        shutil.copyfile(project / 'assets/remote-cache' / name, root / 'assets' / name)
        test = copy.deepcopy(draft); test['evidence']['image_review'] = {}
        began = time.monotonic()
        verdict = inspect_image(root, test, {'sku_id': 'visual-only', 'variant': '伸缩活动扳手，蓝黑手柄；不判断包装'}, {'local_file': name})
        report['screens'].append({'label': label, 'seconds': round(time.monotonic()-began, 2), **verdict})
        write(root / 'verification.json', report)
        print(json.dumps({'screen': label, 'usable': verdict['usable'], 'reason': verdict['reason']}, ensure_ascii=False), flush=True)
    # Real original SKU facts, isolated from published products; no invented per-SKU image bindings.
    evidence['image_review'] = {'source_gallery': [], 'sku_source_galleries': [], 'sku_listing_galleries': []}
    for sku in evidence.get('sku_details') or []:
        sku.pop('ai_content', None); sku.pop('ai_processing', None)
    draft['payload']['description'] = ''
    draft['payload']['sites_to_sell'] = [{'site_id': 'MLM', 'logistic_type': 'remote'}]
    path = root / 'drafts/smoke-wrench-draft.json'
    write(path, draft)
    workbench_v3.install_guards()
    full = root / 'jobs/smoke-wrench-draft-full-ai-job.json'
    write(full, {'status': 'queued', 'draft': path.name})
    began = time.monotonic()
    legacy._run_full_ai_flow(path, root, full)
    job = legacy.read_json(full)
    result = legacy.read_json(path)
    report.update(seconds=round(time.monotonic()-began, 2), job=job,
        sku_results=[{'sku_id': s['sku_id'], 'processing': s.get('ai_processing'), 'content': s.get('ai_content')} for s in result['evidence']['sku_details']])
    write(root / 'verification.json', report)
    print(json.dumps({'status': job['status'], 'items': job.get('items'), 'pricing_error': job.get('pricing_error')}, ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
