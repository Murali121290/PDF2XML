# Deploy the latest code from dev-Sadha

Use the existing GitHub repository and deploy **dev-Sadha**. Do not merge into main.
The existing FastAPI service remains on port **8087**, using **POST /convert** with the
multipart field **file**. Existing XML responses and `return_xml=all` ZIP downloads remain
available. Docling is the default engine; EPUB, DOCX, BITS and JATS remain supported.

## First deployment with Docker

On the server, from the repository checkout:

```bash
git status --short
git fetch origin
git switch dev-Sadha
git pull --ff-only origin dev-Sadha
docker compose build pdf2xml-api
docker compose up -d pdf2xml-api
docker compose ps
docker compose logs --tail=100 pdf2xml-api
```

If there are local changes, preserve/reconcile them before switching or pulling. If this
is a fresh checkout, `git switch --track origin/dev-Sadha` creates the tracking branch.
Keep any existing Compose overrides, reverse proxy configuration and local `.env` settings.
Compose reads `PDF2XML_CORS_ORIGINS` and `PDF2XML_MAX_UPLOAD_MB` from the environment or `.env`.

The first build installs packages and downloads the pinned layout/TableFormer model files.
It verifies their sizes and SHA-256 checksums, packages them inside the image, and converts
a sample PDF in a build step with networking disabled. Docker 23+ / BuildKit is required.
Allow approximately 8 GB RAM for the service.

## Download once, then use local models

- Models are downloaded during setup/build, never by an API conversion.
- The Docker model stage is independent of application code. On the same builder, ordinary
  code updates reuse the verified model layer. Preserve Docker's build cache.
- Every running container reads `/app/models`. Restarting reloads those local files; it
  does not download them. The first conversion loads the model into the persistent worker,
  and later requests reuse it. Conversion requests run one at a time in that worker.
- A fresh builder or a deleted model cache needs setup again. To keep weights independently
  of Docker's cache, prepare and retain the host's ignored `models/` folder:

  ```bash
  # Using a Python 3.11–3.13 environment with the project's ML dependencies installed:
  python -m pip install -e ".[ml]"
  python tools/prepare_models.py
  python tools/prepare_models.py --check
  ```

  Re-running preparation verifies existing weights without downloading valid files. Docker
  copies and verifies these local files when present. Do not commit the weights to Git.

Existing verified models can be copied without downloading:

```bash
python tools/prepare_models.py --source /path/to/existing/models
```

Without Docker, run the preparation step once, then start the API:

```bash
python -m uvicorn api:app --host 0.0.0.0 --port 8087
```

`PDF2XML_MODELS_DIR` can point to another complete local model folder. Missing model files
fail with a preparation instruction; conversion never falls back to fetching them.

## Subsequent updates

Before deployment, record the running commit and preserve/tag its image for rollback.
From a clean checkout already on dev-Sadha:

```bash
git rev-parse HEAD
git pull --ff-only origin dev-Sadha
docker compose build pdf2xml-api
docker compose up -d pdf2xml-api
docker compose logs --tail=100 pdf2xml-api
```

Building before restarting leaves the existing service running if the build fails.
Schedule recreation after active requests finish. Mounted output data is retained.

## Smoke test

```bash
curl --fail-with-body http://localhost:8087/
curl --fail-with-body \
  -F "file=@tests/fixtures/pdfs/simple/simple.pdf" \
  "http://localhost:8087/convert?targets=json,xml,bits,jats&return_xml=all" \
  -o /tmp/pdf2xml-check.zip
unzip -l /tmp/pdf2xml-check.zip
```

The ZIP should contain `30_canonical.xml`, `40_bits.xml` and `41_jats.xml`. Run a second
conversion to confirm the service continues to work with its already-loaded local models.
The legacy API cleans temporary outputs after each response. The CLI and job-based API
also support persistent results and completeness reports; see HANDOVER-FRONTEND.md.

Optional GROBID integration is included but disabled by default. Leave
`PDF2XML_GROBID_URL` unset when conversion must not call a separate reference service.
