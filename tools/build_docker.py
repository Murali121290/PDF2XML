"""Build the offline Docker build context for the conversion API.

    .venv\\Scripts\\python.exe tools\\build_docker.py
    → releases\\pdf2xml-docker-<version>.zip

The server team unzips it and runs ``docker build`` (or ``docker compose up -d --build``) with
no internet access: the zip holds Linux wheels for every Python package, the Docling models,
the cut application and the Dockerfile. Only the base image (python:3.13-slim-bookworm) comes
from their registry.

The package list is the tested virtual environment's, re-evaluated for Linux x86-64 and
CPython 3.13, pinned to the same versions. PyTorch comes from the CPU-only index. This machine
downloads the wheels once (internet needed here, not on the server). Then an offline
``pip install --dry-run`` against the downloaded wheels checks the set is complete.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_deploy import (  # noqa: E402
    MODELS,
    ROOT,
    RUNTIME,
    SKIP_MODULES,
    _key,
    _requirement,
    closure,
    copy_pdf2xml,
    third_party,
    version,
)

LINUX = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
         "platform_machine": "x86_64", "platform_python_implementation": "CPython",
         "implementation_name": "cpython", "python_version": "3.13",
         "python_full_version": "3.13.0"}
PLATFORMS = ([f"manylinux_2_{m}_x86_64" for m in range(36, 4, -1)]
             + ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64",
                "linux_x86_64"])
TARGET = ["--python-version", "3.13", "--implementation", "cp", "--abi", "cp313",
          "--abi", "abi3", "--abi", "none", "--only-binary=:all:"]
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
GPU_ONLY = ("triton", "cuda-", "nvidia-")
# the API server needs the job queue and the HTTP server, which the Windows zip leaves out
DOCKER_SKIP = SKIP_MODULES - {"preview.py", "jobs.py"}
DOCKER_FILES = ["Dockerfile", "docker-compose.yml", ".dockerignore", "healthcheck.py",
                "README.md", "API.md"]


def _platform_args() -> list[str]:
    out: list[str] = []
    for p in PLATFORMS:
        out += ["--platform", p]
    return out + TARGET


def requirements() -> tuple[list[str], list, str]:  # type: ignore[type-arg]
    """Pinned requirement lines for Linux, the distributions behind them, and the OpenCV
    version (for its headless twin)."""
    missing: list[str] = []
    dists = closure(RUNTIME, LINUX, missing)
    lines = [f"{d.metadata['Name']}=={d.metadata['Version']}" for d in dists]
    # Linux-only packages: pip resolves them. The GPU stack the Windows wheel's metadata
    # names for Linux (Triton, CUDA) is not used by the CPU build of PyTorch.
    lines += [m for m in missing if not _key(_requirement(m).name).startswith(GPU_ONLY)]
    cv = next((d.metadata["Version"] for d in dists
               if _key(d.metadata["Name"]) == "opencv-python"), None)
    if cv is None:
        sys.exit("opencv-python is not in the closure; check the Dockerfile's OpenCV swap")
    return lines, dists, cv


def _pure(dist) -> bool:  # type: ignore[no-untyped-def]
    """Installed from a pure-Python wheel (``*-none-any``): the same wheel works on Linux."""
    text = dist.read_text("WHEEL") or ""
    tags = [ln.split(":", 1)[1].strip() for ln in text.splitlines() if ln.startswith("Tag:")]
    return "Root-Is-Purelib: true" in text and bool(tags) and all(
        t.endswith("-none-any") for t in tags)


def pip(*args: str) -> None:
    cmd = [sys.executable, "-m", "pip", *args]
    print("  $ pip " + " ".join(args[:2]) + " …", flush=True)
    res = subprocess.run(cmd)
    if res.returncode:
        sys.exit(f"pip failed ({res.returncode}): {' '.join(cmd)}")


def build(out_root: Path, models: Path, wheel_cache: Path) -> Path:
    ver = version()
    name = f"pdf2xml-docker-{ver}"
    stage = ROOT / "build" / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    lines, dists, cv = requirements()
    req = stage / "requirements.txt"
    req.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"packages {len(lines)} requirements (Linux, CPython 3.13); OpenCV {cv}")

    # download (cached between builds), then copy what this requirement set needs. Pure-Python
    # packages are made into wheels here first: a few exist only as source archives
    # (antlr4-python3-runtime 4.9.3), and a pure wheel runs on Linux as it does here.
    wheel_cache.mkdir(parents=True, exist_ok=True)
    pure = [f"{d.metadata['Name']}=={d.metadata['Version']}" for d in dists if _pure(d)
            and not any(wheel_cache.glob(f"{_key(d.metadata['Name']).replace('-', '_')}-"
                                         f"{d.metadata['Version']}-*.whl"))]
    if pure:
        pip("wheel", "--no-deps", "--wheel-dir", str(wheel_cache), *pure)
    # --no-deps: pip evaluates environment markers for the machine it runs on (Windows), not
    # for --platform, so it would ask for pywin32. The list is already the complete Linux
    # closure (``closure`` evaluated the markers for Linux).
    pip("download", "--no-deps", "--dest", str(wheel_cache), "--find-links", str(wheel_cache),
        "--extra-index-url", TORCH_INDEX, *_platform_args(), "-r", str(req),
        f"opencv-python-headless=={cv}")
    report = stage.parent / f"{name}-resolve.json"
    with tempfile.TemporaryDirectory() as tmp:
        # offline check: the cache alone has a Linux x86-64 / CPython 3.13 wheel for each
        pip("install", "--dry-run", "--no-deps", "--ignore-installed", "--no-index",
            "--find-links", str(wheel_cache), "--target", tmp, "--report", str(report),
            *_platform_args(), "-r", str(req), f"opencv-python-headless=={cv}")
    chosen = json.loads(report.read_text(encoding="utf-8"))["install"]
    report.unlink()
    wheels = stage / "wheels"
    wheels.mkdir()
    for item in chosen:
        url = item["download_info"]["url"]
        src = Path(url.removeprefix("file:///").removeprefix("file://"))
        if not src.exists():                       # a file URL with %-escapes
            from urllib.parse import unquote, urlparse
            src = Path(unquote(urlparse(url).path).lstrip("/"))
        shutil.copy2(src, wheels / src.name)
    print(f"wheels   {len(chosen)} files, "
          f"{sum(p.stat().st_size for p in wheels.iterdir()) / 2**20:,.0f} MB")

    copy_pdf2xml(stage / "app", DOCKER_SKIP, ("cli.py", "__main__.py", "api.py"))
    for m in MODELS:
        if not (models / m).is_dir():
            sys.exit(f"model folder missing: {models / m}")
        shutil.copytree(models / m, stage / "models" / m)
    for f in DOCKER_FILES:
        shutil.copy2(ROOT / "deploy" / "docker" / f, stage / f)
    shutil.copy2(ROOT / "tests" / "fixtures" / "pdfs" / "simple" / "simple.pdf",
                 stage / "selftest.pdf")
    (stage / "THIRD-PARTY.txt").write_text(third_party(dists), encoding="utf-8")
    (stage / "VERSION.txt").write_text(json.dumps({
        "pdf2xml": ver, "edition": "docker API (PDF → Docling → JSON/XML → BITS/JATS)",
        "python": "3.13 (python:3.13-slim-bookworm)", "torch": "CPU",
        "built": datetime.now().isoformat(timespec="seconds"),
        "wheels": sorted(p.name for p in wheels.iterdir()), "models": MODELS},
        indent=1) + "\n", encoding="utf-8")

    out_root.mkdir(parents=True, exist_ok=True)
    target = out_root / f"{name}.zip"
    target.unlink(missing_ok=True)
    files = sorted(p for p in stage.rglob("*") if p.is_file())
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, allowZip64=True,
                         compresslevel=6) as z:
        for p in files:
            # wheels are zips already: storing them saves minutes and loses nothing
            method = zipfile.ZIP_STORED if p.suffix == ".whl" else zipfile.ZIP_DEFLATED
            z.write(p, f"{name}/{p.relative_to(stage).as_posix()}", compress_type=method)
    size = sum(p.stat().st_size for p in files) / 2**20
    print(f"staged   {stage}  ({len(files)} files, {size:,.0f} MB)")
    print(f"zip      {target}  ({target.stat().st_size / 2**20:,.0f} MB)")
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", type=Path,
                    default=Path.home() / ".cache" / "docling" / "models")
    ap.add_argument("--wheels", type=Path, default=ROOT / "build" / "wheels-linux-cp313",
                    help="download cache for the Linux wheels")
    ap.add_argument("--out", type=Path, default=ROOT / "releases")
    args = ap.parse_args()
    if sys.prefix == sys.base_prefix:
        sys.exit("run this with the project's virtual environment (.venv\\Scripts\\python.exe)")
    build(args.out, args.models, args.wheels)


if __name__ == "__main__":
    main()
