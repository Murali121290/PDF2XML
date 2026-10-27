"""The Docling cache (``11_structure_raw.json``) is reused only for the same PDF and pages."""

from __future__ import annotations

from pathlib import Path

from pdf2xml.config import Config
from pdf2xml.pipeline import cache_key, understand

PDF = Path(__file__).resolve().parents[1] / "fixtures" / "pdfs" / "simple" / "simple.pdf"


def test_cache_key_includes_the_page_selection() -> None:
    sha = "ab" * 32
    assert cache_key(sha, None) == sha                    # as older whole-document caches
    assert cache_key(sha, [3, 1, 2, 2]) == f"{sha} pages=1,2,3"
    assert cache_key(sha, [1]) != cache_key(sha, None)


def test_a_heuristic_run_drops_the_docling_stamp(tmp_path: Path) -> None:
    # the heuristic engine overwrites 11_structure_raw.json with its own format; a stamp left
    # behind would make the next Docling run read that file as Docling output
    stamp = tmp_path / "11_structure_raw.sha256"
    stamp.write_text("stale")
    understand(PDF, Config(engine="heuristic"), tmp_path)
    assert not stamp.exists()
    assert (tmp_path / "11_structure_raw.json").exists()
