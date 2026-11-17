"""Prepare pinned Docling models before deployment; never called by the running API."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "models" / "manifest.json"


def file_errors(root: Path, models: list[dict[str, Any]]) -> list[str]:
    errors = []
    for model in models:
        for name, expected in model["files"].items():
            path = root / model["folder"] / name
            if not path.is_file() or path.stat().st_size != expected["size"]:
                errors.append(f"missing or wrong size: {model['folder']}/{name}")
                continue
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != expected["sha256"]:
                errors.append(f"checksum mismatch: {model['folder']}/{name}")
    return errors


def download_model(model: dict[str, Any], destination: Path) -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=model["repo_id"], revision=model["revision"],
        local_dir=destination / model["folder"], allow_patterns=list(model["files"]),
    )


def prepare_models(destination: Path, *, source: Path | None = None, check: bool = False,
                   manifest_path: Path | None = None) -> None:
    models = json.loads((manifest_path or MANIFEST).read_text(encoding="utf-8"))["models"]
    if source is not None:
        errors = file_errors(source, models)
        if errors:
            raise RuntimeError(
                "Existing model folder is incomplete or corrupt:\n" + "\n".join(errors)
            )
        if source.resolve() != destination.resolve():
            for model in models:
                for name in model["files"]:
                    target = destination / model["folder"] / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source / model["folder"] / name, target)

    errors = file_errors(destination, models)
    if errors and check:
        raise RuntimeError("Local model verification failed:\n" + "\n".join(errors))
    if errors:
        for model in models:
            if file_errors(destination, [model]):
                print(f"Preparing {model['repo_id']} at {model['revision']}", flush=True)
                download_model(model, destination)
        errors = file_errors(destination, models)
        if errors:
            raise RuntimeError("Downloaded model verification failed:\n" + "\n".join(errors))
    print(f"Verified local Docling models: {destination.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "models")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--source", type=Path,
                      help="Copy verified existing models without downloading")
    mode.add_argument("--check", action="store_true",
                      help="Verify local models without downloading")
    args = parser.parse_args()
    try:
        prepare_models(args.output, source=args.source, check=args.check)
    except RuntimeError as e:
        parser.exit(1, f"{e}\n")


if __name__ == "__main__":
    main()
