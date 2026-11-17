# PDF2XML Application

Clean runtime snapshot: application 0.5.6 with the latest local Content XML and Layout QA
updates; Docling pinned to 2.150.0. Original workspace files were copied, never moved.

## Contents

- `src/pdf2xml/`: runtime code, frontend, publishing DTDs, XML schemas and stylesheets.
- `models/`: verified local Heron and TableFormer (fast/accurate) weights and manifest.
- `api.py`: legacy FastAPI conversion API, port 8087.
- `run.py`: launcher that always selects this folder's code and models.
- `tools/`: API/preview launchers and offline model verification helper.
- `pyproject.toml`, `requirements.txt`: Python installation requirements.
- `Dockerfile`, `docker-compose.yml`, `.env`: container configuration.

Regression tests are under `tests/`. Runtime DTD/schema modules are required application resources.
Model repository README files are retained as model provenance supplied by the manifest.

## Run on Windows

Python 3.11-3.13 is supported. From this application folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe tools/prepare_models.py --check
.\.venv\Scripts\python.exe run.py api
```

API startup requires credentials; see Production job API below. Health endpoints remain public.
Dependencies require an initial install; packaged models need no download during conversion.
For CPU-only dependency installation, install torch/torchvision from the PyTorch CPU index
before installing requirements, as done in the Dockerfile.

Convert a PDF (replace the example input path):

CLI defaults are canonical JSON/XML, with overlays and intermediate debug dumps disabled.
Use `--to bits,jats,content,epub,docx` for additional formats, `--overlays` for QA images,
and `--debug-artifacts` for extraction diagnostics. The upload UI offers explicit format
and QA-image checkboxes; it defaults to canonical and Content XML outputs.
TableFormer Accurate remains the default. Use `--table-mode fast` in the CLI or
`table_mode=fast` in API requests to compare the smaller model on your corpus. The upload UI
also exposes this choice. Fast and Accurate entries use separate cache keys; complex tables
still require quality review. Switching modes can keep two converters resident in one worker.

```powershell
.\.venv\Scripts\python.exe run.py convert "C:\path\book.pdf" -o out --engine docling
```

Reuse existing canonical JSON without running PDF analysis again:

```powershell
.\.venv\Scripts\python.exe run.py export "C:\path\converted-book" --to bits,jats,content
```

PDF-left/XML-right Layout QA and upload UI:

```powershell
.\.venv\Scripts\python.exe run.py preview "C:\path\output-root" --pdf-root "C:\path\pdfs" --no-browser
```

Open http://127.0.0.1:8765/inspect or /upload. The preview and legacy API are separate servers.
`output-root` contains document conversion folders; `pdfs` contains their original PDFs.
Output directories are created when needed; conversion inputs/results are not bundled here.

## Production job API

Configure `PDF2XML_API_KEYS` as a JSON map of stable tenant names to unique bearer tokens of
at least 32 characters. Generate tokens using Python's `secrets.token_urlsafe(32)` and store
them in your environment secret store, never in source control. Send
`Authorization: Bearer <token>` on every request except health checks. Jobs, cancellation,
downloads and metrics are restricted to their owning tenant. Token rotation preserves access
when tenant names stay unchanged. Credentials are not stored in job records.

For local development only, explicitly enable anonymous access before starting the server:

```powershell
$env:PDF2XML_ALLOW_ANONYMOUS = "1"
.\.venv\Scripts\python.exe run.py api --host 127.0.0.1
```

Use TLS at shared ingress. The preview UI is a local administration surface, not the tenant API.

Prefer `POST /jobs?targets=json,xml` with a multipart `file` field. It returns HTTP 202
and a job ID immediately. Poll `GET /jobs/{id}` for state, current stage, queue wait,
elapsed time, and quality findings. Once state is `done`, use
`GET /jobs/{id}/artifacts` to discover downloadable outputs. `DELETE /jobs/{id}` cancels
queued or running work. A completed conversion can still require human review; inspect
the quality findings before accepting its outputs.

`POST /convert` remains a synchronous compatibility endpoint and uses the same worker queue.
Its default targets are now `json,xml`; request EPUB, DOCX, BITS, JATS or Content XML explicitly.
Legacy file responses are retained on disk rather than deleted after download so job records
and subsequent downloads remain consistent.

The queue keeps models loaded between conversions, limits outstanding work to 32 jobs,
and stops a conversion after 1800 seconds. Override these with `PDF2XML_MAX_PENDING` and
`PDF2XML_JOB_TIMEOUT_S`. A full queue returns HTTP 503. Both include running work in their
capacity/deadline semantics; the deadline starts when processing starts, not at upload.
Uploads remain limited by `PDF2XML_MAX_UPLOAD_MB` (default 300).
The request stream is bounded before multipart parsing, with 1 MiB allowed for multipart overhead.
`PDF2XML_MAX_PAGES` limits whole PDFs to 1000 pages before extraction.

Job records are atomically replaced under `out/_jobs`. After restart, interrupted jobs are
marked failed with a resubmission message; they are not automatically retried. Results use
job-specific directory names, so identical uploaded filenames cannot overwrite each other.
One server process owns each output root using an operating-system file lock. Do not run
multiple Uvicorn workers or API and preview servers against the same root concurrently.
Automatic retention runs approximately every minute. `PDF2XML_RETENTION_HOURS` defaults to 24;
expired terminal jobs lose their input, outputs and record, then return 404. Queued/running jobs
are preserved. Cache entries expire after the same period of inactivity. Only owned job folders
are deleted; unrelated files remain. Cleanup errors are logged for operators.

Tenant-scoped extraction caching reuses raw Docling inference by PDF hash, page selection,
library/options signature and model identity. Recovery, merging, requested exports and QA rerun.
Entries have checksums and atomic publication; invalid entries are ignored. Verified model
manifests and model file metadata invalidate entries after replacement. API jobs skip large
per-job style/raw debug dumps. Required figure/formula assets remain preserved; API overlays stay off.

`GET /health/live` checks HTTP availability. `GET /health/ready` checks queue/listener availability,
Docling installation and required local model files; it does not run inference or warm models.
`report.json` includes triage, recovery, coverage, quality and total timings in addition to
the existing stage timings. Total time covers conversion through the quality report, excluding
upload, queue wait, and final report serialization.
Authenticated `GET /metrics` returns tenant-scoped state and review counts. JSON logs include job IDs,
request IDs, states and durations. Responses include `X-Request-ID`. Requested outputs that fail
schema validation fail the job; other quality findings remain available for review.
Prometheus text metrics are available at `/metrics/prometheus`; starter scrape and alert rules
are under `monitoring/`.

Run regression checks with:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c requirements.lock.txt pytest httpx psutil
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe tools/make_regression_corpus.py out/regression-inputs
.\.venv\Scripts\python.exe tools/benchmark.py out/regression-inputs/corpus.json --out out/benchmark --repeat 2 --cache
```

