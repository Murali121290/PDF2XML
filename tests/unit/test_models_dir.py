"""Docling's models from a local folder (offline use): which folder, and early clear errors."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pdf2xml.cli import app
from pdf2xml.config import Config
from pdf2xml.extract import structure_docling as sd
from pdf2xml.pipeline import ConversionError, understand
from tests.conftest import PDFS

SIMPLE = PDFS / "simple" / "simple.pdf"


def _models(root: Path, *kinds: str) -> Path:
    for kind in kinds:
        (root / sd.REQUIRED_MODELS[kind]).mkdir(parents=True)
        for name in sd.REQUIRED_FILES[kind]:
            path = root / sd.REQUIRED_MODELS[kind] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test model file")
    return root


def test_which_models_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    full = _models(tmp_path / "full", "layout", "tableformer")
    half = _models(tmp_path / "half", "layout")
    monkeypatch.delenv(sd.MODELS_ENV, raising=False)
    monkeypatch.setattr(sd, "DEFAULT_MODELS_DIR", tmp_path / "none")
    monkeypatch.setattr(sd, "CACHED_MODELS_DIR", tmp_path / "empty-cache")
    with pytest.raises(ValueError, match="Runtime model downloads are disabled"):
        sd.resolve_models_dir()
    assert sd.resolve_models_dir(full) == full                # --models-dir
    monkeypatch.setenv(sd.MODELS_ENV, str(full))
    assert sd.resolve_models_dir() == full                    # $PDF2XML_MODELS_DIR
    monkeypatch.delenv(sd.MODELS_ENV)
    monkeypatch.setattr(sd, "DEFAULT_MODELS_DIR", full)
    assert sd.resolve_models_dir() == full                    # `docling-tools` filled it
    monkeypatch.setattr(sd, "DEFAULT_MODELS_DIR", half)
    with pytest.raises(ValueError, match="Runtime model downloads are disabled"):
        sd.resolve_models_dir()
    monkeypatch.setattr(sd, "CACHED_MODELS_DIR", full)
    assert sd.resolve_models_dir() == full                    # existing local cache
    with pytest.raises(ValueError, match="missing tableformer model"):
        sd.resolve_models_dir(half)                           # asked for: an error


def test_docling_options_use_the_folder() -> None:
    pytest.importorskip("docling")
    opts = sd.pipeline_options("C:/models")
    assert (str(opts.artifacts_path), opts.do_ocr, opts.do_table_structure) == (
        "C:/models", False, True)
    assert sd.pipeline_options(None).artifacts_path is None


def test_a_bad_models_folder_is_reported_before_the_models_load(tmp_path: Path) -> None:
    half = _models(tmp_path / "half", "layout")
    res = CliRunner().invoke(app, ["convert", str(SIMPLE), "-o", str(tmp_path / "out"),
                                   "--engine", "docling", "--models-dir", str(half)])
    assert res.exit_code == 2 and "missing tableformer model" in res.output
    with pytest.raises(ConversionError, match="missing tableformer model"):
        understand(SIMPLE, Config(engine="docling", models_dir=half))


@pytest.mark.parametrize("engine", ["heuristic", "auto"])
def test_non_ml_cli_conversion_does_not_require_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str,
) -> None:
    from pdf2xml.extract import base

    monkeypatch.delenv(sd.MODELS_ENV, raising=False)
    monkeypatch.setattr(sd, "DEFAULT_MODELS_DIR", tmp_path / "no-project-models")
    monkeypatch.setattr(sd, "CACHED_MODELS_DIR", tmp_path / "no-cached-models")
    monkeypatch.setattr(base, "docling_available", lambda: False)
    output = tmp_path / "out"
    result = CliRunner().invoke(app, [
        "convert", str(SIMPLE), "-o", str(output), "--engine", engine,
        "--to", "json,xml", "--no-overlays", "--no-completeness",
    ])
    assert result.exit_code == 0, result.output
    assert (output / "simple/30_canonical.xml").is_file()
