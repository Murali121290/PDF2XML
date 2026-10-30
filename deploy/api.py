"""pdf2xml API server (deploy edition): the conversion API for a frontend, nothing else.

    python -m pdf2xml.api                      # settings from the environment, see below
    python -m pdf2xml.api --data /data --port 8765 --cors-origin https://app.example.com

Routes (same as the full server's API; docs/HANDOVER-FRONTEND.md section 5):
``POST /api/convert``, ``GET /api/jobs``, ``GET|DELETE /api/jobs/<id>``,
``POST /api/jobs/<id>/cancel``, ``GET /files/<doc>/<file>``, ``GET /api/report?doc=``,
``GET /api/validate?doc=&file=``, ``GET /api/source?doc=&file=``, ``GET /api/docs``,
``GET /api/health``. Conversions use Docling only and write canonical JSON/XML, BITS and JATS.

Environment (each also a command-line option):

=========================  =========================================================
PDF2XML_DATA               folder for uploads and results (default /data)
PDF2XML_HOST / _PORT       interface and port (default 0.0.0.0 and 8765)
PDF2XML_CORS_ORIGINS       comma list of frontend origins allowed to call the API
PDF2XML_ALLOWED_HOSTS      comma list of names clients use in the Host header;
                           "*" (default) accepts any, for a server behind a proxy
PDF2XML_MAX_UPLOAD_MB      largest PDF accepted (default 300)
PDF2XML_MODELS_DIR         the Docling models (set by the image)
=========================  =========================================================
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
from pathlib import Path
from typing import Any

TARGETS = ("json", "xml", "bits", "jats")


def _env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in (os.environ.get(name) or default).split(",") if x.strip()]


def _stop(*_: Any) -> None:
    raise KeyboardInterrupt        # `docker stop` sends SIGTERM: shut down like Ctrl+C


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pdf2xml.api", description="pdf2xml conversion API "
                                 "(PDF → Docling → canonical JSON/XML → BITS → JATS)")
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("PDF2XML_DATA", "/data")),
                    help="folder for uploads and results (default: %(default)s)")
    ap.add_argument("--host", default=os.environ.get("PDF2XML_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PDF2XML_PORT", "8765")))
    ap.add_argument("--cors-origin", action="append",
                    default=_env_list("PDF2XML_CORS_ORIGINS"),
                    help="origin of a frontend served elsewhere; repeatable")
    ap.add_argument("--allowed-host", action="append",
                    default=_env_list("PDF2XML_ALLOWED_HOSTS", "*"),
                    help='name clients use in the Host header; repeatable; "*" for any')
    ap.add_argument("--max-upload-mb", type=int,
                    default=int(os.environ.get("PDF2XML_MAX_UPLOAD_MB", "300")))
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: "
                        "%(message)s")
    from pdf2xml.cli import _models
    from pdf2xml.jobs import ConversionQueue
    from pdf2xml.preview import serve

    models = _models()                      # fails now, not at the first upload
    args.data.mkdir(parents=True, exist_ok=True)
    jobs = ConversionQueue(args.data, default_engine="docling",
                           max_upload_mb=args.max_upload_mb, models_dir=models,
                           engines=("docling",), targets=TARGETS, qa=False, views=False)
    signal.signal(signal.SIGTERM, _stop)
    print(f"  Docling models: {models}", flush=True)
    serve(args.data, host=args.host, port=args.port, open_browser=False,
          cors=args.cors_origin, allowed_hosts=args.allowed_host, api_only=True, jobs=jobs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