The benchmark measures wall time, sampled peak process RSS, pages/second, cache hits and reference
checks for text presence/order, table cells, coverage and schemas. First conversion includes cold
model loading; later documents use warm models. Cached repeats are identified separately. Failed
checks produce a nonzero exit. The generated corpus is synthetic: add reviewed production PDFs
using the same manifest format before setting production performance/quality gates.

`requirements.lock.txt` pins the tested environment; constraints do not install unrelated packages.
The CI workflow defines Windows/Linux tests, Docling benchmarks and Docker smoke checks. This is
a single-host service; multi-host scheduling requires a database and external scheduler. Actual
Linux/container validation remains pending until CI or the documented deployment checks pass.
See [operations](docs/OPERATIONS.md) for deployment checks, monitoring thresholds and rollback.
See [recorded validation](docs/VALIDATION.md) for the supplied book's measured performance,
the index text-loss fix, review findings and remaining deployment verification.
See [Quality Report](docs/QUALITY_REPORT.md) for full page/font/artifact evidence, processing
statistics, printable reports and separate BITS/JATS/Content XML validation downloads.

## Docker

```powershell
docker compose up --build -d
```

The build verifies bundled weights offline, uses no test fixture and does not download models.
Building the Python/system dependency layers still needs network access. Results use `./out`.
The container runs as UID 10001; Linux bind-mounted output directories must be writable by that UID.

Content XML (`42_content.xml`) has titles, paragraphs, tables and inline emphasis only;
no IDs, font size/family/color or layout metadata. BITS/JATS/canonical outputs keep their
respective structures. Schema validity does not imply editorial correctness.
