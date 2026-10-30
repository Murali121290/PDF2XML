# pdf2xml

The conversion core is updated to **0.5.3**, while the existing FastAPI `/convert` contract,
port **8087**, EPUB/DOCX exporters, and `src/pdf2xml` layout are retained. **Docling is now
the default engine**, using pre-downloaded local models with runtime internet access disabled.
Existing web-app uploads and direct XML responses continue to work.

This branch includes the latest local conversion code: completeness reports, export/image/
Unicode checks, the current job and preview APIs, and optional GROBID integration. Server
deployment instructions are in [docs/DEVOPS.md](docs/DEVOPS.md). Deploy **`dev-Sadha`**;
`main` is unchanged.

Convert born-digital PDFs into a canonical JSON/XML document, then into EPUB 3, DOCX and the NISO publishing standards **BITS 2.2** and **JATS 1.4**. 

This project includes a robust FastAPI server allowing you to run these conversions via an HTTP API.

## Requirements

- Python **3.11, 3.12, or 3.13** (Note: Python 3.14 is not supported yet)

## Installation

### Linux / macOS

1. **Create and Activate a Virtual Environment:**
   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install Dependencies:**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   pip install -e ".[ml]"
   ```

3. **Prepare Models Once:**
   ```bash
   python tools/prepare_models.py
   ```

4. **Start the API Server:**
   ```bash
   uvicorn api:app --reload --port 8087
   ```

### Windows (PowerShell)

> [!IMPORTANT]
> - Ensure **Python 3.11, 3.12, or 3.13** is installed (`winget install Python.Python.3.11`). Python 3.14 is currently not supported.
> - Ensure **Microsoft Visual C++ Redistributable (x64)** is installed (`winget install --id Microsoft.VCRedist.2015+.x64 -e`). This is required by native packages like `lxml`, `pikepdf`, and `pydantic-core`.

1. **Create and Activate a Virtual Environment:**
   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   *(If script execution is disabled in PowerShell, first run: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`)*

2. **Install Dependencies:**
   ```powershell
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   pip install -e ".[ml]"
   ```

   > [!TIP]
   > If you encounter dependency conflicts with Pillow or compiled packages on Windows, lock Pillow to `<13`:
   > ```powershell
   > pip install "pillow>=10.0.0,<13.0.0"
   > ```

3. **Prepare Models Once:**
   ```powershell
   python tools/prepare_models.py
   ```

   If you already have the models, copy and verify them without internet access:
   ```powershell
   python tools/prepare_models.py --source D:\path\to\existing\models
   ```

4. **Start the API Server:**
   ```powershell
   uvicorn api:app --reload --port 8087
   ```

This stores the layout and TableFormer models in `models/`. Conversion never downloads
models. Missing or incomplete local models produce an error instead of a network fallback.
`python tools/prepare_models.py --check` verifies all pinned checksums without downloading.

## Running the API Server

The project includes a standalone FastAPI application (`api.py`) for PDF conversions.

1. Start the server on port 8087 (or your preferred port):
```bash
.venv/bin/uvicorn api:app --reload --port 8087
```

