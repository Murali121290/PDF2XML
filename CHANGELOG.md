# Changelog

## 0.3.1 — 2026-09-29

### Changed
- The canonical XML is shown to users as **S4C XML** (the product name): tab label on `/upload`
  and the handover document. File names and API keys are unchanged (`30_canonical.xml`,
  `canonical_xml`).

## 0.3.0 — 2026-09-29

### Added
- **Upload and convert over HTTP** (`pdf2xml serve`): `POST /api/convert` takes a PDF (raw body
  or multipart form) and options (engine, outputs, pages), and returns a job. `GET /api/jobs/<id>`
  shows the state (queued / running / done / failed / cancelled), the current pipeline stage
  and, when done, links to view and download the canonical, BITS and JATS XML, EPUB, DOCX and
  the report. `GET /api/jobs` lists the jobs; `DELETE /api/jobs/<id>` cancels one.
  Conversions run one at a time in a background process that keeps Docling's models loaded.
- `/upload`: a reference page for the flow (drop a PDF → Docling → XML previews and
  downloads).
- `pdf2xml serve` options: `--convert/--no-convert`, `--engine` (default `docling`),
  `--max-upload-mb`, `--cors-origin` (CORS for a frontend dev server).
- `pipeline.convert(progress=…)` reports each stage as it starts.
- JSON error bodies `{error, message}` on the new routes; writes from other web sites are
  refused.
- The handover document describes the new routes (section 5.2).

## 0.2.1 — 2026-09-29

### Added
- `docs/HANDOVER-FRONTEND.md`: handover to the frontend team (running the backend, output
  folder contract, HTTP API reference with real responses, data formats, integration notes,
  open points to agree). Linked from the README; included in release zips.

No code changes.

## 0.2.0 — 2026-09-29

### Added
- **Whole books in BITS** (`bits.root: auto`, the new default): a PDF with a printed contents
  page, or a copyright page with an ISBN, becomes `<book>` with `front-matter`, `book-body`
  (chapters, or parts holding chapters) and `book-back`. Chapter PDFs stay `book-part-wrapper`.
- **Printed table of contents** → BITS `toc` / `toc-entry` with `nav-pointer`s to the chapter or
  section (or its page). Reads dot leaders, tables, page numbers in their own column, lines the
  layout engine ran together, "Part / chapter / Section" levels.
- **Front matter**: cover, half-title, title page, copyright page, dedication, epigraph, series
  page, preface, foreword, acknowledgments, contributors. `book-meta` is filled from the title
  and copyright pages (title, subtitle, editors, ISBNs with format, publisher and place, year,
  copyright, edition); a metadata file overrides it.
- **Back matter**: appendices, glossaries (`def-list`), endnotes (`fn-group`, linked from
  superscript numbers), back-of-book index (`index`, see / see also, sub-entries, page links),
  contributor notes (`bio`), acknowledgments, further reading. Each chapter keeps its own
  reference list and notes.
- Roman front-matter page numbers (i, ii … xii); page targets such as `page_vii`.
- QA warnings: contents entries that match no heading, chapters missing from the contents.
- Preview proof sheet rules for all new elements.
- `examples/profiles/bits-book.yaml`; profile options `matter`, `ids.chapter`,
  `ids.book_part`, `ids.matter`.

### Fixed
- **Printer's PDFs (CropBox inside a larger MediaBox)**: Docling boxes, figure crops and QA
  overlays were offset by the CropBox corner (~30 pt), so words fell into neighbouring blocks
  (14 % "orphan" words, merged contents lines).
- Docling's own list numbering was written as list markers (text not on the page; coverage
  check failed).
- A small "REFERENCES" heading swallowed the following sections into the reference list;
  heading rank now uses type size within a catalogue level.
- Running heads: mid-page contents entries ("PART I … 1") are no longer dropped; running heads
  extracted as text in the page margin are dropped.
- Slug-line words outside the trim box no longer raise "words outside all detected blocks".

## 0.1.0

- First version: canonical JSON/XML, EPUB 3, DOCX, BITS 2.2 (one chapter), JATS 1.4.
