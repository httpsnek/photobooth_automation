#!/usr/bin/env python3
"""Build a clean release archive for a customer / a new booth.

Output:  InstaBOX_release.zip  in the repo root.

Contents: source (backend/, frontend/, tests/), launch scripts (*.bat, *.ps1),
.env.example, requirements*.txt, docs (*.md), Dockerfile — plus empty
logs/ and data/ (each with a .gitkeep).

Excluded: .git .gitignore .gitattributes .github .claude · .venv venv env ·
__pycache__ .pytest_cache node_modules · the real .env and any .env.* except
.env.example · *.pyc *.pyo *.db *.sqlite3 *.lock *.log · .DS_Store ·
make_release.* · any previous InstaBOX_release.zip

Run:  make_release.bat   (or  python make_release.py)
"""
from __future__ import annotations

import fnmatch
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ZIP_NAME = "InstaBOX_release.zip"

# directory names excluded anywhere in the tree
EXCLUDE_DIRS = {
    ".git", ".github", ".claude", ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", "data", "logs",
}
# file names / globs excluded anywhere
EXCLUDE_FILES = {
    ".gitignore", ".gitattributes", ".DS_Store", ".env",
    ZIP_NAME, "make_release.py", "make_release.bat",
}
EXCLUDE_GLOBS = ("*.pyc", "*.pyo", "*.db", "*.db-*", "*.sqlite3",
                 "*.lock", "*.log", "*.env.local")


def is_excluded_file(name: str) -> bool:
    if name in EXCLUDE_FILES:
        return True
    if name.startswith(".env") and name != ".env.example":
        return True   # .env.production, .env.dev-backup, …
    return any(fnmatch.fnmatch(name, g) for g in EXCLUDE_GLOBS)


def collect(root: Path):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.is_file() and not is_excluded_file(p.name):
            yield p, rel


def main() -> int:
    if not (ROOT / "backend").is_dir():
        print("ERROR: run from the repo root (backend/ not found)")
        return 1

    zip_path = ROOT / ZIP_NAME
    if zip_path.exists():
        zip_path.unlink()

    files = list(collect(ROOT))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, rel in files:
            z.write(src, rel.as_posix())
        # keep runtime dirs present but empty
        for d in ("logs", "data"):
            z.writestr(f"{d}/.gitkeep", "")

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"  {len(files)} files")
    print(f"  OK  ->  {zip_path}  ({size_mb:.2f} MB)")
    print("  Give this archive to the customer: unzip, then run the START .bat as administrator.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
