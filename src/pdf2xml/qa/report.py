"""Complete, versioned conversion audit. A report is evidence, not editorial certification."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import hashlib
import json
import re
from typing import Any

from pdf2xml.extract.types import StyleLayer
from pdf2xml.model import Document, Table, ListItem, iter_blocks
from pdf2xml.qa.coverage import Coverage, _norm


def build(pdf: Path, doc: Document, style: StyleLayer, coverage: Coverage,
          quality: dict[str, Any], completeness: dict[str, Any] | None,
          targets: list[str], exports: dict[str, Any], xml_errors: list[str],
          content_errors: list[str], timings: dict[str, float], out_dir: Path,
          pages: list[int] | None = None, dehyphenated: dict[int, int] | None = None) -> dict[str, Any]:
    blocks = [b for part in (doc.body, doc.footnotes, doc.references) for b in iter_blocks(part)]
    by_page = defaultdict(list)
    output = defaultdict(Counter)
    output_words = 0
    for b in blocks:
        by_page[b.page].append(b)
        text = (" ".join(r.text for row in b.rows for c in row for r in c.runs)
                if isinstance(b, Table) else " ".join(r.text for r in getattr(b, "runs", [])))
        if isinstance(b, ListItem) and b.marker:
            text += " " + b.marker
        output[b.page].update(_norm(text))
        output_words += len(text.split())
    artifacts = defaultdict(list)
    for a in doc.artifacts:
        artifacts[a.page].append(a.model_dump(mode="json"))
        output[a.page].update(_norm(a.text))
        output_words += len(a.text.split())
    expected = set(pages if pages is not None else range(1, doc.meta.pages + 1))
    extracted = {p.number for p in style.pages}
    canonical = {p.n for p in doc.pages}
    checks = (completeness or {}).get("checks", {})
    issues = list(quality.get("issues", []))
    if completeness is None:
        issues.append({"code": "completeness_not_run", "severity": "warn",
                       "message": "Independent text, image, font and uncovered-ink checks were not run."})
    inventory = []
    for p in style.pages:
        source = _norm(" ".join(w.text for w in p.words))
        actual = output[p.number].copy()
        removed = (dehyphenated or {}).get(p.number, 0)
        actual["-"] += removed
        missing, extra = source - actual, actual - source
        row = {"page": p.number, "included": p.number in expected,
               "source_words": len(p.words), "source_characters": sum(source.values()),
               "output_characters": sum(output[p.number].values()),
               "allowed_removed_line_hyphens": removed,
               "missing_characters": dict(missing), "extra_characters": dict(extra),
               "character_coverage_ok": not missing and not extra,
               "source_image_objects": len(p.images), "source_drawings": len(p.drawings),
               "block_counts": dict(Counter(b.type for b in by_page[p.number])),
               "artifacts": artifacts[p.number],
               "fonts": sorted({w.font for w in p.words}),
               "issues": [i for i in issues if i.get("page") == p.number],
               "text_status": "text_extracted" if p.words else "no_extractable_text",
               "canonical_page_present": p.number in canonical}
        inventory.append(row)
        evidence = {}
        for name, check in checks.items():
            matches = [entry for key in ("pages", "missed", "minor", "figures_without_file")
                       for entry in check.get(key, []) if isinstance(entry, dict) and entry.get("page") == p.number]
            if matches:
                evidence[name] = {"check_status": check.get("status"), "findings": matches}
        row["completeness_evidence"] = evidence
        row["font_risks"] = [{"name": f["name"], "risk": f["risk"]}
                             for f in checks.get("unicode", {}).get("fonts", [])
                             if f.get("risk") and p.number in f.get("pages", [])]
        if (missing or extra) and not any(i.get("code") == "page_text_coverage" and i.get("page") == p.number for i in issues):
            issue = {"code": "page_text_coverage", "severity": "error", "page": p.number,
                     "message": f"Character mismatch: {sum(missing.values())} missing, {sum(extra.values())} extra.",
                     "missing": dict(missing), "extra": dict(extra)}
            issues.append(issue)
            row["issues"].append(issue)
    missing_pages = sorted(expected - extracted | expected - canonical)
    for page in missing_pages:
        issues.append({"code": "missing_page", "severity": "error", "page": page,
                       "message": "Requested page is absent from extraction or the canonical page inventory."})
    validations = {}
    assets = []
    for block in blocks:
        image = getattr(block, "image", None)
        if image:
            path = (out_dir / image).resolve()
            contained = path.is_relative_to(out_dir.resolve())
            exists = contained and path.is_file()
            assets.append({"page": block.page, "block": block.id, "file": image,
                           "status": "present" if exists else "missing_or_outside_output"})
            if not exists:
                issues.append({"code": "missing_asset_file", "severity": "error", "page": block.page,
                               "block": block.id, "message": f"Referenced asset is missing or outside the output folder: {image}"})
    filenames = {"canonical": "30_canonical.xml", "bits": "40_bits.xml", "jats": "41_jats.xml", "content": "42_content.xml"}
    for target in filenames:
        requested = ("xml" in targets or "epub" in targets) if target == "canonical" else target in targets
        res = exports.get(target)
        errors = (xml_errors if target == "canonical" else content_errors if target == "content" else res.errors if res else [])
        schema = res.schema if res else "application canonical Relax NG" if target == "canonical" else "application Content XML Relax NG" if target == "content" else "BITS 2.2" if target == "bits" else "JATS 1.4"
        path = out_dir / filenames[target]
        generated = requested and path.is_file()
        state = ("failed" if errors else "passed") if target == "canonical" else "not_requested" if not requested else "not_generated" if not generated else "failed" if errors else "passed"
        validation = {"target": target, "status": state, "schema": schema,
                      "requested": requested, "file": path.name if generated else None,
                      "generated": generated, "validation_performed": target == "canonical" or generated,
                      "error_count": len(errors), "errors": [diagnostic(e) for e in errors],
                      "diagnostic_scope": "All diagnostics returned by the schema validator; a fatal parser error can stop further validation.",
                      "warnings": res.warnings if res else [],
                      "statistics": res.stats if res else {},
                      "diagnostics": getattr(res, "diagnostics", {}) if res else {},
                      "intentional_omissions": [{"reason": reason, "page": page, "text": text}
                                                for reason, page, text in res.dropped] if res else [],
                      "completeness": checks.get(f"export_{target}", {"status": "not_run"}),
                      "scope": "Schema validity does not establish content completeness or editorial correctness."}
        if generated:
            validation.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        validations[target] = validation
        (out_dir / f"validation_{target}.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
        if state == "not_generated":
            issues.append({"code": "missing_output", "severity": "error", "target": target,
                           "message": "Requested output was not generated."})
    versions = {}
    for package in ("pdf2xml", "docling", "docling-core", "pdfplumber", "pypdfium2", "lxml"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = "not_installed"
    severity = Counter(i["severity"] for i in issues)
    result = {**quality, "issues": issues, "errors": severity["error"], "warnings": severity["warn"],
              "issue_count": len(issues), "status": "needs_review" if issues or completeness is None else "passed",
              "report_schema": "pdf2xml-conversion-quality/1.0", "generated_at": datetime.now(timezone.utc).isoformat(),
              "source": {"filename": pdf.name, "sha256": doc.meta.sha256, "bytes": pdf.stat().st_size,
                         "total_pdf_pages": doc.meta.pages},
              "runtime": {"engine": doc.meta.engine, "style_backend": doc.meta.style_backend,
                          "versions": versions, "stage_seconds": dict(timings)},
              "page_inventory": {"requested": sorted(expected), "extracted": sorted(extracted),
                                 "canonical": sorted(canonical), "missing": missing_pages,
                                 "excluded_by_selection": sorted(set(range(1, doc.meta.pages + 1)) - expected)},
              "statistics": {"processed_pages": len(style.pages), "block_counts": dict(Counter(b.type for b in blocks)),
                             "paragraphs": sum(b.type == "paragraph" for b in blocks),
                             "headings": sum(b.type == "heading" for b in blocks),
                             "tables": sum(b.type == "table" for b in blocks),
                             "figures": sum(b.type == "figure" for b in blocks),
                             "formulas": sum(b.type == "formula" for b in blocks),
                             "asset_files": len(assets), "output_words_including_artifacts": output_words,
                             "source_words": sum(len(p.words) for p in style.pages),
                             "source_characters": coverage.source_chars,
                             "output_characters": sum(sum(chars.values()) for chars in output.values()),
                             "output_characters_after_normalization_allowance": coverage.output_chars,
                             "removed_line_hyphens": doc.meta.stats.get("dehyphenated", 0),
                             "artifacts": len(doc.artifacts), "artifact_types": dict(Counter(a.kind for a in doc.artifacts)),
                             "source_image_objects": sum(len(p.images) for p in style.pages),
                             "fonts_used": len({w.font for p in style.pages for w in p.words})},
              "coverage": {"ok": coverage.ok, "ratio": coverage.ratio, "missing": coverage.missing, "extra": coverage.extra},
              "pages": inventory, "validations": validations,
              "completeness": completeness or {"status": "not_run", "reason": "Completeness checks disabled"},
              "font_audit": checks.get("unicode", {"status": "not_run"}),
              "font_usage": [{"name": font, "words": sum(w.font == font for p in style.pages for w in p.words),
                              "pages": [p.number for p in style.pages if any(w.font == font for w in p.words)],
                              "sizes_pt": sorted({w.size for p in style.pages for w in p.words if w.font == font})}
                             for font in sorted({w.font for p in style.pages for w in p.words})],
              "asset_files": assets,
              "canonical_styles": {name: value.model_dump(mode="json", exclude_none=True)
                                   for name, value in doc.styles.items()},
              "verification_scope": {"missing_page_inventory": "failed" if missing_pages else "passed",
                                     "native_text_character_preservation": "passed" if coverage.ok else "failed",
                                     "independent_text_reader": checks.get("text_layer", {}).get("status", "not_run"),
                                     "image_preservation": checks.get("images", {}).get("status", "not_run"),
                                     "uncovered_ink": checks.get("ink", {}).get("status", "not_run"),
                                     "font_unicode_mapping": checks.get("unicode", {}).get("status", "not_run"),
                                     "visual_font_fidelity": "not_verified",
                                     "reading_order_against_approved_reference": "not_run",
                                     "editorial_approval": "not_verified"},
              "warnings_from_pipeline": doc.meta.warnings,
              "methodology": {"characters": "Normalized non-whitespace Unicode character multiset; ligatures expanded and soft hyphens removed. Removed line hyphens are accounted for separately. Includes preserved artifacts.",
                              "pages": "Physical PDF page numbers, starting at 1. No-text pages are not assumed blank.",
                              "words": "Source words are extractor tokens; output words use whitespace tokenization and include artifacts. Hyphenation and tokenization mean counts need not be equal.",
                              "images": "Source image objects and output figure blocks are different measurements; vector artwork may have no image object.",
                              "artifacts": "Headers, footers, page numbers and figure text remain in canonical artifacts and this report. Publishing outputs may omit them intentionally; omission ledgers are shown separately.",
                              "fonts": "PDF font embedding/Unicode mapping and suspicious characters are audited; visual typography fidelity is not certified.",
                              "standards": "BITS/JATS use the bundled schema identified in each validation record; canonical and Content XML use application schemas. This report is not an ISO certification.",
                              "limits": "Automated checks cannot prove absence of all missing visual content, correct reading order or editorial accuracy. Review flagged pages and not-run checks."}}
    return result


def failure_report(filename: str, message: str, stage: str | None, targets: list[str]) -> dict[str, Any]:
    return {"report_schema": "pdf2xml-conversion-quality/1.0", "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "failed", "errors": 1, "warnings": 0, "issue_count": 1,
            "source": {"original_filename": filename, "total_pdf_pages": None},
            "statistics": {"processed_pages": None, "source_words": None, "source_characters": None},
            "runtime": {"last_stage": stage}, "page_inventory": {"status": "not_available"}, "pages": [],
            "coverage": {"status": "not_run"}, "font_audit": {"status": "not_run"},
            "completeness": {"status": "not_run", "reason": "Conversion did not complete"},
            "issues": [{"code": "conversion_failed", "severity": "error", "stage": stage, "message": message}],
            "validations": {target: {"status": "not_run" if (target in targets or target == "canonical" and "xml" in targets) else "not_requested",
                                      "validation_performed": False, "reason": "Conversion did not reach report generation"}
                            for target in ("canonical", "bits", "jats", "content")},
            "methodology": {"limits": "Unknown counts are not zero. No successful validation is implied by an unavailable check."}}


def diagnostic(message: str) -> dict[str, Any]:
    match = re.match(r"line (\d+): (.*)", message, re.S)
    return {"line": int(match[1]) if match else None, "message": match[2] if match else message}


def refresh_exports(out_dir: Path, doc: Document, exports: dict[str, Any],
                    content_errors: list[str] | None = None) -> None:
    """Re-export changes output validation only; never imply the PDF was re-audited."""
    from pdf2xml.qa.completeness import _run
    from pdf2xml.qa.exports import check_export
    path = out_dir / "quality.json"
    quality = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    updates = {target: (res.path, res.schema, res.errors, res.warnings, res.stats, res.dropped)
               for target, res in exports.items()}
    if content_errors is not None:
        updates["content"] = (out_dir / "42_content.xml", "application Content XML Relax NG", content_errors, [], {}, [])
    for target, (output, schema, errors, warnings, stats, dropped) in updates.items():
        evidence = _run(lambda: check_export(doc, output, target, dropped).to_json())
        validation = {"target": target, "status": "failed" if errors else "passed",
                      "schema": schema, "file": output.name, "requested": True, "generated": output.is_file(),
                      "validation_performed": True, "error_count": len(errors), "errors": [diagnostic(e) for e in errors],
                      "warnings": warnings, "statistics": stats, "completeness": evidence,
                      "intentional_omissions": [{"reason": r, "page": p, "text": t} for r, p, t in dropped],
                      "bytes": output.stat().st_size, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                      "scope": "Re-export from canonical data; source PDF/font/page audits were not rerun."}
        validation["diagnostics"] = getattr(exports.get(target), "diagnostics", {})
        quality.setdefault("validations", {})[target] = validation
        (out_dir / f"validation_{target}.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
        issues = quality.setdefault("issues", [])
        issues[:] = [i for i in issues if i.get("target") != target and i.get("code") != f"completeness_export_{target}"]
        if errors:
            issues.append({"code": "export_schema", "target": target, "severity": "error", "message": f"{target} failed schema validation; see its full validation report."})
        if evidence["status"] in ("warn", "error"):
            issues.append({"code": f"completeness_export_{target}", "target": target,
                           "severity": evidence["status"], "message": evidence["summary"]})
        quality.setdefault("completeness", {}).setdefault("checks", {})[f"export_{target}"] = evidence
    quality["last_export_refresh_at"] = datetime.now(timezone.utc).isoformat()
    statuses = [c.get("status") for c in quality.get("completeness", {}).get("checks", {}).values()]
    known = [s for s in statuses if s in ("ok", "info", "warn", "error")]
    quality.setdefault("completeness", {})["status"] = max(known, key=("ok", "info", "warn", "error").index) if known else "not_run"
    counts = Counter(i["severity"] for i in quality.get("issues", []))
    quality.update(errors=counts["error"], warnings=counts["warn"], issue_count=len(quality.get("issues", [])),
                   status="needs_review" if quality.get("issues") else "passed")
    path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "quality_report.html").write_text(html_report(quality), encoding="utf-8")
    report_path = out_dir / "report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["quality"] = quality
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def html_report(report: dict[str, Any]) -> str:
    """Self-contained, printable and offline; every field is visible without script execution."""
    import html
    def render(value):
        if isinstance(value, dict):
            return '<dl>' + ''.join(f'<dt>{html.escape(str(k).replace("_", " "))}</dt><dd>{render(v)}</dd>' for k, v in value.items()) + '</dl>'
        if isinstance(value, list):
            return '<ol>' + ''.join(f'<li>{render(v)}</li>' for v in value) + '</ol>' if value else '<span class="muted">None recorded</span>'
        return html.escape(str(value) if value is not None else 'Not available')
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>PDF2XML Quality Report</title>'
            '<style>body{font:14px/1.5 system-ui;max-width:1200px;margin:32px auto;padding:20px;color:#182d3c}dl{margin:8px 0}dt{font-weight:650}dd{margin:0 0 12px 18px;overflow-wrap:anywhere}li{margin:8px 0}.muted{color:#687785}h1{font-size:26px}@media print{body{margin:0}li,dd{break-inside:avoid}}</style>'
            '<h1>Conversion Quality Report</h1><p>Full conversion evidence, checks and validation results. Automated review is separate from editorial approval.</p>' + render(report) + '</html>')
