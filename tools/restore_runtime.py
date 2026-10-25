"""Restore the privately transferred model/runtime archive, with hash checks."""
import argparse,hashlib,json,os,stat,tempfile,zipfile
from pathlib import Path,PurePosixPath
try:
    from .portable_data import safe_parts
except ImportError:
    from portable_data import safe_parts
MANIFEST='RUNTIME_MANIFEST.json'

def restore(archive,root):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        body=json.loads(z.read(MANIFEST));rows=body['files'];names=[r['path'] for r in rows]
        if body.get('version')!=1 or len(set(n.casefold() for n in names))!=len(names):raise ValueError('Invalid manifest')
        if set(z.namelist())!=set(names+[MANIFEST]) or len(z.namelist())!=len(names)+1:raise ValueError('Unexpected archive entries')
        for name in names:
            if not safe_parts(name) or not name.startswith(('models/','runtime/llama/','tools/rag_models/')):raise ValueError('Unsafe runtime path')
            dest=root/PurePosixPath(name)
            if not dest.resolve().is_relative_to(root):raise ValueError('Destination escapes project')
            if dest.exists():raise FileExistsError(dest)
            for parent in [dest,*dest.parents]:
                if parent==root:break
                if parent.is_symlink() or parent.is_junction():raise ValueError('Destination link')
            if stat.S_IFMT(z.getinfo(name).external_attr>>16)==stat.S_IFLNK:raise ValueError('Archive link')
        with tempfile.TemporaryDirectory(prefix='.runtime-import-',dir=root) as temp:
            staging=Path(temp)
            for row in rows:
                dest=staging/PurePosixPath(row['path']);dest.parent.mkdir(parents=True,exist_ok=True);digest=hashlib.sha256();size=0
                with z.open(row['path']) as src,dest.open('wb') as out:
                    while block:=src.read(4*1024*1024):out.write(block);digest.update(block);size+=len(block)
                if size!=row['size'] or digest.hexdigest()!=row['sha256']:raise ValueError('Runtime checksum mismatch')
            for row in rows:
                dest=root/PurePosixPath(row['path']);dest.parent.mkdir(parents=True,exist_ok=True)
                if dest.exists():raise FileExistsError(dest)
                os.replace(staging/PurePosixPath(row['path']),dest)
    return len(rows)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('archive',type=Path);p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);a=p.parse_args()
    print(json.dumps({'restored_files':restore(a.archive,a.root),'started_processes':0}))
