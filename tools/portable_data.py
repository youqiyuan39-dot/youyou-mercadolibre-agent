"""Checksummed private business backup. Never upload the resulting ZIP."""
from __future__ import annotations
import argparse, hashlib, json, os, re, stat, tempfile, zipfile
from pathlib import Path, PurePosixPath
ROOT=Path(__file__).resolve().parents[1]
MANIFEST='RESTORE_MANIFEST.json'
DATA_DIRS=('assets','drafts','draft_versions','data','jobs','reviews','ai_outputs','recycle_bin')
EXTRA_FILES={'config/model_settings.json'}
SKIP_FILES={'data/workflow_board.json','data/connection_probe.json','data/orders_snapshot.json','assets/brand/youyou-logo.png','assets/brand/youyou-original.ico','assets/ui/embedded-v3.css','assets/ui/embedded-v3.js'}
EXCLUDED=('credential','secret','token','cookie','webview','browser','local storage','session storage')

def safe_parts(name):
    if not name or '\\' in name or name.startswith('/') or '//' in name:return False
    return all(p not in ('','.', '..') and ':' not in p and not p.endswith((' ','.')) and not re.fullmatch(r'(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',p,re.I) for p in name.split('/'))

def allowed(name):
    if not safe_parts(name) or name in SKIP_FILES:return False
    p=PurePosixPath(name)
    if p.parts[:2] in {('assets','brand'),('assets','ui')}:return False
    if any(x.startswith('.') or x in ('__pycache__','test-scratch','operations') or any(t in x.lower() for t in EXCLUDED) for x in p.parts):return False
    if p.suffix.lower() in {'.log','.dat','.pem','.p12','.pfx','.pid','.lock','.pyc','.db','.sqlite','.sqlite3'}:return False
    return (p.parts[0] in DATA_DIRS and len(p.parts)>1) or name in EXTRA_FILES

def files(root):
    for name in DATA_DIRS:
        directory=root/name
        if not directory.is_dir() or directory.is_symlink() or directory.is_junction():continue
        for parent, directories, names in os.walk(directory,followlinks=False):
            directories[:]=[n for n in directories if allowed((Path(parent)/n/'probe.json').relative_to(root).as_posix()) and not (Path(parent)/n).is_symlink() and not (Path(parent)/n).is_junction()]
            for n in names:
                p=Path(parent)/n
                if not p.is_symlink() and allowed(p.relative_to(root).as_posix()):yield p
    for n in EXTRA_FILES:
        p=root/n
        if p.is_file() and not p.is_symlink():yield p

def export_data(output,root=ROOT):
    root=Path(root).resolve();output=Path(output).resolve()
    if output.is_relative_to(root):raise ValueError('Place the private archive outside the Git project')
    if output.exists():raise FileExistsError(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    sources=sorted(files(root));before={p:(p.stat().st_size,p.stat().st_mtime_ns) for p in sources}
    manifest={'version':2,'source_root':str(root),'excluded':['credentials','browser profiles','logs','process state','account configuration','operations snapshots'],'files':[]}
    try:
        with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as z:
            for p in sources:
                name=p.relative_to(root).as_posix();digest=hashlib.sha256()
                with p.open('rb') as src,z.open(name,'w',force_zip64=True) as dst:
                    while block:=src.read(1024*1024):digest.update(block);dst.write(block)
                manifest['files'].append({'path':name,'size':before[p][0],'sha256':digest.hexdigest()})
            if sources!=sorted(files(root)) or any((p.stat().st_size,p.stat().st_mtime_ns)!=before[p] for p in sources):raise RuntimeError('Source changed during export; keep workbench idle and retry')
            z.writestr(MANIFEST,json.dumps(manifest,ensure_ascii=False,indent=2))
    except Exception:
        output.unlink(missing_ok=True);raise
    return len(sources)

def inspect_archive(archive,root):
    items=[];seen=set()
    for info in archive.infolist():
        if info.is_dir():continue
        name=info.filename;folded=name.casefold()
        if (name!=MANIFEST and not allowed(name)) or folded in seen:raise ValueError('Unsafe or duplicate archive entry: '+name)
        if stat.S_IFMT(info.external_attr>>16)==stat.S_IFLNK:raise ValueError('Archive links are forbidden')
        seen.add(folded)
        if name==MANIFEST:continue
        dest=root.joinpath(*PurePosixPath(name).parts)
        if not dest.resolve().is_relative_to(root):raise ValueError('Destination escapes project')
        for parent in [dest,*dest.parents]:
            if parent==root:break
            if parent.is_symlink() or parent.is_junction():raise ValueError('Destination link is forbidden')
        if dest.exists():raise FileExistsError(dest)
        items.append(info)
    return items

def rebase(value,old,new):
    if isinstance(value,dict):return {k:rebase(v,old,new) for k,v in value.items()}
    if isinstance(value,list):return [rebase(v,old,new) for v in value]
    if isinstance(value,str):
        canonical=value.replace('\\','/');prefix=old.replace('\\','/').rstrip('/')+'/'
        if canonical.casefold().startswith(prefix.casefold()):
            suffix=canonical[len(prefix):]
            if safe_parts(suffix):return (new/PurePosixPath(suffix)).as_posix()
    return value

def import_data(source,root=ROOT):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(source) as z:
        items=inspect_archive(z,root)
        if MANIFEST not in z.namelist():raise ValueError('Archive lacks integrity manifest; re-export with current tools')
        manifest=json.loads(z.read(MANIFEST));entries=manifest.get('files',[]);expected={row['path']:row for row in entries}
        if manifest.get('version')!=2 or len(expected)!=len(entries) or set(expected)!={i.filename for i in items}:raise ValueError('Manifest mismatch')
        marker=root/'data/MIGRATION_REVIEW_REQUIRED.json'
        if marker.exists():raise FileExistsError(marker)
        with tempfile.TemporaryDirectory(prefix='.portable-import-',dir=root) as folder:
            staging=Path(folder)
            for info in items:
                p=staging/PurePosixPath(info.filename);p.parent.mkdir(parents=True,exist_ok=True);h=hashlib.sha256();size=0
                with z.open(info) as src,p.open('wb') as dst:
                    while block:=src.read(1024*1024):h.update(block);size+=len(block);dst.write(block)
                if h.hexdigest()!=expected[info.filename]['sha256'] or size!=expected[info.filename]['size']:raise ValueError('Checksum mismatch: '+info.filename)
                if p.suffix=='.json':
                    try:value=json.loads(p.read_text(encoding='utf-8-sig'))
                    except (ValueError,UnicodeError):continue
                    rebased=rebase(value,manifest['source_root'],root)
                    if rebased!=value:p.write_text(json.dumps(rebased,ensure_ascii=False,indent=2),encoding='utf-8')
            inspect_archive(z,root)
            marker.parent.mkdir(parents=True,exist_ok=True)
            with marker.open('x',encoding='utf-8') as f:json.dump({'version':1,'automatic_processing_paused':True,'reason':'Reauthorize stores and verify imported facts, images and prices before resuming'},f)
            for info in items:
                dest=root/PurePosixPath(info.filename);dest.parent.mkdir(parents=True,exist_ok=True)
                if dest.exists():raise FileExistsError(dest)
                os.replace(staging/PurePosixPath(info.filename),dest)
    return len(items)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('export','import'));p.add_argument('archive',type=Path);p.add_argument('--root',type=Path,default=ROOT)
    a=p.parse_args();n=(export_data if a.action=='export' else import_data)(a.archive,a.root);print(json.dumps({'action':a.action,'files':n,'credentials_included':False}))
if __name__=='__main__':main()
