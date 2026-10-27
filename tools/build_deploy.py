"""Build the offline deploy package: PDF → Docling → canonical JSON/XML → BITS → JATS.

    .venv\\Scripts\\python.exe tools\\build_deploy.py
    → releases\\pdf2xml-deploy-<version>-win64.zip

Run it with the project's virtual environment: the packages are copied from there, so the
package holds exactly the versions the tests ran with, and nothing is downloaded. The zip holds:

- ``python/``: a private CPython (the base interpreter of the virtual environment, without its
  docs, Tcl/Tk and headers) with the Visual C++ runtime DLLs beside it, and in its
  ``Lib/site-packages`` the runtime dependencies (Docling, PyTorch CPU, pdfplumber …) and a cut
  ``pdf2xml`` holding only the conversion (no EPUB, DOCX, GROBID, preview server or QA checks);
- ``models/``: the two Docling models the pipeline uses (layout, TableFormer);
- ``convert.bat``, ``README.md``, ``examples/`` (profile and metadata samples),
  ``THIRD-PARTY.txt`` (every bundled package with its version and licence) and ``MANIFEST.txt``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import zipfile
from datetime import datetime
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# what the conversion imports (pyproject's dependencies, minus typer, saxonche, python-docx)
RUNTIME = ["pydantic", "pdfplumber", "pypdfium2", "pikepdf", "lxml", "pillow", "pyyaml",
           "docling"]
MODELS = ["docling-project--docling-layout-heron", "docling-project--docling-models"]
VC_RUNTIME = ["msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll",
              "vcruntime140_1.dll", "concrt140.dll", "vcomp140.dll"]

# the cut pdf2xml: modules for other outputs and tools are left out
SKIP_MODULES = {
    "cli.py",                                   # replaced by deploy/cli.py
    "preview.py", "jobs.py",                    # preview / upload server
    "export/epub.py", "export/docx.py", "export/css.py", "xml/xslt.py",   # EPUB, DOCX
    "publishing/grobid.py",                     # GROBID
    "extract/structure_heuristic.py",           # Docling only
    "qa/completeness.py", "qa/exports.py", "qa/images.py", "qa/ink.py", "qa/overlay.py",
    "qa/textlayer.py", "qa/unicode.py",         # QA reports and overlays
}
SKIP_RESOURCE_DIRS = {"resources/preview", "resources/xslt"}

# the base interpreter: what a conversion never needs
PY_SKIP_DIRS = {"Doc", "tcl", "include", "libs", "Scripts", "__pycache__", "test", "tests",
                "idlelib", "tkinter", "turtledemo", "ensurepip", "venv", "lib2to3"}
PY_SKIP_FILES = {"__install__.json", "pythonw.exe", "_tkinter.pyd", "tcl86t.dll", "tk86t.dll"}
# inside site-packages: build-time files (import libraries, headers, debug symbols, CMake)
SITE_SKIP_SUFFIXES = {".lib", ".pdb", ".pyc", ".pyo"}
SITE_SKIP_PREFIXES = ("torch/include/", "torch/share/")


def version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text("utf-8"),
                  re.M)
    if not m:
        sys.exit("no version in pyproject.toml")
    return m.group(1)


def _requirement(spec: str):  # -> packaging Requirement
    try:
        from packaging.requirements import Requirement
    except ImportError:                                  # pip always vendors it
        from pip._vendor.packaging.requirements import Requirement  # type: ignore
    return Requirement(spec)


def _key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def closure(roots: list[str], env: dict[str, str] | None = None,
            missing: list[str] | None = None) -> list[metadata.Distribution]:
    """The installed distributions ``roots`` need, extras included. ``env``: marker values of
    another platform (e.g. Linux); a requirement only that platform has is not installed
    here, so it goes to ``missing`` (as written in the metadata) instead of failing."""
    by_key = {_key(d.metadata["Name"]): d for d in metadata.distributions()}
    seen: dict[str, metadata.Distribution] = {}
    todo: list[tuple[str, frozenset[str], str]] = [(r, frozenset(), r) for r in roots]
    done_extras: dict[str, set[str]] = {}
    while todo:
        name, extras, spec_text = todo.pop()
        key = _key(name)
        dist = by_key.get(key)
        if dist is None:
            if missing is not None:
                if spec_text not in missing:
                    missing.append(spec_text)
                continue
            sys.exit(f"{name} is required but not installed in this environment")
        new = set(extras) - done_extras.get(key, set())
        if key in seen and not new:
            continue
        seen[key] = dist
        done_extras.setdefault(key, set()).update(extras)
        for spec in dist.requires or []:
            req = _requirement(spec)
            envs = [{**(env or {}), "extra": e} for e in sorted(extras)] or [
                {**(env or {}), "extra": ""}]
            if req.marker is None or any(req.marker.evaluate(e) for e in envs):
                todo.append((req.name, frozenset(req.extras), spec.split(";")[0].strip()))
    return sorted(seen.values(), key=lambda d: _key(d.metadata["Name"]))


def copy_python(base: Path, dest: Path) -> None:
    for src in base.rglob("*"):
        rel = src.relative_to(base)
        if (PY_SKIP_DIRS.intersection(rel.parts) or src.name in PY_SKIP_FILES
                or rel.parts[:2] == ("Lib", "site-packages")):
            continue
        if src.is_file():
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
    system32 = Path("C:/Windows/System32")
    for dll in VC_RUNTIME:                    # app-local Visual C++ runtime (redistributable)
        if (system32 / dll).exists() and not (dest / dll).exists():
            shutil.copy2(system32 / dll, dest / dll)


def copy_dists(dists: list[metadata.Distribution], site: Path) -> int:
    n = 0
    for d in dists:
        for f in d.files or []:
            rel = f.as_posix()
            if rel.startswith("..") or "__pycache__" in rel or rel.startswith(SITE_SKIP_PREFIXES):
                continue                      # console scripts, caches, build-time files
            if Path(rel).suffix in SITE_SKIP_SUFFIXES:
                continue
            src = Path(str(d.locate_file(f)))
            if not src.is_file():
                continue
            out = site / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
            n += 1
    return n


def copy_pdf2xml(site: Path, skip: set[str] | frozenset[str] = frozenset(SKIP_MODULES),
                 extra: tuple[str, ...] = ("cli.py", "__main__.py")) -> None:
    """The cut package: ``src/pdf2xml`` without ``skip``, plus ``extra`` from ``deploy/``."""
    src = ROOT / "src" / "pdf2xml"
    dest = site / "pdf2xml"
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src).as_posix()
        if (not p.is_file() or "__pycache__" in rel or rel in skip
                or any(rel.startswith(d + "/") for d in SKIP_RESOURCE_DIRS)
                or p.suffix in {".pyc", ".pyo"}):
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)
    for name in extra:
        shutil.copy2(ROOT / "deploy" / name, dest / name)


def third_party(dists: list[metadata.Distribution]) -> str:
    lines = ["Bundled Python packages (name, version, licence). The Python interpreter is",
             "CPython (PSF License); the Docling models are licensed as stated in models/.", ""]
    for d in dists:
        m = d.metadata
        lic = (m.get("License-Expression") or "").strip()
        if not lic:
            lic = (m.get("License") or "").strip().splitlines()[0][:60] if m.get("License") \
                else ""
        if not lic:
            classifiers = [c.split("::")[-1].strip() for c in m.get_all("Classifier") or []
                           if c.startswith("License ::")]
            lic = "; ".join(classifiers) or "see package metadata"
        lines.append(f"{m['Name']:<32} {m['Version']:<14} {lic}")
    return "\n".join(lines) + "\n"


def build(out_root: Path, base_python: Path, models: Path) -> Path:
    ver = version()
    name = f"pdf2xml-deploy-{ver}-win64"
    stage = ROOT / "build" / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    print(f"python   ← {base_python}")
    copy_python(base_python, stage / "python")
    site = stage / "python" / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    dists = closure(RUNTIME)
    n = copy_dists(dists, site)
    print(f"packages ← {len(dists)} distributions, {n} files")
    copy_pdf2xml(site)

    for m in MODELS:
        if not (models / m).is_dir():
            sys.exit(f"model folder missing: {models / m} "
                     "(docling-tools models download layout tableformer)")
        shutil.copytree(models / m, stage / "models" / m)
    print(f"models   ← {models}")

    shutil.copy2(ROOT / "deploy" / "convert.bat", stage / "convert.bat")
    shutil.copy2(ROOT / "deploy" / "README.md", stage / "README.md")
    for sub in ("profiles", "metadata"):
        if (ROOT / "examples" / sub).is_dir():
            shutil.copytree(ROOT / "examples" / sub, stage / "examples" / sub)
    (stage / "THIRD-PARTY.txt").write_text(third_party(dists), encoding="utf-8")
    (stage / "VERSION.txt").write_text(json.dumps({
        "pdf2xml": ver, "edition": "deploy (PDF → Docling → JSON/XML → BITS/JATS)",
        "python": sys.version.split()[0], "built": datetime.now().isoformat(timespec="seconds"),
        "packages": {d.metadata["Name"]: d.metadata["Version"] for d in dists},
        "models": MODELS}, indent=1) + "\n", encoding="utf-8")

    out_root.mkdir(parents=True, exist_ok=True)
    target = out_root / f"{name}.zip"
    if target.exists():
        target.unlink()
    files = sorted(p for p in stage.rglob("*") if p.is_file())
    lines = [f"{name}", f"files {len(files)}", ""]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, allowZip64=True,
                         compresslevel=6) as z:
        for p in files:
            rel = p.relative_to(stage).as_posix()
            z.write(p, f"{name}/{rel}")
            h = hashlib.sha256()
            with p.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            lines.append(f"{h.hexdigest()}  {rel}")
        z.writestr(f"{name}/MANIFEST.txt", "\n".join(lines) + "\n")
    size = sum(p.stat().st_size for p in files) / 2**20
    print(f"staged   {stage}  ({len(files)} files, {size:,.0f} MB)")
    print(f"zip      {target}  ({target.stat().st_size / 2**20:,.0f} MB)")
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    cfg = Path(sys.prefix) / "pyvenv.cfg"
    home = next((ln.split("=", 1)[1].strip() for ln in cfg.read_text().splitlines()
                 if ln.startswith("home")), None) if cfg.exists() else None
    ap.add_argument("--python", type=Path, default=Path(home) if home else None,
                    help="base CPython folder to bundle (default: the venv's base)")
    ap.add_argument("--models", type=Path,
                    default=Path.home() / ".cache" / "docling" / "models",
                    help="folder holding the Docling models")
    ap.add_argument("--out", type=Path, default=ROOT / "releases", help="folder for the zip")
    args = ap.parse_args()
    if sys.prefix == sys.base_prefix:
        sys.exit("run this with the project's virtual environment (.venv\\Scripts\\python.exe)")
    if args.python is None or not (args.python / "python.exe").exists():
        sys.exit(f"no python.exe in {args.python}; pass --python <CPython folder>")
    build(args.out, args.python, args.models)


if __name__ == "__main__":
    main()