2. Once running, explore the interactive API documentation and test file uploads directly from your browser: 
[http://localhost:8087/docs](http://localhost:8087/docs)

`python api.py` also starts the server on port 8087. Conversions run one at a time in a
persistent child process, keeping model loading and XSLT processing away from HTTP handling.
The request still waits for its final result; there is no job ID or polling requirement.

### API Usage Example (cURL)

Convert a PDF and return the raw JATS XML output directly in the response:

```bash
curl -X 'POST' \
  'http://localhost:8087/convert?targets=json,xml,jats,bits&return_xml=jats' \
  -H 'accept: application/xml' \
  -F 'file=@/path/to/your/document.pdf'
```

The multipart field is still `file`. The existing query parameters are unchanged:

| Parameter | Default | Supported values |
|---|---|---|
| `engine` | `docling` | `heuristic`, `docling`, `auto` |
| `targets` | `json,xml,epub,docx` | Comma list of `json,xml,epub,docx,bits,jats` |
| `return_xml` | omitted | `jats`, `bits`, `rawxml`; include the corresponding target. `all` returns a ZIP of `rawxml`, `bits` and `jats`, so all three must be targets |
| `pages` | all pages | Optional 1-based selection such as `1-3,7` |

Without `return_xml`, the response retains the same fields: `status`, `filename`,
`output_directory`, `blocks`, `coverage`, and `xml_valid`. With `return_xml`, it returns
`application/xml` directly (`application/zip` for `all`). Errors retain FastAPI's `detail` field. Invalid options are
rejected with HTTP 400, and an XML target that was not requested returns HTTP 404.

Temporary uploads and outputs are removed after the response, as in the previous API.
`output_directory` describes the temporary conversion location; it is not a download URL.
Use `return_xml` to retrieve the XML content.

## Docker and environment settings

From this folder, rebuild the service after updating the source:

```bash
docker compose up -d --build
```

The service remains on `http://localhost:8087`. The image contains CPU-only PyTorch and
verified local models. A build reuses weights already present in `models/`; on a fresh clone,
it downloads the pinned model revisions during the build. Package installation and the base
image also require internet access or a local mirror/cache at build time.

Models are prepared in a separate Docker stage. Application-only updates reuse that stage
on the same builder. Keep its build cache, or prepare `models/` on the host once and keep
that directory across Git pulls. A fresh builder without local weights needs to download
the models again; service restarts always read the files already in the image.

The finished container requires **no internet for conversion**, including its first request.
An image self-test runs with Docker networking disabled during the build. The first Docling
request loads models from local disk; later requests reuse the same worker and converter.
After a restart, the worker reloads those local files. Allow approximately 8 GB of container
memory. Docker 23+ / BuildKit is required for the offline build self-test.

| Variable | Default | Purpose |
|---|---|---|
| `PDF2XML_DATA` | `out` locally; `/app/out` in Docker | Temporary uploads and conversion results |
| `PDF2XML_OUT` | unset | Alternative output-root setting when `PDF2XML_DATA` is unset |
| `PDF2XML_MAX_UPLOAD_MB` | `300` | Upload size limit; larger uploads return HTTP 413 |
| `PDF2XML_CORS_ORIGINS` | empty | Comma list of web-app origins, e.g. `http://localhost:5173` |
| `PDF2XML_MODELS_DIR` | project `models/`; `/app/models` in Docker | Local Docling layout and TableFormer models; no download fallback |
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` | `1` during conversion | Enforced by the API and Docling engine; model downloads are disabled |
| `PDF2XML_PORT` | `8087` | Port used by `python api.py`; Uvicorn's `--port` takes precedence when invoked directly |

Compose passes through `PDF2XML_MAX_UPLOAD_MB` and `PDF2XML_CORS_ORIGINS` from the host or
a local `.env` file. Models are packaged in the image, so no runtime model volume is required.
An alternative local model folder can be mounted read-only with `PDF2XML_MODELS_DIR` set to
its container path. It must contain both `docling-project--docling-layout-heron` and
`docling-project--docling-models`, including the layout configuration/weights and both
TableFormer modes. `models/manifest.json` records the exact revisions and checksums.
The approximately 530 MB of weights are excluded from Git and copied into the image at build time.

The update includes fixes for superscript placement, missed figures, chapter-opening art,
table-of-contents parsing, shared bibliographies/endnotes, and layout cache invalidation when
the selected pages change. Reference parsing uses the built-in parser by default. Optional
GROBID integration is available with `--grobid` or `PDF2XML_GROBID_URL`; leave it unset for
fully local conversion without an external reference-processing service.

Each conversion also writes `qa/completeness.json`, with a summary in `report.json`.
The existing `/convert` JSON response fields and XML/ZIP download contract are unchanged;
temporary conversion files are still removed after the response. Use the CLI or the
job-based API described in `docs/HANDOVER-FRONTEND.md` when persistent reports are needed.

## Verification

Install the project and test dependencies, then run:

```bash
pip install -e ".[ml]" pytest httpx reportlab epubcheck
python -m pytest -m "not slow"
```

EPUB validation requires Java. With local models prepared, also run
`python -m pytest -m slow` for ML conversion checks. The default-Docling API test uses an
empty Hugging Face cache and blocks all outbound socket connections in the conversion worker.

## Command Line Interface (CLI)

You can also use the original built-in command-line tools provided by `pdf2xml`:

```bash
pdf2xml convert my_document.pdf -o out
```

The CLI also defaults to offline Docling. `--engine heuristic` remains available explicitly.

## How It Works

The core logic of `pdf2xml` relies on a highly accurate two-layer extraction pipeline that ensures **no text is ever dropped** during the conversion:

1. **Style Layer (`pdfplumber`)**: 
   The pipeline extracts every single word from the PDF, recording its exact font, size, color, bold/italic weight, and physical bounding box (coordinates). It knows *how text looks*.
2. **Structure Layer (`docling` or `heuristic`)**: 
   A separate engine extracts the geometrical blocks (headings, paragraphs, lists, tables) and determines the overall reading order of the document. It knows *what the blocks are*.
3. **Canonical Merge**:
   The two layers are merged together by geometry. Each word is assigned to the structural block covering the largest share of its area. If a word falls outside a block, it is attached to a nearby block or inserted into the reading order—guaranteeing 100% text coverage.
4. **Enrichment and Export**:
   The merged data becomes the **Canonical JSON/XML Document**. This flat canonical document is then parsed to build hierarchical semantic structures (like `book`, `chapter`, `sec`, references, and footnotes). Finally, it's serialized into the desired target standards like **BITS 2.2** or **JATS 1.4**, passing through strict NLM DTD schema validation.
