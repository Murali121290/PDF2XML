# Changelog

## dev-Sadha source refresh ? 2026-10-27

- Bring in the latest local source, including completeness checks (text layer, exports,
  images, Unicode and page ink), current job/preview APIs, publishing rules and optional
  GROBID integration. Include the matching tests, frontend handover and packaging helpers.
- Keep the existing FastAPI `/convert` interface, port 8087, direct XML/ZIP responses and
  persistent conversion worker. Keep Docling as the default with verified local models.
- Prepare the pinned models in a separate Docker stage so ordinary code updates reuse
  its cached layer. Setup downloads missing models; API conversions only read local files.
- Add a download-once regression test, restore Git/Docker ignore rules, and remove tracked
  bytecode and OS metadata. Model weights, generated output and releases stay outside Git.
- Add `docs/DEVOPS.md` with deployment instructions for dev-Sadha only. Main is unchanged.
- Preserve non-ML CLI conversions when local models are absent, and include the server
  wrapper/configuration in source archives.
- Validation: 218 regular tests, four real ML/offline API tests, and the final model/CLI
  checks pass (including two additional non-ML CLI regression cases). Ruff and Git whitespace
  checks pass. Docker is unavailable on the development machine; the container build must
  still be verified on the deployment host.


## 0.5.3 — 2026-10-03

### Added
- **Docker deployment of the conversion API** (`tools/build_docker.py` →
  `releases/pdf2xml-docker-<version>.zip`, about 960 MB). The server team builds it with
  `docker build` and no internet access. The zip holds:
  - Linux x86-64 / CPython 3.13 wheels for every package, at the tested versions, with
    PyTorch CPU;
  - the Docling models;
  - the cut application;
  - a Dockerfile, which installs offline, swaps OpenCV for its headless build and converts a
    test PDF during the build;
  - `docker-compose.yml`, a README for the server team and `API.md` for the frontend team.

  The container serves `python -m pdf2xml.api`: Docling only; JSON, canonical XML, BITS and
  JATS only; no QA reports.
- `pdf2xml serve --api-only`: only the API (convert, jobs, downloads, report, validate,
  source, docs). Viewer pages, XSLT rendering and QA images answer 404, and job results carry
  no `view` links.
- `pdf2xml serve --allowed-host <name>` (repeatable): more names for the `Host` check. With
  `*` any name is accepted, for a server behind a reverse proxy or in a container. Without
  it, a request by service name or domain was refused with `403 host`.
- `GET /api/health`: status, version and queue counts, for health checks.
- `ConversionQueue` can limit what jobs may ask for (`engines`, `targets`) and switch off the
  QA overlays and the completeness report (`qa=False`).

### Verified
- The Linux wheel set was resolved offline for Linux x86-64 / CPython 3.13 (`pip install
  --dry-run --no-index`).
- The container's app folder was run as the API on this machine, with an empty HuggingFace
  cache and the bundled models. A client sent requests the way the frontend does:
  - `Host: pdf2xml-api:8765` and a CORS origin;
  - health check, upload, polling, then the four downloads.

  A 14-page chapter converted in 26 s. All three XML files are valid, and the outputs match
  the earlier verified ones. The viewer routes answer 404, and `to=epub` is refused.
- **Not verified:** the `docker build` itself. Docker is not available on this machine, so the
  first build on the server is the first run of the Dockerfile. Its self-test step converts a
  PDF, so a problem shows during the build.

## 0.5.2 — 2026-10-03

### Added
- **Offline deploy package** (`tools/build_deploy.py` → `releases/pdf2xml-deploy-<version>-win64.zip`,
  about 800 MB). It does one thing: PDF → Docling → canonical JSON → canonical XML → BITS →
  JATS. Extract it and run `convert.bat <pdf or folder>`; nothing is installed or downloaded.
  - It holds its own Python 3.13 with the Visual C++ runtime, the 112 runtime libraries
    copied from the tested virtual environment, and the two Docling models.
  - It leaves out EPUB, DOCX, GROBID, the preview/upload server, the QA reports and the
    heuristic engine.
  - Files are `deploy/cli.py` (the cut command line), `deploy/convert.bat` and
    `deploy/README.md` (how to install and run). `THIRD-PARTY.txt` in the package lists
    every library with its licence.

