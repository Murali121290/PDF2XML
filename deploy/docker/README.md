# pdf2xml conversion API: Docker deployment

This folder builds one container. The container converts born-digital PDFs over HTTP:

```
PDF → Docling layout analysis (CPU) → canonical JSON → canonical XML → BITS 2.2 → JATS 1.4
```

**The build needs no internet access.** This folder already holds:

- every Python package, as Linux wheels in `wheels/`;
- the Docling models (`models/`);
- the application (`app/`).

Only the base image `python:3.13-slim-bookworm` is pulled, from Docker Hub or your mirror. At run time the container makes no outbound connections.

The frontend API is described in **`API.md`**. Give that file to the frontend team.

## 1. Requirements

| | |
|---|---|
| Host | Linux x86-64 with Docker 23+ (BuildKit, the default builder) or Docker Desktop. No GPU needed |
| CPU | 4–8 cores recommended. One conversion uses all the cores it gets |
| Memory | 8 GB for the container (a 300-page book needs about 3–4 GB at its peak) |
| Disk | About 4 GB for the image, plus the data volume (a book's results: 50–150 MB) |
| Base image | `python:3.13-slim-bookworm`. On an air-gapped host: `docker pull` it elsewhere, `docker save`, then `docker load` |

## 2. Build

```bash
unzip pdf2xml-docker-<version>.zip
cd pdf2xml-docker-<version>
docker build -t pdf2xml-api:<version> .
```

The build does three things:

1. It installs the packages from `wheels/` with `pip --no-index`.
2. It swaps OpenCV for its headless build, because the slim image has no libGL.
3. It runs a **self-test**: it loads the models and converts `selftest.pdf`. A broken image fails the build here, not at the first upload.

The build takes 3–6 minutes. The image is about 3.5 GB.

## 3. Run

With Compose (edit the environment in `docker-compose.yml` first):

```bash
PDF2XML_VERSION=<version> docker compose up -d --build
docker compose logs -f          # "pdf2xml API: http://0.0.0.0:8765/ …"
curl http://localhost:8765/api/health
```

Or with `docker run`:

```bash
docker run -d --name pdf2xml-api --init --restart unless-stopped \
  -p 8765:8765 -v pdf2xml-data:/data --memory 8g \
  -e PDF2XML_CORS_ORIGINS=https://app.example.com \
  pdf2xml-api:<version>
```

Keep `--init` (`init: true` in Compose). The API runs conversions in a child process, and `--init` makes `docker stop` clean.

## 4. Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `PDF2XML_CORS_ORIGINS` | empty | Comma list of frontend origins that may call the API from a browser, for example `https://app.example.com,http://localhost:5173`. Not needed when the frontend and the API share one origin behind a reverse proxy |
| `PDF2XML_ALLOWED_HOSTS` | `*` | Names clients use in the `Host` header. `*` accepts any name, which is right behind a reverse proxy or gateway that checks names. If the container is reachable directly, list its names (`pdf2xml.example.com,pdf2xml-api`). Other names get `403 host`, which protects against DNS rebinding |
| `PDF2XML_MAX_UPLOAD_MB` | `300` | Largest PDF accepted |
| `PDF2XML_PORT` | `8765` | Port inside the container |
| `PDF2XML_DATA` | `/data` | Uploads and results. Mount a volume here |
| `OMP_NUM_THREADS` | all cores | CPU threads for the layout model |

The models (`PDF2XML_MODELS_DIR`) and the offline switches (`HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`) are set in the image. Do not change them.

## 5. Operations

- **Health:** `GET /api/health` returns `{"status":"ok", "queued":…, "running":…}`. The image has a `HEALTHCHECK` on it. Allow 60 s at start.
- **One worker per container.** Jobs run one at a time in upload order, and the job list is kept in memory. **Run one replica.** Behind a load balancer, several replicas would each have their own job list, so `GET /api/jobs/<id>` could reach the wrong one. For more throughput, run separate containers with separate URLs, or use sticky sessions.
- **Restarts:** queued and running jobs are lost. Finished results stay in `/data` and are listed by `GET /api/docs`.
- **Data volume** `/data`:
  - `/data/<doc>/` holds the results of each PDF.
  - `/data/_uploads/<job id>/` holds the uploaded PDF.

  Nothing is deleted automatically. Clean old folders with a cron job if needed, for example `find /data -mindepth 1 -maxdepth 1 -mtime +30 -exec rm -rf {} +`. The container runs as uid 10001. A bind-mounted host folder must be writable by that uid: `chown -R 10001 /srv/pdf2xml-data`.
- **Logs** go to stdout/stderr (`docker logs`).
- **Security:**
  - The API has no login. Put it behind your gateway, VPN or authentication proxy, and expose it only to the frontend.
  - Uploads are size-limited, and file paths cannot leave `/data`.
  - Browsers on other sites are refused unless they are listed in `PDF2XML_CORS_ORIGINS`.
- **Reverse proxy** (nginx), when the frontend and the API share a domain:

  ```nginx
  location /pdf2xml/ {
      proxy_pass http://pdf2xml-api:8765/;
      client_max_body_size 300m;          # = PDF2XML_MAX_UPLOAD_MB
      proxy_read_timeout 300s;
  }
  ```

  With that prefix, the frontend calls `/pdf2xml/api/convert`, and so on. The `download`/`source` links in job results start with `/`, so the frontend prepends its API base (`/pdf2xml`).

## 6. Batch conversion without the API (optional)

The same image converts files from the command line:

```bash
docker run --rm -v /srv/pdfs:/in:ro -v /srv/xml:/out pdf2xml-api:<version> \
  python -m pdf2xml /in -o /out
```

Each PDF in `/in` gets `/out/<name>/` holding `20_canonical.json`, `30_canonical.xml`, `40_bits.xml`, `41_jats.xml`, `assets/` and `report.json`.

## 7. Contents of this folder

| Path | What |
|---|---|
| `Dockerfile`, `docker-compose.yml`, `.dockerignore` | Build and run |
| `requirements.txt`, `wheels/` | Pinned Python packages, Linux x86-64, CPython 3.13, PyTorch CPU |
| `models/` | Docling layout (heron) and TableFormer models, with their licence files |
| `app/pdf2xml/` | The application: conversion and API only (no EPUB, DOCX, GROBID, previews) |
| `healthcheck.py`, `selftest.pdf` | Health check script; small test PDF for the build self-test |
| `API.md` | The HTTP API, for the frontend team |
| `THIRD-PARTY.txt`, `VERSION.txt` | Bundled libraries with licences; versions and build date |

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `docker build` fails at `pip install` with "No matching distribution" | The base image is not CPython 3.13 on x86-64. Use `python:3.13-slim-bookworm` on an amd64 host (on Apple Silicon or ARM: `docker build --platform linux/amd64`) |
| `RUN --mount` is rejected | An old builder. Use Docker 23+, or set `DOCKER_BUILDKIT=1` |
| Every request answers `403 host` | The name the clients use is not in `PDF2XML_ALLOWED_HOSTS`. Add it, or use `*` behind a proxy |
| The browser shows a CORS error | Add the frontend's exact origin (scheme + host + port) to `PDF2XML_CORS_ORIGINS` |
| A job fails with "the conversion process stopped unexpectedly" | Out of memory. Raise the container memory (8 GB) |
| A job fails with "no usable text layer" | A scanned PDF. OCR is not part of this service |
| The first job is slow | The models load once per container start (about 40 s) |
