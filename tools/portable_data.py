"""Export or import local workbench data without account-bound credentials.

The archive stays on the operator's computer. Never commit it to Git.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DATA_DIRS = (
    "assets", "drafts", "draft_versions", "data", "jobs",
    "reviews", "ai_outputs", "recycle_bin",
)
EXTRA_FILES = {"config/model_settings.json"}
SKIP_FILES = {"data/workflow_board.json", "assets/brand/youyou-logo.png", "assets/ui/embedded-v3.css", "assets/ui/embedded-v3.js"}


def allowed(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    if any(part in ("", ".", "..") or ":" in part for part in path.parts):
        return False
    if name in SKIP_FILES:
        return False
    if any(part.startswith(".") or any(word in part.lower() for word in ("credential", "secret", "token")) for part in path.parts):
        return False
    if path.suffix.lower() in {".log", ".dat", ".pem", ".p12", ".pfx"}:
        return False
    return (path.parts[0] in DATA_DIRS and len(path.parts) > 1) or name in EXTRA_FILES


def export_data(output: Path, root: Path = ROOT) -> int:
    root = root.resolve()
    output = output.resolve()
    if output == root or root in output.parents:
        raise ValueError("Place the private archive outside the Git project")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in DATA_DIRS:
                folder = root / name
                if not folder.is_dir():
                    continue
                for source in folder.rglob("*"):
                    if source.is_symlink() or not source.is_file():
                        continue
                    relative = source.relative_to(root).as_posix()
                    if allowed(relative):
                        archive.write(source, relative)
                        count += 1
            for relative in EXTRA_FILES:
                source = root / relative
                if source.is_file() and not source.is_symlink():
                    archive.write(source, relative)
                    count += 1
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return count


def inspect_archive(archive: zipfile.ZipFile, root: Path) -> list[zipfile.ZipInfo]:
    items = []
    seen = set()
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if not allowed(name) or name in seen:
            raise ValueError(f"Unsafe or duplicate archive entry: {name}")
        if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
            raise ValueError(f"Symbolic link is not accepted: {name}")
        destination = root.joinpath(*PurePosixPath(name).parts)
        if destination.exists():
            raise FileExistsError(destination)
        seen.add(name)
        items.append(info)
    return items


def import_data(source: Path, root: Path = ROOT) -> int:
    root = root.resolve()
    with zipfile.ZipFile(source) as archive:
        items = inspect_archive(archive, root)
        with tempfile.TemporaryDirectory(prefix=".portable-import-", dir=root) as temporary:
            staging = Path(temporary)
            for info in items:
                target = staging.joinpath(*PurePosixPath(info.filename).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as reader, target.open("wb") as writer:
                    shutil.copyfileobj(reader, writer)
            for info in items:
                relative = PurePosixPath(info.filename)
                target = root.joinpath(*relative.parts)
                if target.exists():
                    raise FileExistsError(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging.joinpath(*relative.parts), target)
    return len(items)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "import"))
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    count = export_data(args.archive) if args.action == "export" else import_data(args.archive)
    print(f"{args.action}: {count} files")


if __name__ == "__main__":
    main()