### Changed
- GROBID is imported only when `--grobid` is given, so the conversion runs without its module.

### Verified
- The zip was extracted to a new folder, and its HuggingFace cache pointed at an empty folder.
  Converting a 14-page chapter there gave byte-identical canonical JSON, canonical XML, BITS
  and JATS to the full code base. The models loaded from the package, and nothing was
  downloaded.

## 0.5.1 — 2026-10-03

GROBID tested against a real server for the first time.

### Fixed
- GROBID 0.9 answers its version as JSON. The version is now read from it, so a new GROBID
  version discards the kept answers as intended.
- **Vancouver initials:** in "Smith JA." the period ends the author list; it is no longer
  tagged as part of `given-names`.
- **Access dates:** "accessed 22 January 2018" after a URL is no longer tagged as a page
  number. Volume, pages and publisher are only looked for before the URL.
- **Warnings:** each GROBID warning now says what was used instead. A failed author request
  was reported as "references were read without it".

### Added
- In `prefer` mode, the elements GROBID left out but the built-in reader found (an edition,
  an issue, an e-locator) are kept where they overlap nothing GROBID tagged.
- `tools/grobid_windows.py`: lets a GROBID source tree build and run on Windows. Only the
  references work there, because GROBID's PDF reader has no Windows build; the authors need
  Linux or Docker.

### Verified
With a real GROBID 0.9.1 (CRF models, Windows build), on scratch copies of the sample books;
every BITS and JATS file stays DTD-valid. References left as plain text:
- medical book: 133 → 26 of 505;
- policy book: 31 → 5 of 333;
- classics chapter: 6 → 0 of 13;
- nursing chapter: 3 → 0 of 42.

Placing GROBID's fields on real references found two rules, now covered by tests:
- In "Smith JA." the period ends the author list; it is not part of the initials.
- "accessed 22 January 2018" after a URL is not a page number. Volume, pages and publisher
  are only looked for before the URL.

`fallback` stays the default. `prefer` reads Harvard references with quoted titles better,
but annotated bibliographies worse.

## 0.5.0 — 2026-10-03

### Added
- **GROBID as an optional second reader** (`--grobid <url>` on `convert`, `export` and `serve`,
  or `PDF2XML_GROBID_URL`). GROBID is a local Java service (Apache-2.0).
  - **References** the built-in reader cannot read are sent to GROBID: MLA, Chicago, Vancouver
    books, organisations as authors. Its fields (authors, year, titles, journal, volume,
    issue, pages, publisher, place, editors) are placed back on the printed text, so every
    printed character stays where it was. Profile `grobid_references`: `fallback` (default),
    `prefer` (every reference) or `off`.
  - **Authors** of an article or chapter, with affiliations, e-mail addresses and ORCIDs, when
    the metadata file names no contributors. Only names printed in the first pages are used.
    Profile `grobid_header` (default on).
  - Answers are kept in `12_grobid.json`. A re-export, or a run while the server is down,
    gives the same XML. A new GROBID version discards them, and `--no-cache` asks again.
  - `report.json` counts `references_tagged_grobid`. A server that cannot be reached is a
    warning; the references are then read as before.
- Without `--grobid` the output is unchanged.

Tested only with a stand-in server that answers in GROBID's TEI format.

## 0.4.3 — 2026-10-03

### Added
- **Docling models from a local folder, for offline use.** `convert`, `inspect` and `serve`
  take `--models-dir <folder>`. The `PDF2XML_MODELS_DIR` environment variable does the same.
  Without either, the folder `docling-tools models download` fills (`~/.cache/docling/models`)
  is used when it holds both models the pipeline needs: layout and TableFormer. A folder that
  lacks a model is reported at once, before the 40-second model load.
