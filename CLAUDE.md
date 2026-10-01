# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`pdf2xml` converts **born-digital** PDFs (no OCR, ever) into a canonical JSON/XML document model, then exports EPUB 3, DOCX, BITS 2.2 and JATS 1.4, keeping structure *and* styles. Python 3.11–3.13, `src/` layout, managed with `uv` (dev tools are in the PEP 735 `dev` dependency group).

## Commands

```powershell
uv sync                       # install + dev group (pytest, ruff, mypy, reportlab, epubcheck)
uv sync --extra ml            # add Docling (pulls torch); without it engine=auto falls back to heuristic

uv run pdf2xml convert file.pdf -o out --engine heuristic --to json,xml,epub,docx --pages "1-3,7"
uv run pdf2xml export out/<name> --to bits,jats --profile examples/profiles/bits-chapter.yaml --meta examples/metadata/chapter.yaml
uv run pdf2xml validate out/<name>/40_bits.xml   # offline DTD check (schema detected from root + DOCTYPE)
uv run pdf2xml inspect file.pdf        # triage + QA overlays only
uv run pdf2xml serve out --xslt my-house.xsl   # live XSLT preview + checks at http://127.0.0.1:8765
uv run pdf2xml serve out --cors-origin http://localhost:5173   # + upload/convert API; page /upload
uv run pdf2xml schema                  # regenerate resources/schemas/canonical.schema.json from the Pydantic model

uv run pytest -m "not slow"            # fast suite (heuristic engine only)
uv run pytest -m slow                  # Docling end-to-end (loads ML models, ~40 s startup)
uv run pytest tests/unit/test_merger.py::test_name
uv run pytest tests/integration/test_pipeline.py --update-goldens   # rewrite tests/fixtures/expected/*.heuristic.json

uv run ruff check src tests
uv run mypy src                        # merge/styles/xml/model packages are mypy --strict

uv run python tests/fixtures/make_fixtures.py   # regenerate fixture PDFs (reportlab)
```

`test_outputs_are_valid` runs W3C EPUBCheck, which needs Java on PATH.

## Pipeline architecture

`pipeline.convert()` runs the stages and writes every intermediate to `out/<pdf-stem>/` with a numbered prefix, so each stage can be inspected in isolation:

| File | Stage | Code |
|---|---|---|
| `10_style_layer.json` | **Style layer**: every word with exact font, size, colour, bold/italic, bbox | `extract/style_pdfplumber.py` |
| `00_triage.json` | Encrypted? tagged? has a text layer? doc-type guess. Rejects scanned PDFs | `triage.py` |
| `11_structure_raw.json` | **Structure layer**: blocks (heading, list_item, table…) with bbox + reading order | `extract/structure_docling.py` or `structure_heuristic.py` |
| `20_canonical.json` | Merge → artifacts → style catalogue → `Document` | `merge/`, `styles/`, `model/` |
| `30_canonical.xml` | `Document` → XML, validated against RelaxNG | `xml/serialize.py` |
| `40_bits.xml`, `41_jats.xml` | `Document` → semantic enrichment → BITS/JATS, validated against bundled NLM DTDs | `publishing/` |
| `50_book.epub` | XML → XHTML via XSLT 3.0 (Saxon-HE) + generated CSS | `export/epub.py`, `resources/xslt/` |
| `51_document.docx` | `Document` → python-docx; catalogue styles become named Word styles | `export/docx.py` |
| `assets/`, `qa/`, `report.json` | Figure crops (pypdfium2), bbox overlay PNGs, coverage/validity/timings summary | `export/assets.py`, `qa/` |

Key design rules:

- **Two independent layers, merged by geometry.** The style layer knows *how text looks*; the structure engine knows *what blocks are*. `merge/merger.py` assigns each word to the block covering the largest share of its area (≥ `assign_threshold`, default 0.5). Words outside every block attach to a nearby text block or become new paragraphs inserted into reading order — **no word is ever dropped**. Engine block text (`RawBlock.text`) is fallback only; output text always comes from style-layer words.
- **Adapter boundary.** No third-party object (Docling, pdfplumber, pikepdf) may leave `extract/`. Adapters convert to the dataclasses in `extract/types.py` (`Word`, `StyleLayer`, `RawBlock`, `StructureLayer`) with top-left-origin PDF points. Backends/engines are chosen via factories in `extract/base.py` (`StyleBackend` / `StructureEngine` protocols).
- **Canonical model is the single source of truth** (`model/document.py`, Pydantic, `extra="forbid"`, `Block` is a discriminated union on `type`). Bboxes rounded to 2 dp, colours lowercase `#rrggbb`. Changing it means updating, together: the model, `resources/schemas/canonical.rng`, `xml/serialize.py`, `resources/xslt/canonical-to-xhtml.xsl`, `canonical.schema.json` (`pdf2xml schema`), and the golden fixtures.
- **Style catalogue + diff runs** (`styles/catalogue.py`). Runs are grouped into signatures (font, size, bold, italic, colour). The dominant body signature becomes `body`; heading tiers by (size, bold) become `h1`…`h6`; role styles `caption`, `code`, `footnote`…; the rest `s1`, `s2`…. Each block references a style key and its `Run`s store **only properties that differ** from that style. Short large-type paragraphs are promoted to headings here.
- **Text-coverage invariant** (`qa/coverage.py`): the multiset of non-whitespace characters in the output (body, footnotes, references, artifacts, markers, table cells) must equal the style layer's, allowing only removed end-of-line hyphens. Failures become warnings in `report.json` and fail the integration tests.
- **Determinism.** Same input → byte-identical JSON/XML (tested). Sort tie-breaks explicitly; EPUB zips use fixed timestamps and honour `SOURCE_DATE_EPOCH`.
- **Printer's PDFs and page origins.** Docling and pdfium measure a page from its CropBox; the style layer (pdfplumber) from the MediaBox. `structure_docling._crop_origins` shifts every Docling box (and table cell) by the CropBox's top-left corner when Docling's page size is the CropBox; `export.assets.page_origin` does the same for figure crops and QA overlays. Without it every block sits ~30 pt off and words fall into neighbouring blocks (tests in `tests/unit/test_page_boxes.py`).
- **List markers come from the text.** `build._split_marker` only uses a marker printed at the start of the item; Docling's own numbering is never written (it is not on the page and broke the coverage invariant). The item's `ordered` flag keeps the list type. Loose words outside the trim box (slug lines) are no orphan warning.
- **Docling caching.** Docling output is cached in `11_structure_raw.json`, keyed by the PDF's SHA-256 in `11_structure_raw.sha256`; `--no-cache` forces a re-run. `from_docling_document` is a pure mapping so it can be tested from saved JSON. The Docling converter is an `lru_cache` singleton (model load is slow), as is the Saxon processor (saxonche allows only one).
- The heuristic engine has **no table detection** (cells become paragraphs); it is for tests and machines without Docling.
- **Page geometry recovers structure the engines miss** (tests in `tests/unit/test_structure.py`):
  - **Printer's PDFs.** `PageLayer.trim` is the PDF TrimBox and `PageLayer.frame` is the trim box or the whole page. `merge/artifacts.py` marks blocks mostly outside the frame as `other` artifacts: slug lines, crop marks and registration marks. The header and footer margins are measured inside the frame.
  - **Nested lists** (`merge/lists.py`). A flat list is nested by marker kind (`1.`, `a)`, `•`, `–`…) and indent. An ordered marker that follows the previous one stays at that level. A jump of more than 100 pt is the next column, not an indent.
  - **Boxes** (`merge/boxes.py`). Filled or stroked rectangles and closed curves become frames: nested rectangles merge, and so do overlapping ones of one colour. A frame enclosing ≥ 2 blocks, including running text, wraps them in a canonical `Box`. Its role comes from the title (tip, example, warning…). The trim frame, table borders and heading-only banners are not boxes. A box at the foot of a page continues into a same-colour, untitled frame at the head of the next page.

### BITS / JATS export (`publishing/`)

