"""Bounded Windows sharing-violation recovery; never overwrite a changed target."""
import hashlib,json,time,uuid
from pathlib import Path

def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    initial=fingerprint(path)
    candidate=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    candidate.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    for attempt in range(6):
        if fingerprint(path)!=initial:raise RuntimeError('保存目标已被其他操作修改，未覆盖；候选保留于 '+str(candidate))
        try:candidate.replace(path);return
        except PermissionError as exc:
            if attempt==5:raise PermissionError('保存遇到持续文件占用，已停止；候选保留于 '+str(candidate)) from exc
            time.sleep(.02*(2**attempt))