- README, "Docling models offline": download only the two models needed
  (`docling-tools models download layout tableformer`), and run with `HF_HUB_OFFLINE=1`.

### Verified
- A fresh Docling conversion with `HF_HUB_OFFLINE=1` loaded its models from the local folder,
  without contacting the model hub.

## 0.4.2 — 2026-10-01

Root-cause fixes for book layouts the earlier rules did not cover, found on a book with one
bibliography at the back. Every fix is a general rule; there is no code for a particular book.
All books were re-converted from their PDFs to check: coverage 1.0, BITS/JATS valid, no text
missing from an export, no picture outside a figure.

### Fixed
- **Citations in books with one bibliography.** Chapters linked citations only to their own
  reference list. A chapter without one now cites the book's bibliography, and so do the
  front and back matter. Bloomsbury: 0 → 440 citation links in BITS.
- **Endnotes printed together at the back** ("Chapter 2" groups) now reach a chapter whose
  label the engine missed. A chapter's number comes from its label, or follows the previous
  chapter's.
- **Chapter labels read as paragraphs.** A label line ("Chapter 2", "PART III") right above a
  heading is the chapter's label, whatever the layout engine called it. A bare number only
  counts in a heading style.
- **Contents in a table with dot leaders in its cells** (". . . . 19") is recognised. Every
  table row is read through one line reader that strips leaders.
- **Note number raised after a title's second line** joined the first line ("LEAD TO 1
  IMPROVED"). Small raised words are now placed in the line they overlap most.
- **No picture is ever dropped.** An image no layout block holds (a cover, a plate, a logo, a
  chapter-opening photo) becomes a figure. Page backgrounds and scans under the page's text do
  not.
- **Citations** "Logan's (2009)" (possessive) and "ICTD/UNU-WIDER (2017)" (slash name) now
  link.
- **Completeness checks.** Letters read in the other case by the second reader (capitals vs.
  replacement text) are reported as information, not as lost text. A full-page image with the
  page's text on it counts as a background.

## 0.4.1 — 2026-10-01

### Fixed
These were found by the 0.4.0 completeness checks.
- **Figures lost after a reference list.** A figure, box, code block or formula between a
  reference list and the next heading was dropped from BITS and JATS. This happened to the
  three map plates in Tacitus with their captions, and to ten figures in Shimizu. Such content
  now stays in the chapter. Reference, abstract and keyword regions never drop anything they
  cannot read.
- **Chapter-opening art.** A figure printed on a chapter's first page above its heading now
  starts that chapter, instead of ending the previous one.
- **Book index read as a table** (BITS). Index pages that the layout engine read as tables
  lost 89 entries in Shimizu. They are now read column by column:
  - "69t" / "93f" page numbers are kept;
  - entries run together are split;
  - "See X (Y)" cross-references are kept;
  - lower-case lines become sub-entries;
  - letter dividers inside the text are found.

  Shimizu's index now has all 22 letter groups and 659 entries.
- **Refused uploads** (`pdf2xml serve`). A refused POST (unknown Host, foreign Origin, unknown
  route) now reads a small request body before answering. Windows otherwise reset the
  connection, and the client saw a network error instead of the 403.

## 0.4.0 — 2026-10-01

### Added
- **Completeness report** (`qa/completeness.json`): what the output may lack from the PDF.
  Every conversion runs it, unless `--no-completeness` is given. `report.json` gets the status
  and one line per check; the job API gets `result.completeness`. Each check is `ok`, `info`,
  `warn` or `error`, and none can fail a conversion. No OCR is used.
  - `text_layer`: a second reader (PDFium) finds text that pdfplumber never read.
  - `export_bits` / `export_jats`: every canonical text block is checked in the BITS and JATS
    files. Text left out on purpose is now recorded by the export (`ExportResult.dropped`):
    running heads, author queries, list markers, the JATS contents page …. So only real losses
    show as "missing".
  - `images`: PDF pictures that are in no figure, and figures an export does not write.
  - `unicode`: unmapped glyphs (`(cid:N)`), private-use characters, Tamil vowel signs stored
    in visual order, and fonts whose text cannot be trusted (Type 3 fonts, no `/ToUnicode`,
    legacy Tamil encodings).
  - `ink`: printed areas nothing captured, such as text set as outlines or an unmarked drawing.
    Rules, box frames, banners and repeated page decorations are told apart.
