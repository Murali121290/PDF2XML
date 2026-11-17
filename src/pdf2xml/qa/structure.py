"""Publication quality checks, distinct from character coverage and schema validity.

A passed result means these checks found no issues; it is not a guarantee that an arbitrary
PDF has been editorially proofread. Findings retain page/block evidence for human review.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from lxml import etree

from pdf2xml.model import Box, Caption, Document, Figure, Formula, Heading, Table, iter_blocks

_EDITORIAL = re.compile(
    r"^(?:CHAPTER\b|\d+\s+CHAPTER\b|BOX\s+\d|STOP AND CONSIDER\b|"
    r"LEARNING (?:OUTCOMES|OBJECTIVES)\b|NOTABLE RESEARCH\b)", re.I)


def check(doc: Document, exports: dict[str, Any], out_dir: Path, *, coverage_ok: bool,
          xml_errors: list[str], completeness: dict[str, Any] | None = None,
          content_errors: list[str] | None = None) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    def add(code: str, message: str, page: int | None = None, *, severity: str = "error",
            block: str | None = None, target: str | None = None) -> None:
        issue: dict[str, Any] = {"code": code, "severity": severity, "message": message}
        if page is not None:
            issue["page"] = page
        if block:
            issue["block"] = block
        if target:
            issue["target"] = target
        issues.append(issue)

    if not coverage_ok:
        add("text_coverage", "Extracted text and canonical text do not match.")
    if xml_errors:
        add("canonical_schema", "Canonical XML failed schema validation.")
    if content_errors:
        add("content_schema", "Content XML failed schema validation.", target="content")
    confidence_path = out_dir / "11_structure_confidence.json"
    confidence = None
    if doc.meta.engine == "docling" and confidence_path.exists():
        confidence = json.loads(confidence_path.read_text(encoding="utf-8"))
        for number, page in confidence.get("pages", {}).items():
            if page.get("low_grade") in ("poor", "fair"):
                add("layout_confidence", "Docling flagged uncertain extraction on this page.",
                    int(number), severity="warn")
    for name, finding in (completeness or {}).get("checks", {}).items():
        if finding.get("status") in ("warn", "error"):
            add("completeness_" + name, finding["summary"], severity=finding["status"])
            issues[-1]["details_file"] = "qa/completeness.json"
    heads = [b for b in iter_blocks(doc.body) if isinstance(b, Heading)]
    recovered_index = {b.page for b in iter_blocks(doc.body)
                       if (b.source_ref or "").startswith("index-native:")}
    for page in sorted(recovered_index):
        add("index_line_recovery", "Index text was recovered in native column/line order after "
            "an empty table prediction; index-entry semantics and links require review.",
            page, severity="warn")
    for head in heads:
        text = "".join(r.text for r in head.runs)
        if re.search(r"(?:([A-Z])\1){4,}", text):
            add("overprinted_heading", "Heading contains repeated overprinted characters.",
                head.page, block=head.id)
        native_label = (head.source_ref or "").startswith("native-chapter-label:") and any(
            h.role == "chapter" and h.page == head.page and h.bbox[1] >= head.bbox[3] for h in heads)
        if text.strip().isdigit() and not head.role and not native_label:
            anchored = bool(head.source_ref and any(e.page == head.page for e in doc.meta.outline or []))
            add("numeral_heading", "A numeral-only heading needs its relationship to the "
                "following title reviewed." if anchored else
                "A numeral-only heading has no structural source role.",
                head.page, block=head.id, severity="warn" if anchored else "error")
        if re.match(r"^\s*[●•▪◦]\s", text):
            add("bullet_heading", "A printed bullet became a section heading.",
                head.page, block=head.id)
    caption_pattern = re.compile(r"^\s*[▲△▶►]?\s*(?:figure|fig\.?|table)\s+\d+\.\d+[.:]", re.I)
    for block in iter_blocks(doc.body):
        if isinstance(block, Table):
            cells = [cell for row in block.rows for cell in row]
            if len(cells) == 1 and len(" ".join(r.text for r in cells[0].runs).split()) > 80:
                add("table_structure_collapse", "A long table region has only one cell; verify "
                    "its rows, columns and reading order against the source.", block.page,
                    block=block.id, severity="warn")
        text = "".join(r.text for r in getattr(block, "runs", []))
        if text and caption_pattern.match(text) and not isinstance(block, Caption):
            add("detached_caption", "Numbered caption has no canonical caption relationship.",
                block.page, block=block.id)
        if (block.type == "paragraph" and not block.source_ref and 0 < len(text.split()) <= 3
                and len(text) < 16):
            add("orphan_fragment", "Loose short text needs its source sentence checked.",
                block.page, block=block.id)
        if isinstance(block, Formula) and not (block.latex or block.mathml):
            add("formula_transcription", "Equation preserved as source image; structured math "
                "transcription remains unverified." if block.image else
                "Equation has no structured math or source image fallback.",
                block.page, block=block.id, severity="warn" if block.image else "error")
        if isinstance(block, Figure) and re.search(r"\(cid:\d+\)", block.alt or ""):
            add("figure_glyph_mapping", "Figure text contains unresolved source-font glyphs; "
                "inspect the preserved source image before transcription.",
                block.page, block=block.id, severity="warn")
    for box in (b for b in iter_blocks(doc.body) if isinstance(b, Box)):
        for child in box.children:
            if isinstance(child, Figure) and child.decorative:
                continue  # A callout icon may intentionally straddle its panel border.
            if child.page == box.page and (
                    child.bbox[0] < box.bbox[0] - 4 or child.bbox[1] < box.bbox[1] - 4
                    or child.bbox[2] > box.bbox[2] + 4 or child.bbox[3] > box.bbox[3] + 4):
                add("box_containment", "Box contains a block outside its physical boundary.",
                    child.page, block=child.id)
    if len(heads) >= 10 and sum(h.level == 6 for h in heads) / len(heads) > 0.8:
        add("heading_hierarchy", "Most headings collapsed to level 6; hierarchy needs review.")
    selected = {p.n for p in doc.pages}
    index_starts = [e.page for e in doc.meta.outline or [] if e.kind == 'index']
    if index_starts:
        image_index_pages = {b.page for b in iter_blocks(doc.body)
                             if isinstance(b, Figure) and b.page >= min(index_starts)
                             and len((b.alt or '').split()) > 100}
        for number in sorted(image_index_pages):
            add('image_based_index', 'Index content is preserved as an image; structured '
                'index-entry transcription remains unverified.', number, severity='warn')
    chapters = [e for e in doc.meta.outline or [] if e.kind == "chapter" and e.page in selected]
    recovered = {h.page for h in heads if h.role == "chapter"}
    for entry in chapters:
        if not any(entry.page <= page <= entry.page + 3 for page in recovered):
            add("chapter_anchor", "A bookmarked chapter has no recovered chapter heading.",
                entry.page)
    for artifact in doc.artifacts:
        if artifact.kind == "figure_text" and _EDITORIAL.match(artifact.text.strip()):
            add("editorial_text_as_figure", "Editorial text remains inside a figure artifact.",
                artifact.page, severity="warn" if chapters and artifact.page < min(
                    e.page for e in chapters) else "error")
    parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False)
    for target, result in sorted(exports.items()):
        if not result.valid:
            add("export_schema", "Export failed its publishing schema.", target=target)
        for warning in result.warnings:
            if warning.startswith("TOC:") or "chapter(s) are not listed" in warning:
                add("contents_mapping", warning, severity="warn", target=target)
        tree = etree.parse(str(result.path), parser)
        root = tree.getroot()
        if target == "bits" and root.tag == "book" and len(chapters) >= 2:
            count = len(root.xpath('.//book-part[@book-part-type="chapter"]'))
            if count != len(chapters):
                message = f"Expected {len(chapters)} bookmarked chapters; found {count}."
                add("chapter_count", message,
                    target=target)
        ids = set(root.xpath("//@id"))
        for ref in root.xpath('.//xref[@rid]'):
            if any(rid not in ids for rid in ref.get("rid", "").split()):
                add("broken_reference", "An XML cross-reference has no target.", target=target)
        # A legal DTD does not require plain-text figure callouts to be linked.
        # Check them explicitly, excluding labels/captions/accessibility text.
        for text in root.xpath('.//text()[not(ancestor::xref) and not(ancestor::caption) '
                               'and not(ancestor::alt-text) and not(ancestor::label)]'):
            match = re.search(r"\b(?:Fig\.|Figure)\s+\d+\.\d+[A-Za-z]?", str(text))
            if match:
                element = text.getparent()
                anchor = next((e.get('id') for e in [element, *element.iterancestors()]
                               if e.get('id')), None)
                add("unresolved_figure_callout", f"{match[0]} is plain text without a resolved "
                    "target.", target=target, severity="warn", block=anchor)
                issues[-1]['xml_line'] = element.sourceline
        for graphic in root.iter("graphic"):
            href = graphic.get("{http://www.w3.org/1999/xlink}href", "")
            if href and "://" not in href and not (out_dir / href).is_file():
                add("missing_asset", "A referenced graphic file is missing.", target=target)
        for section in root.xpath('.//sec[@sec-type="practice-questions"]'):
            # Inspect direct question-section content, not lists in a nested
            # discussion/short-answer section. Partial success must not hide failures.
            if section.xpath('./list/list-item'):
                add("unstructured_questions", "Ambiguous questions/options; kept as lists.",
                    severity="error" if section.xpath('./question-wrap | ./question-wrap-group')
                    else "warn", target=target, block=section.get("id"))
            numbers = []
            for question in section.xpath('./question-wrap/question | '
                                          './question-wrap-group/question-wrap/question'):
                label = question.findtext("label", "").strip().rstrip(".)")
                if label.isdigit():
                    numbers.append(int(label))
                options = question.findall("option")
                labels = [o.findtext("label", "").strip().rstrip(".)").lower()
                          for o in options]
                if question.get('question-response-type') == 'multiple-choice' and (
                        len(labels) < 2 or labels != [chr(ord('a') + i)
                                                     for i in range(len(labels))]):
                    add("question_options", "Question options are incomplete or out of order.",
                        target=target, block=question.getparent().get("id"))
            if numbers and (len(numbers) != len(set(numbers)) or any(
                    b != a + 1 for a, b in zip(numbers, numbers[1:], strict=False))):
                add("question_sequence", "Question numbers have gaps, duplicates or wrong order.",
                    target=target, block=section.get("id"))
    counts = Counter(i["severity"] for i in issues)
    return {"status": "needs_review" if issues else "passed",
            "editorially_verified": False,
            "errors": counts["error"], "warnings": counts["warn"],
            "issue_count": len(issues), "issues": issues,
            **({"model_confidence": confidence} if confidence is not None else {})}