- **Pipeline:** `semantic.build()` turns the flat canonical `Document` into a `SemanticDoc`, and `writer.py` serialises it. The semantic doc holds title, byline, abstract and keywords, the nested `Section` tree, `FloatObj` (table/figure + label + notes), `BoxNode`, `Epigraph`, `Query`, references and footnotes; for a whole book also `front` / `units` (chapters, or parts holding chapters) / `back` as `Matter` and `Unit` objects. `inline.py` resolves run formatting against the catalogue style and detects links on the joined block text.
- **Rules:**
  - `build()` must never mutate the canonical Document. Canonical blocks are copied with `model_copy`, and IDs for canonical objects go in `SemanticDoc.node_ids`. This is tested.
  - Semantic IDs come from the profile's `ids` patterns.
- **Enrichment heuristics:** each is generic (no per-publisher code) and has a unit test in `tests/unit/test_publishing.py`.
  - Heading depth = dense rank of (catalogue level, larger type size first, ALL-CAPS first), or the profile's `style-map`. The catalogue gives many sizes one level (`h6`, `h6-2` …), so size must separate them.
  - A reference list ends at the next heading ranked as high, and also at a lower heading or a short year-less line (a section title the engine did not mark) when what follows does not look like a reference (`_reference_like`: a year and a citation-like start).
  - A canonical `Box` becomes a `BoxNode` (`boxed-text/@content-type` = its role). Its first heading is the caption title ("Box 3.1 Tip" → `label` + `title`), and later headings open `sec`s inside the box, so box headings never become document sections.
  - A heading labelled "Box N" also opens a `BoxNode`. It holds same-or-lower headings until a higher heading or the next box.
  - Running heads are headings in the top or bottom sixth of the page that match a header artifact *and* carry a digit or ornament; the real title matches too but has neither, and a contents entry ("PART I … 1") matches mid-page. Running heads extracted as text (a short block in the top or bottom tenth of the page) are dropped when their words repeat there on 3 pages, or on 2 when they also match a heading or header artifact.
  - Queries are recognised by `queries.pattern`, by small-type styles used mostly for queries, by small runs inside a paragraph, and by a query column inside a table.
  - Paragraph joining needs no end punctuation on the first part and a leading lower-case letter on the second. Across a page or column break, a capital or digit also continues the sentence when the first part is at least 60 characters. Joining looks past floats, queries, epigraphs, boxes and other-style paragraphs, and it rejoins a word hyphenated at the break.
  - Lists join the same way. An open last item takes a lower-case paragraph that follows it, and a list continues into the next list after a break when the marker kind matches (ordered markers must follow on).
  - Page markers: `_folios` reads printed page numbers from page-number artifacts and from the number at either end of a running head or footer. The most common printed-minus-PDF offset wins; roman front-matter numbers (i–xii) get their own offset for the pages before the arabic ones start. `_page_marks` records where each page starts: the start of a node, or an offset inside a joined paragraph (`breaks`). The writer emits `<target target-type="page">` or `<?page-break N?>` there (profile `page_markers`). Markers for nodes without text wait in `pending` for the next inline text, but never go inside `disp-formula`. Without `fpage` in the metadata, the folios supply `fpage`/`lpage`.
  - References (`citations.py`): author–year (APA/Harvard/Chicago) and Vancouver references are tagged granularly inside `mixed-citation`. `string-name` is used, not `name`, because `name` is element-only and would reject the printed punctuation. Every printed character stays in place. Italic runs mark the source or book title. Unparsed references stay plain and are counted in a warning.
  - Author–year citations: one `xref` per author–year pair, including further years ("Smith, 2020a, b, 2021") and multi-word organisations in narrative form. Keys also cover names extracted without spaces and acronyms. Citations with no matching reference are listed in the export warnings.
  - Only headings next to extracted front matter are ever dropped; other empty headings keep their text.
  - A heading that is only a label ("S E C T I O N 1", "CHAPTER FOURTEEN", a bare "4") with a title heading right after it on the same page becomes that section's `label`.