- The handover document describes the report (section 6.3).

### Found by the new checks (fixed in 0.4.1)
- BITS: a book index that the layout engine read as tables loses its entries (Shimizu, pp.
  340–343).
- BITS/JATS: some figures are not written: three map plates with their captions (Tacitus, pp.
  47–49) and ten figures in Shimizu.

## 0.3.2 — 2026-10-01

Fixes from a code review.

### Fixed
- **Docling cache:** the cache is now keyed by the PDF and the `--pages` selection. A partial run's
  cache was reused for the whole document, so pages were missing. A heuristic run now deletes
  the Docling stamp, so the next Docling run no longer reads heuristic output as Docling output.
- **Lost words:** a table or figure marked as a header/footer/slug artifact kept no text, which
  broke text coverage. Its words now go into the artifact.
- **Lists:** a footnote or reference between two list items (a list running over a page break)
  no longer splits the list in two.
- **Upload API:**
  - A job no longer lists outputs left over from an earlier conversion with the same name.
  - An error while finishing a job no longer stops the queue: it is logged and the next job
    runs.
  - A job whose conversion process cannot start fails at once instead of staying queued.
- **Security:** every request must name the server in its `Host` header (an IP address,
  `localhost`, or the `--host` name). Other names get `403 host`, which blocks DNS rebinding.
- **Preview:** a stylesheet that runs past the timeout is stopped and the XSLT process
  restarted; later previews no longer wait behind it.
- **Uploads:** multipart forms are split in place instead of going through the `email` parser,
  which copied the PDF several times. Malformed forms get `400 bad_form`.
- `pdf2xml serve` without `--engine` uses Docling when it is installed and `auto` (heuristic)
  otherwise. `--engine docling` without Docling is refused at start-up.

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

## Server compatibility history


## API: return all XML outputs at once

- Add `return_xml=all`, which returns one ZIP with `30_canonical.xml`, `40_bits.xml` and
  `41_jats.xml` from a single conversion. It requires `xml`, `bits` and `jats` in `targets`.
- Existing `jats`, `bits` and `rawxml` responses are unchanged.

## Offline Docling default — 2026-10-22

- Make Docling the API, pipeline and CLI default while preserving the existing HTTP contract.
- Resolve only complete local models; disable runtime downloads, telemetry, remote services
  and external plugins. Missing weights fail with a model-preparation instruction.
- Pin model revisions/checksums and provide `tools/prepare_models.py` for one-time downloads,
  copying existing weights, or offline verification. Keep large weights outside Git.
- Bake verified models into the Docker image, enforce offline settings, and run a build
  self-test with networking disabled. Reuse one conversion worker after the first model load.
- Test default Docling requests with an empty model-hub cache and blocked outbound sockets.

## 0.5.3 compatibility update — 2026-10-22

- Port the 0.5.3 Docling model configuration, page-aware layout cache, text merge,
  image recovery, and BITS/JATS publishing improvements into the existing `src/` package.
- Preserve `/convert`, `file`, `engine`, `targets`, `return_xml`, direct XML responses,
  JSON response fields, the heuristic default engine, and port 8087.
- Preserve EPUB/DOCX exports, heuristic extraction, overlays, and the proof viewer.
- Use one persistent conversion process so ML and XSLT work does not block HTTP handling.
- Add optional page selection, upload limits, output-root settings, and CORS configuration.
- Clean up partially written uploads and failed conversions; reject invalid options before
  starting the conversion.
- Keep reference parsing local; remove external reference-service hooks and settings.
- Replace the machine-specific requirements file with a portable editable project install
  and pin the Docling extra to the release's version.
- Add legacy API and conversion regression tests.
