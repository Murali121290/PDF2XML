"""Save the current code as a versioned zip in ``releases/``.

    .venv\\Scripts\\python.exe tools\\package.py       # releases\\pdf2xml-0.2.0-20260929-1530.zip
    .venv\\Scripts\\python.exe tools\\package.py --out D:\\backups

The zip holds the source only: ``src/``, ``tests/`` (with its small generated fixture PDFs),
``examples/``, ``tools/``, ``docs/`` and the project files. It never holds the virtual environment,
generated output (``out/``), caches, earlier releases or the input PDFs kept in the project
folder. A ``MANIFEST.txt`` inside lists every file with its SHA-256, so a copy can be checked.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["pyproject.toml", "README.md", "CLAUDE.md", "CHANGELOG.md", ".gitignore", "uv.lock"]
DIRS = ["src", "tests", "examples", "tools", "docs"]
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "out",
             "samples", "releases", "dist", "build"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


def version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text("utf-8"),
                  re.M)
    if not m:
        sys.exit("no version in pyproject.toml")
    return m.group(1)


def collect() -> list[Path]:
    out = [ROOT / f for f in FILES if (ROOT / f).is_file()]
    for d in DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            rel = p.relative_to(ROOT)
            if (p.is_file() and not SKIP_DIRS.intersection(rel.parts)
                    and p.suffix not in SKIP_SUFFIXES and not p.name.endswith(".egg-info")):
                out.append(p)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "releases", help="folder for the zip")
    args = ap.parse_args()
    ver = version()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"pdf2xml-{ver}-{stamp}.zip"
    if target.exists():
        sys.exit(f"{target} already exists")
    files = collect()
    lines = [f"pdf2xml {ver}", f"packaged {datetime.now().isoformat(timespec='seconds')}",
             f"files {len(files)}", ""]
    prefix = f"pdf2xml-{ver}"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            rel = p.relative_to(ROOT).as_posix()
            data = p.read_bytes()
            z.writestr(f"{prefix}/{rel}", data)
            lines.append(f"{hashlib.sha256(data).hexdigest()}  {rel}")
        z.writestr(f"{prefix}/MANIFEST.txt", "\n".join(lines) + "\n")
    size = target.stat().st_size / 1024
    print(f"{target}  ({len(files)} files, {size:,.0f} KB)")


if __name__ == "__main__":
    main()