- **Books, contents, front and back matter** (`semantic.py` stages `_toc` → `_book`/`_chapter` → `_link_toc`; pure readers in `matter.py`, no XML there):
  - `bits.root: auto` (default) writes `<book>` when the PDF prints a contents page with ≥ 3 page-numbered entries, or has a copyright page with an ISBN in its first 15 pages; otherwise `book-part-wrapper`. `build(book=None|True|False)` mirrors this; JATS is always one article.
  - Printed contents (`_toc`, `matter.parse_toc`): a "Contents"-type heading in the first quarter of the book, then its page and each next page where ≥ 20 % of lines end in a page number. Entries come from table rows, dot leaders, or page numbers/labels set in their own column (paired by line position). Authors under an entry become its contributors, wrapped titles are rejoined, "Part N" lines without a page number hold the entries after them. Page-number lines near the Contents heading are never paragraph-joined. The engine may read the heading after the entries of its page, run several lines into one block (split at kept line breaks, e.g. a block read as code, and, in a block taller than one line, after each page number followed by the next entry) and read a block out of place (a one-column contents page is re-read top to bottom). Levels: dotted labels ("2.1"), else the typical indent of the label kind ("PART #", "#", "Section #"; right-aligned numbers make "10" start left of "7"), else the entry's own indent; facing pages are aligned on their word labels; an unlabelled line split out of a block sits below the labelled line before it. "Section" is not a division.
  - Chapters start at the headings the top-level entries name (printed page ±1 and a close title, including a label heading above and up to three title lines); without a usable contents page, at the highest-ranked headings after the last contents/copyright page. Everything before the first chapter is front matter; from the first back-matter heading on (appendix, glossary, index, notes, references, bio …) it is back matter; unclassified parts there become `book-part`s in `book-back`.
  - Front matter: titled parts by `matter.kind_of` (preface, foreword, dedication, acknowledgments, contributors → `bio`, abbreviations → `glossary`); untitled pages are classed cover / half-title / title-page / copyright-page / dedication / epigraph / series-page / other (`front-matter-part/@book-part-type`). The title page (largest type) gives book title, subtitle and "Edited by …" people; the copyright page gives ISBNs with their format (`PB:`, `(epdf)` …), publisher and place (also from the LoC CIP "Description:" line), year, copyright and edition. The metadata file overrides all of it.
  - Back matter of a chapter or article (`_regions`, `BACK_KINDS | ANY_KINDS`) moves to its `back`: appendices (`app-group/app`, `book-app` in `book-back`), glossary (`def-list` from two-column tables, "Term: definition" or bold-lead paragraphs), endnotes (`fn-group`; notes run together are split where the numbering jumps), `bio`, `ack`, further reading (`ref-list/@content-type="further-reading"`), index (BITS `index` with `nav-pointer`s to page targets, "see / see also", indented sub-entries; JATS `sec sec-type="index"`). Index and glossary lines are never paragraph-joined.
  - Each chapter keeps its own reference list, page footnotes and endnotes; `LinkContext.scope()` switches citation and note targets per chapter. Superscript note numbers (`³`, `³ ⁴`, `²⁻⁴`) link to their note (`xref ref-type="fn"`).
  - Contents entries get a `nav-pointer` holding the printed page number and pointing at their chapter/section, or at the page target when no heading matches. Warnings list entries matching nothing and chapters missing from the contents. JATS drops the contents with a warning. `_drop_dangling` removes any link whose target was not written.
  - The page-marker walk (`_page_marks`) follows exactly the order the writer emits front matter, chapters and back matter in; change both together.
- **Schemas:**
  - Body markup is shared: BITS reuses the JATS body model; only the wrappers (`book-part-wrapper`, `book`, `article`) and the metadata differ.
  - DTD content models are strict. For example, `sec`/`body` allow blocks *then* `sec*` and never blocks after a sub-section, and `list-item` needs `p`. Print a model with lxml (`etree.DTD(...).iterelements()`, format `e.content`) before adding elements.
  - Output is validated from the serialised bytes, so error line numbers point into the file. `_missing_metadata` adds plain-language reasons before the DTD errors.
  - Indentation only touches element-only content: the DTD's `mixed` elements are never re-indented, because whitespace inside `p` would change the text.
- **Bundled schemas:** `resources/schemas/nlm/` holds the unmodified official DTD packages (BITS 2.2, JATS 1.4 Archiving/Publishing/Authoring, MathML 3; public domain), loaded offline by `publishing/schemas.py`. JATS defaults to Archiving because Publishing needs journal-id + ISSN and Authoring needs contributors + abstract.
- **Profiles and metadata:** `publishing/profile.py`, examples in `examples/profiles/` and `examples/metadata/`; all example files are loaded by a test.

