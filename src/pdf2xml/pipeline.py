"""Pipeline orchestrator: runs the stages and writes each stage's output to ``out/<doc>/``."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdf2xml.config import Config
from pdf2xml.extract.base import get_structure_engine, get_style_backend
from pdf2xml.extract.structure_docling import DoclingEngine
from pdf2xml.extract.types import StyleLayer
from pdf2xml.merge.artifacts import detect_artifacts
from pdf2xml.merge.build import build_document
from pdf2xml.merge.merger import merge
from pdf2xml.model import Document, Meta, PageInfo
from pdf2xml.publishing import ExportResult
from pdf2xml.qa.coverage import Coverage, check_coverage
from pdf2xml.styles.catalogue import build_catalogue
from pdf2xml.triage import TriageReport, triage

log = logging.getLogger(__name__)

# Called with the name of each stage as it starts ("style_layer", "structure_layer", …), so a
# caller such as the conversion job queue can show where a long conversion is.
Progress = Callable[[str], None]


def _step(progress: Progress | None, stage: str) -> None:
    if progress is not None:
        progress(stage)


class ConversionError(RuntimeError):
    pass


@dataclass
class Result:
    out_dir: Path
    document: Document
    triage: TriageReport
    coverage: Coverage
    xml_errors: list[str] = field(default_factory=list)
    files: dict[str, Path] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    exports: dict[str, ExportResult] = field(default_factory=dict)   # "bits" / "jats"


def _dump(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")
    return path


def understand(pdf: Path, cfg: Config, out_dir: Path | None = None,
               progress: Progress | None = None) -> tuple[
        Document, StyleLayer, TriageReport, dict[str, float]]:
    """Stages 0–3: PDF → canonical Document (no files written unless ``out_dir`` is given)."""
    timings: dict[str, float] = {}
    t = time.perf_counter()

    _step(progress, "style_layer")
    style = get_style_backend(cfg.style_backend).extract(pdf, cfg.pages)
    timings["style_layer"] = time.perf_counter() - t

    report = triage(pdf, style)
    if report.encrypted and report.pages == 0:
        raise ConversionError(f"{pdf.name}: encrypted PDF (password required)")
    if not report.born_digital:
        raise ConversionError(f"{pdf.name}: no usable text layer; " + "; ".join(report.notes))
    if out_dir is not None:
        _dump(out_dir / "00_triage.json", report.model_dump())
        _dump(out_dir / "10_style_layer.json", style.to_dict())

    t = time.perf_counter()
    _step(progress, "structure_layer")
    engine = get_structure_engine(cfg.engine)
    if isinstance(engine, DoclingEngine):
        cache = None
        if out_dir is not None:
            cache = out_dir / "11_structure_raw.json"
            stamp = out_dir / "11_structure_raw.sha256"
            if not (cfg.reuse_cache and stamp.exists()
                    and stamp.read_text().strip() == report.sha256):
                cache.unlink(missing_ok=True)
        structure = engine.analyze(pdf, style, cfg.pages, cache=cache)
        if out_dir is not None:
            (out_dir / "11_structure_raw.sha256").write_text(report.sha256)
    else:
        structure = engine.analyze(pdf, style, cfg.pages)
        if out_dir is not None:
            _dump(out_dir / "11_structure_raw.json", structure.to_dict())
    timings["structure_layer"] = time.perf_counter() - t

    t = time.perf_counter()
    _step(progress, "merge_build")
    merged = merge(style, structure, threshold=cfg.assign_threshold, dehyphenate=cfg.dehyphenate)
    n_artifacts = detect_artifacts(merged.blocks, {p.number: p.frame for p in style.pages})
    cat = build_catalogue(merged.blocks)

    meta = Meta(
        source=pdf.name, sha256=report.sha256, pages=report.pages,
        doc_type=report.doc_type_guess,  # type: ignore[arg-type]
        lang=cfg.lang, tagged=report.tagged, engine=engine.name, style_backend=style.backend,
        warnings=[*report.notes, *merged.warnings],
        stats={"dehyphenated": merged.dehyphenated, "orphan_words": merged.orphans,
               "artifacts": n_artifacts, "promoted_headings": cat.promoted},
    )
    pages = [PageInfo(n=p.number, width=p.width, height=p.height) for p in style.pages]
    doc = build_document(merged.blocks, cat, meta, pages, style.pages)
    timings["merge_build"] = time.perf_counter() - t
    return doc, style, report, timings


def convert(pdf: Path, out_root: Path, cfg: Config | None = None,
            progress: Progress | None = None) -> Result:
    cfg = cfg or Config()
    pdf = pdf.resolve()
    out_dir = out_root / pdf.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    doc, style, report, timings = understand(pdf, cfg, out_dir, progress)
    files: dict[str, Path] = {}

    t = time.perf_counter()
    _step(progress, "figures")
    from pdf2xml.export.assets import extract_figures

    extract_figures(pdf, doc, out_dir, scale=cfg.image_scale)
    timings["figures"] = time.perf_counter() - t

    coverage = check_coverage(style, doc)
    if not coverage.ok:
        doc.meta.warnings.append(
            f"text coverage {coverage.ratio:.4f}: missing={coverage.missing} "
            f"extra={coverage.extra}"
        )

    files["json"] = _dump(out_dir / "20_canonical.json",
                          doc.model_dump(mode="json", exclude_none=True))

    from pdf2xml.xml.serialize import to_xml, validate, write_xml

    t = time.perf_counter()
    _step(progress, "xml")
    root = to_xml(doc)
    xml_errors = validate(root)
    if xml_errors:
        log.error("canonical XML is not schema-valid: %s", xml_errors[:5])
    if "xml" in cfg.targets or "epub" in cfg.targets:
        write_xml(root, out_dir / "30_canonical.xml")
        files["xml"] = out_dir / "30_canonical.xml"
    timings["xml"] = time.perf_counter() - t

    if "epub" in cfg.targets:
        from pdf2xml.export.epub import write_epub

        t = time.perf_counter()
        _step(progress, "epub")
        if cfg.epub_layout == "fixed":
            doc.meta.warnings.append("fixed-layout EPUB is Phase 5; wrote reflowable EPUB")
        files["epub"] = write_epub(doc, root, out_dir, out_dir / "50_book.epub")
        timings["epub"] = time.perf_counter() - t

    if "docx" in cfg.targets:
        from pdf2xml.export.docx import write_docx

        t = time.perf_counter()
        _step(progress, "docx")
        files["docx"] = write_docx(doc, out_dir, out_dir / "51_document.docx")
        timings["docx"] = time.perf_counter() - t

    if "bits" in cfg.targets or "jats" in cfg.targets:
        _step(progress, "bits_jats")
    exports = publish(doc, out_dir, cfg, timings)
    files.update({k: r.path for k, r in exports.items()})

    if cfg.overlays:
        from pdf2xml.qa.overlay import render_overlays

        t = time.perf_counter()
        _step(progress, "overlays")
        render_overlays(pdf, doc, out_dir / "qa")
        timings["overlays"] = time.perf_counter() - t

    _dump(out_dir / "report.json", {
        "source": pdf.name,
        "engine": doc.meta.engine,
        "coverage": {"ok": coverage.ok, "ratio": round(coverage.ratio, 5),
                     "missing": coverage.missing, "extra": coverage.extra},
        "xml_valid": not xml_errors,
        "xml_errors": xml_errors[:20],
        "warnings": doc.meta.warnings,
        "stats": doc.meta.stats,
        "styles": sorted(doc.styles),
        "blocks": len(doc.body),
        "exports": export_summary(exports),
        "timings_s": {k: round(v, 3) for k, v in timings.items()},
    })
    return Result(out_dir, doc, report, coverage, xml_errors, files, timings, exports)


def publish(doc: Document, out_dir: Path, cfg: Config,
            timings: dict[str, float] | None = None) -> dict[str, ExportResult]:
    """Write the BITS / JATS exports requested in ``cfg.targets``."""
    from pdf2xml.publishing import export, load_metadata, load_profile

    wanted = [t for t in ("bits", "jats") if t in cfg.targets]
    if not wanted:
        return {}
    profile = load_profile(cfg.profile)
    meta = load_metadata(cfg.metadata)
    out: dict[str, ExportResult] = {}
    for target in wanted:
        t = time.perf_counter()
        res = export(doc, out_dir, target, profile, meta)
        if not res.valid:
            log.error("%s export is not DTD-valid (%s): %s", target, res.schema, res.errors[:3])
        out[target] = res
        if timings is not None:
            timings[target] = time.perf_counter() - t
    return out


def export_summary(exports: dict[str, ExportResult]) -> dict[str, Any]:
    return {k: {"file": r.path.name, "schema": r.schema, "valid": r.valid,
                "errors": r.errors[:20], "warnings": r.warnings, "stats": r.stats}
            for k, r in exports.items()}
