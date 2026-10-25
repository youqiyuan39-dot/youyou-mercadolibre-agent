import shutil
def node():
    executable=shutil.which('node')
    if not executable: raise RuntimeError('Node.js 22+ must be on PATH for collector tests')
    return executable
