"""Pipeline orchestrator: runs the stages and writes each stage's output to ``out/<doc>/``."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdf2xml import __version__
from pdf2xml.config import Config
from pdf2xml.extract.base import get_structure_engine, get_style_backend
from pdf2xml.extract.structure_docling import DoclingEngine, cache_signature
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
    quality: dict[str, Any] = field(default_factory=dict)


def _dump(path: Path, data: Any) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")
    return path


def cache_key(sha256: str, pages: Sequence[int] | None, table_mode: str = "accurate") -> str:
    """Invalidate old extraction after dependency/options or page-selection changes."""
    selection = ','.join(str(p) for p in sorted(set(pages))) if pages else "all"
    return f"{sha256} pages={selection} {cache_signature(table_mode)}"


def understand(pdf: Path, cfg: Config, out_dir: Path | None = None,
               progress: Progress | None = None, audit: dict[str, Any] | None = None) -> tuple[
        Document, StyleLayer, TriageReport, dict[str, float]]:
    """Stages 0–3: PDF → canonical Document (no files written unless ``out_dir`` is given)."""
    timings: dict[str, float] = {}
    t = time.perf_counter()

    import pikepdf
    try:
        with pikepdf.open(pdf) as source:
            count = len(source.pages)
            if count > cfg.max_pages:
                raise ConversionError(f"PDF exceeds the {cfg.max_pages}-page limit")
            if cfg.pages and (min(cfg.pages) < 1 or max(cfg.pages) > count):
                raise ConversionError("Selected pages are outside the PDF page range")
    except (pikepdf.PdfError, pikepdf.PasswordError) as e:
        raise ConversionError("PDF is invalid or requires a password") from e
    timings["preflight"] = time.perf_counter() - t
    t = time.perf_counter()

    _step(progress, "style_layer")
    style = get_style_backend(cfg.style_backend).extract(pdf, cfg.pages)
    from pdf2xml.extract.recovery import deduplicate_overprint

    overprinted = deduplicate_overprint(style)
    timings["style_layer"] = time.perf_counter() - t

    t = time.perf_counter()
    report = triage(pdf, style)
    timings["triage"] = time.perf_counter() - t
    if report.encrypted and report.pages == 0:
        raise ConversionError(f"{pdf.name}: encrypted PDF (password required)")
    if not report.born_digital:
        raise ConversionError(f"{pdf.name}: no usable text layer; " + "; ".join(report.notes))
    if out_dir is not None:
        _dump(out_dir / "00_triage.json", report.model_dump())
        if cfg.debug_artifacts:
            _dump(out_dir / "10_style_layer.json", style.to_dict())

    t = time.perf_counter()
    _step(progress, "structure_layer")
    try:
        engine = get_structure_engine(cfg.engine, cfg.models_dir)
    except ValueError as e:                 # e.g. a models folder without the layout model
        raise ConversionError(str(e)) from None
    if isinstance(engine, DoclingEngine):
        engine.table_mode = cfg.table_mode
        from pdf2xml.extract import cache as shared_cache
        shared_key = None
        shared_hit = False
        entry = None
        if cfg.cache_dir and cfg.reuse_cache:
            shared_key = shared_cache.identity(report.sha256, cfg.pages, cfg.cache_scope,
                                               engine.models_dir, cache_signature(cfg.table_mode))
        cache = None
        if out_dir is not None and (cfg.debug_artifacts or cfg.cache_dir is None):
            cache = out_dir / "11_structure_raw.json"
            stamp = out_dir / "11_structure_raw.sha256"
            if not (cfg.reuse_cache and stamp.exists()
                    and stamp.read_text().strip() == cache_key(report.sha256, cfg.pages, cfg.table_mode)):
                stamp.unlink(missing_ok=True)
                cache.unlink(missing_ok=True)
                (out_dir / "11_structure_confidence.json").unlink(missing_ok=True)
        if shared_key:
            entry = shared_cache.load(cfg.cache_dir, shared_key)
            if entry:
                try:
                    from docling_core.types.doc import DoclingDocument
                    DoclingDocument.model_validate(entry["raw"])
                    shared_hit = True
                except (ValueError, KeyError, TypeError):
                    entry = None
        structure = engine.analyze(pdf, style, cfg.pages, cache=cache,
                                   raw_data=entry["raw"] if entry else None,
                                   confidence_data=entry.get("confidence") if entry else None)
        if cfg.debug_artifacts and cache is not None and entry:
            _dump(cache, engine.last_raw)
        if out_dir is not None and engine.last_confidence is not None:
            _dump(out_dir / "11_structure_confidence.json", engine.last_confidence)
        timings["structure_cache_hit"] = float(shared_hit)
        if shared_key and not shared_hit:
            shared_cache.save(cfg.cache_dir, shared_key, {"raw": engine.last_raw,
                                                         "confidence": engine.last_confidence})
        if cache is not None and out_dir is not None:
            (out_dir / "11_structure_raw.sha256").write_text(
                cache_key(report.sha256, cfg.pages, cfg.table_mode))
    else:
        structure = engine.analyze(pdf, style, cfg.pages)
        if out_dir is not None:
            # this file is no longer Docling output: drop the stamp so Docling never reuses it
            (out_dir / "11_structure_raw.sha256").unlink(missing_ok=True)
            if cfg.debug_artifacts:
                _dump(out_dir / "11_structure_raw.json", structure.to_dict())
    timings["structure_layer"] = time.perf_counter() - t

    t = time.perf_counter()
    outline = []
    if isinstance(engine, DoclingEngine):
        from pdf2xml.extract.recovery import recover

        outline = recover(pdf, style, structure)
    timings["recovery"] = time.perf_counter() - t

    t = time.perf_counter()
    _step(progress, "merge_build")
    merged = merge(style, structure, threshold=cfg.assign_threshold, dehyphenate=cfg.dehyphenate)
    if audit is not None:
        audit["dehyphenated_by_page"] = merged.dehyphenated_by_page
    n_artifacts = detect_artifacts(merged.blocks, {p.number: p.frame for p in style.pages})
    cat = build_catalogue(merged.blocks)

    meta = Meta(
        source=pdf.name, sha256=report.sha256, pages=report.pages,
        doc_type=report.doc_type_guess,  # type: ignore[arg-type]
        lang=cfg.lang, tagged=report.tagged, engine=engine.name, style_backend=style.backend,
        warnings=[*report.notes, *merged.warnings],
        stats={"dehyphenated": merged.dehyphenated, "orphan_words": merged.orphans,
               "artifacts": n_artifacts, "promoted_headings": cat.promoted},
        outline=outline or None,
    )
    if overprinted:
        meta.stats["duplicate_words_removed"] = overprinted
    pages = [PageInfo(n=p.number, width=p.width, height=p.height) for p in style.pages]
    doc = build_document(merged.blocks, cat, meta, pages, style.pages, structure.regions)
    timings["merge_build"] = time.perf_counter() - t
    return doc, style, report, timings


def convert(pdf: Path, out_root: Path, cfg: Config | None = None,
            progress: Progress | None = None) -> Result:
    cfg = cfg or Config()
    total_started = time.perf_counter()
    pdf = pdf.resolve()
    out_dir = out_root / pdf.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    audit: dict[str, Any] = {}
    doc, style, report, timings = understand(pdf, cfg, out_dir, progress, audit)
    _dump(out_dir / 'source.json', {'path': str(pdf), 'sha256': doc.meta.sha256})
    files: dict[str, Path] = {}

    t = time.perf_counter()
    _step(progress, "figures")
    from pdf2xml.export.assets import extract_figures

    extract_figures(pdf, doc, out_dir, scale=cfg.image_scale)
    timings["figures"] = time.perf_counter() - t

    t = time.perf_counter()
    coverage = check_coverage(style, doc)
    timings["coverage"] = time.perf_counter() - t
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

    content_summary = None
    content_errors: list[str] = []
    if "content" in cfg.targets:
        from pdf2xml.xml.content import write_content

        t = time.perf_counter()
        _step(progress, "content")
        files["content"], content_errors = write_content(doc, out_dir)
        content_summary = {"file": files["content"].name, "schema": "content (RelaxNG)",
                           "valid": not content_errors, "errors": content_errors}
        timings["content"] = time.perf_counter() - t

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
    exports = publish(doc, out_dir, cfg, timings, pdf)
    files.update({k: r.path for k, r in exports.items()})

    completeness: dict[str, Any] | None = None
    completeness_details: dict[str, Any] | None = None
    if cfg.completeness:
        from pdf2xml.qa import completeness as qa_completeness

        t = time.perf_counter()
        _step(progress, "completeness")
        result = qa_completeness.check(pdf, style, doc, exports, content_path=files.get("content"))
        completeness_details = result
        files["completeness"] = qa_completeness.write(result, out_dir)
        completeness = qa_completeness.summary(result)
        timings["completeness"] = time.perf_counter() - t

    if cfg.overlays:
        from pdf2xml.qa.overlay import render_overlays

        t = time.perf_counter()
        _step(progress, "overlays")
        render_overlays(pdf, doc, out_dir / "qa")
        timings["overlays"] = time.perf_counter() - t

    from pdf2xml.qa.structure import check as check_structure

    t = time.perf_counter()
    quality = check_structure(doc, exports, out_dir, coverage_ok=coverage.ok,
                              xml_errors=xml_errors, completeness=completeness,
                              content_errors=content_errors)
    timings["quality"] = time.perf_counter() - t
    from pdf2xml.qa.report import build as build_quality_report, html_report
    t = time.perf_counter()
    quality = build_quality_report(pdf, doc, style, coverage, quality, completeness_details,
                                   list(cfg.targets), exports, xml_errors, content_errors,
                                   timings, out_dir, cfg.pages, audit.get("dehyphenated_by_page"))
    timings["reporting"] = time.perf_counter() - t
    timings["total"] = time.perf_counter() - total_started
    quality["runtime"]["stage_seconds"] = dict(timings)
    files["quality"] = _dump(out_dir / "quality.json", quality)
    files["quality_html"] = out_dir / "quality_report.html"
    files["quality_html"].write_text(html_report(quality), encoding="utf-8")
    timings["total"] = time.perf_counter() - total_started
    _dump(out_dir / "report.json", {
        "version": __version__,
        "structure_runtime": cache_signature(cfg.table_mode) if doc.meta.engine == "docling" else None,
        "source": pdf.name,
        "engine": doc.meta.engine,
        "coverage": {"ok": coverage.ok, "ratio": round(coverage.ratio, 5),
                     "missing": coverage.missing, "extra": coverage.extra},
        "xml_valid": not xml_errors,
        "xml_errors": xml_errors,
        "warnings": doc.meta.warnings,
        "stats": doc.meta.stats,
        "styles": sorted(doc.styles),
        "blocks": len(doc.body),
        "exports": export_summary(exports),
        **({"content_xml": content_summary} if content_summary is not None else {}),
        "quality": quality,
        **({"completeness": completeness} if completeness is not None else {}),
        "timings_s": {k: round(v, 3) for k, v in timings.items()},
    })
    return Result(out_dir, doc, report, coverage, xml_errors, files, timings, exports, quality)


def publish(doc: Document, out_dir: Path, cfg: Config,
            timings: dict[str, float] | None = None,
            pdf: Path | None = None) -> dict[str, ExportResult]:
    """Write the BITS / JATS exports requested in ``cfg.targets``. ``pdf``: the source, for
    GROBID's reading of the authors (a re-export without it uses ``12_grobid.json``)."""
    from pdf2xml.publishing import export, load_metadata, load_profile

    wanted = [t for t in ("bits", "jats") if t in cfg.targets]
    if not wanted:
        return {}
    profile = load_profile(cfg.profile)
    meta = load_metadata(cfg.metadata)
    grobid = None
    if cfg.grobid_url:
        # imported only when asked for: the deploy package leaves GROBID out
        from pdf2xml.publishing.grobid import CACHE_FILE, Grobid, with_authors

        t = time.perf_counter()
        grobid = Grobid(cfg.grobid_url, out_dir / CACHE_FILE, reuse=cfg.reuse_cache)
        if profile.grobid_header and not meta.part.contributors:
            meta = with_authors(meta, grobid.header(pdf, doc.meta.sha256), doc)
        if timings is not None:
            timings["grobid_header"] = time.perf_counter() - t
    out: dict[str, ExportResult] = {}
    for target in wanted:
        t = time.perf_counter()
        res = export(doc, out_dir, target, profile, meta, grobid=grobid)
        if not res.valid:
            log.error("%s export is not DTD-valid (%s): %s", target, res.schema, res.errors[:3])
        out[target] = res
        if timings is not None:
            timings[target] = time.perf_counter() - t
    if grobid is not None:
        grobid.save()
    return out


def export_summary(exports: dict[str, ExportResult]) -> dict[str, Any]:
    return {k: {"file": r.path.name, "schema": r.schema, "valid": r.valid,
                "errors": r.errors, "warnings": r.warnings, "stats": r.stats,
                "intentional_omissions": r.dropped, "diagnostics": r.diagnostics}
            for k, r in exports.items()}