### Preview server (`preview.py`, `pdf2xml serve`)

- A stdlib `ThreadingHTTPServer`, bound to localhost, over an output root. The UI is a single page with no build step: `resources/preview/index.html`.
- It renders any XML in a conversion folder through a stylesheet. The choices are the built-in proof sheet (`resources/xslt/jats-preview.xsl` + `resources/preview/preview.css`), the EPUB sheet for `30_canonical.xml`, or the user's own sheets (`--xslt`, or files in `<root>/_xslt/`).
- **Saxon runs in one child process** (`_XsltWorker`). saxonche is native and crashes when it is called from a thread other than the one that created it.
- Live reload: the UI polls `/api/stamp`, which combines the mtimes of the XML, the sheet, and the `.xsl`/`.css` files beside it.
- The proof sheet gets `pv:line` attributes (`urn:pdf2xml:preview`) added in memory. It turns them into `data-line`, so a click in the preview jumps to the source line. Custom sheets get the XML unchanged.
- The DOCTYPE is dropped before the transform so Saxon never fetches the DTD.
- Routes:
  - `/view/<sheet>/<doc>/<file.xml>` renders the file. Relative links resolve under the same path: the sheet's folder first, then the conversion folder (`assets/`).
  - `/files/<doc>/…` serves the QA page images.
  - `/api/*` returns listings, the source, validation, `report.json`, and the folio → PDF page map.
- Every path is resolved and checked to stay inside the root (`tests/unit/test_preview.py`).
- **Conversions over HTTP** (`jobs.py`, `tests/unit/test_jobs.py`): `POST /api/convert` (raw PDF body or multipart `file`) → `ConversionQueue` → one spawned child process runs `pipeline.convert` job by job, so Docling's models stay loaded and Saxon stays on one thread. `pipeline.convert(progress=…)` reports each stage. `GET /api/jobs[/<id>]` gives state, stage and output links; `DELETE` cancels (a running job by killing the worker). Uploads go to `<root>/_uploads/<id>/`, results to `<root>/<safe name>/`. Jobs are in memory only. Conversion routes answer JSON errors `{error, message}`; the viewing routes keep plain text. Writes from a foreign `Origin` are refused unless allowed by `--cors-origin`. `/upload` is the reference page. Handover for the frontend team: `docs/HANDOVER-FRONTEND.md`.
- The proof sheet shows elements it has no rule for with a red outline (`pv-unknown`). When the export starts writing a new element, add a rule for it.

## Testing notes

- Integration tests convert every PDF under `tests/fixtures/pdfs/` once per session (`heuristic_results` fixture) and check invariants (coverage, schema validity, unique ids, bboxes in page), EPUBCheck, and exact equality with `tests/fixtures/expected/<name>.heuristic.json`. After an intentional behaviour change, run with `--update-goldens` and review the golden diff.
- `tests/conftest.py` has `make_word` / `line_of` helpers for building synthetic `Word`s in unit tests; `tests/unit/test_publishing.py` has `doc()`/`h()`/`p()` helpers for synthetic Documents.
- `pdf2xml export out/<name>` re-runs BITS/JATS from `20_canonical.json`, the fast loop for working on `publishing/` against a real document.
- `out/` and `samples/` are git-ignored (generated output and private input PDFs).

## Scope and licensing constraints

- OCR is out of scope; Docling runs with `do_ocr=False`.
- Dependencies are permissively licensed (licence noted beside each in `pyproject.toml`); the package is proprietary. The PyMuPDF style backend is stubbed because PyMuPDF is AGPL — don't add it without a licence decision.
- Planned but not built (code emits warnings/fallbacks): tagged-PDF structure-tree path and real Word list numbering (Phase 2), formula enrichment/OMML (Phase 3), fixed-layout EPUB (Phase 5, currently falls back to reflow).
- Source comments cite `CLAUDE.md §1/§2.1/§7/§8/§11` from an earlier design document. Those sections map to: scope/born-digital (§1), merge rule (§2.1), canonical model (§7), style catalogue (§8), licensing (§11) — all summarised above.
