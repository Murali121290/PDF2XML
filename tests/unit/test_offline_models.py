"""Offline deployment must fail locally rather than attempting runtime downloads."""

import hashlib
import json
from pathlib import Path

import pytest

from pdf2xml.extract import structure_docling
from tools import prepare_models


def test_missing_local_models_never_fall_back_to_a_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PDF2XML_MODELS_DIR", raising=False)
    monkeypatch.setattr(structure_docling, "DEFAULT_MODELS_DIR", tmp_path / "absent-project")
    monkeypatch.setattr(structure_docling, "CACHED_MODELS_DIR", tmp_path / "empty-cache")
    with pytest.raises(ValueError, match="Runtime model downloads are disabled"):
        structure_docling.DoclingEngine()


def manifest(tmp_path: Path) -> tuple[Path, bytes]:
    data = b"a small test model"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"models": [{
        "repo_id": "example/model", "folder": "example--model", "revision": "pinned-revision",
        "files": {"weights.bin": {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}},
    }]}))
    return path, data


def test_copy_and_verify_existing_models_without_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, data = manifest(tmp_path)
    source = tmp_path / "source"
    weights = source / "example--model/weights.bin"
    weights.parent.mkdir(parents=True)
    weights.write_bytes(data)

    def no_download(*args):
        pytest.fail("Existing local models must not trigger a download")

    monkeypatch.setattr(prepare_models, "download_model", no_download)
    output = tmp_path / "output"
    prepare_models.prepare_models(output, source=source, manifest_path=manifest_path)
    prepare_models.prepare_models(output, check=True, manifest_path=manifest_path)
    assert (output / "example--model/weights.bin").read_bytes() == data


def test_check_mode_rejects_missing_and_corrupt_models_without_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, data = manifest(tmp_path)

    def no_download(*args):
        pytest.fail("Check mode must not trigger a download")

    monkeypatch.setattr(prepare_models, "download_model", no_download)
    output = tmp_path / "output"
    with pytest.raises(RuntimeError, match="missing or wrong size"):
        prepare_models.prepare_models(output, check=True, manifest_path=manifest_path)
    weights = output / "example--model/weights.bin"
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b"X" * len(data))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        prepare_models.prepare_models(output, check=True, manifest_path=manifest_path)
