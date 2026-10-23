"""Local proof viewer: ``pdf2xml serve``.

A small web app over a conversion output folder (``out/``). It renders an XML file of any
conversion (BITS, JATS or canonical) through an XSLT stylesheet: the built-in proof sheet, the
EPUB sheet, or the user's own. It reloads when the XML, the stylesheet or its CSS changes on
disk, shows DTD/RelaxNG errors, the source with line numbers, the matching PDF page (the QA
overlay images) and the ``report.json`` checks.

Standard library HTTP server, bound to localhost; no extra dependencies. Saxon (saxonche) runs
the XSLT, one transform at a time.
"""

from __future__ import annotations

import json
import mimetypes
import multiprocessing
import re
import threading
import webbrowser
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from lxml import etree

from pdf2xml.jobs import ConversionQueue, JobError

PV_NS = "urn:pdf2xml:preview"
RESOURCES = Path(str(resources.files("pdf2xml") / "resources"))
UI_DIR = RESOURCES / "preview"
_XSLT_SUFFIXES = {".xsl", ".xslt"}
_WATCH_SUFFIXES = {".xsl", ".xslt", ".css"}
_PARSER = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False,
                          huge_tree=True)


def _transform(text: str, sheet: str, params: dict[str, str], stamp: str) -> str:
    from pdf2xml.xml.xslt import transform_file

    return transform_file(text, Path(sheet), params, stamp)


class _XsltWorker:
    """Runs every XSLT transform in one child process.

    saxonche is a native library that must be called on the thread that created it, while the
    HTTP server answers on many threads; a single worker process owns Saxon instead (and keeps
    its compiled stylesheets between requests).
    """

    def __init__(self) -> None:
        self._pool: ProcessPoolExecutor | None = None
        self._lock = threading.Lock()

    def run(self, text: str, sheet: Path, params: dict[str, str], stamp: str) -> str:
        with self._lock:
            if self._pool is None:
                self._pool = ProcessPoolExecutor(
                    max_workers=1, mp_context=multiprocessing.get_context("spawn"))
            fut = self._pool.submit(_transform, text, str(sheet), params, stamp)
        try:
            return fut.result(timeout=300)
        except BrokenProcessPool as e:          # the worker died: start a new one next time
            with self._lock:
                self._pool = None
            raise RuntimeError(f"XSLT worker stopped: {e}") from e

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.shutdown(cancel_futures=True)
                self._pool = None


class NotFound(Exception):
    pass


@dataclass(frozen=True)
class Sheet:
    id: str
    name: str
    path: Path
    assets: Path                   # where the sheet's relative links (CSS…) resolve
    builtin: bool = False
    params: dict[str, str] = field(default_factory=dict)


def _builtin_sheets() -> list[Sheet]:
    return [
        Sheet("proof", "BITS / JATS proof (built-in)", RESOURCES / "xslt" / "jats-preview.xsl",
              UI_DIR, True, {"css": "preview.css"}),
        Sheet("canonical", "Canonical → XHTML (EPUB sheet)",
              RESOURCES / "xslt" / "canonical-to-xhtml.xsl", UI_DIR, True,
              {"css": "canonical.css"}),
    ]


def _custom_sheets(paths: list[Path]) -> list[Sheet]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(f for f in p.iterdir() if f.suffix.lower() in _XSLT_SUFFIXES))
        elif p.is_file():
            files.append(p)
    out: list[Sheet] = []
    for i, f in enumerate(dict.fromkeys(f.resolve() for f in files)):
        out.append(Sheet(f"custom{i + 1}", f.name, f, f.parent))
    return out


class Viewer:
    """Everything the web UI asks for, independent of HTTP (so it can be tested directly)."""

    def __init__(self, root: Path, sheets: list[Path] | None = None) -> None:
        self.root = root.resolve()
        extra = list(sheets or [])
        if (self.root / "_xslt").is_dir():
            extra.append(self.root / "_xslt")
        self.sheets = {s.id: s for s in _builtin_sheets() + _custom_sheets(extra)}
        self._cache: dict[tuple[str, str], tuple[int, Any]] = {}
        self._xslt = _XsltWorker()

    def close(self) -> None:
        self._xslt.close()

    # ------------------------------------------------------------ lookup (path-safe)
    def doc_dir(self, doc: str) -> Path:
        d = (self.root / doc).resolve()
        if "/" in doc or "\\" in doc or d.parent != self.root or not d.is_dir():
            raise NotFound(f"no conversion folder {doc!r}")
        return d

    def xml_path(self, doc: str, file: str) -> Path:
        p = (self.doc_dir(doc) / file).resolve()
        if p.parent != self.doc_dir(doc) or p.suffix.lower() != ".xml" or not p.is_file():
            raise NotFound(f"no XML file {file!r} in {doc!r}")
        return p

    def sheet(self, sheet_id: str) -> Sheet:
        if sheet_id not in self.sheets:
            raise NotFound(f"no stylesheet {sheet_id!r}")
        return self.sheets[sheet_id]

    @staticmethod
    def inside(base: Path, rel: str) -> Path | None:
        p = (base / rel).resolve()
        return p if p.is_file() and p.is_relative_to(base.resolve()) else None

    # ------------------------------------------------------------ listings
    def docs(self) -> list[dict[str, Any]]:
        order = {"40_bits.xml": 0, "41_jats.xml": 1, "30_canonical.xml": 2}
        out = []
        for d in sorted(self.root.iterdir(), key=lambda p: p.name.lower()):
            if not d.is_dir() or d.name.startswith(("_", ".")):
                continue
            files = sorted((f.name for f in d.iterdir() if f.suffix.lower() == ".xml"),
                           key=lambda n: (order.get(n, 9), n))
            if files:
                out.append({"name": d.name, "files": files,
                            "pages": len(list((d / "qa").glob("page-*.png")))})
        return out

    def sheet_list(self) -> list[dict[str, Any]]:
        return [{"id": s.id, "name": s.name, "path": str(s.path), "builtin": s.builtin}
                for s in self.sheets.values()]

    # ------------------------------------------------------------ live reload
    def stamp(self, doc: str, file: str, sheet_id: str) -> str:
        """Changes whenever the XML, the stylesheet or a CSS/XSLT file next to it changes."""
        xml = self.xml_path(doc, file)
        s = self.sheet(sheet_id)
        watched = [xml, s.path]
        for d in {s.path.parent, s.assets}:
            watched.extend(f for f in d.iterdir() if f.suffix.lower() in _WATCH_SUFFIXES)
        return "-".join(str(p.stat().st_mtime_ns) for p in sorted(set(watched)))

    # ------------------------------------------------------------ rendering
    def render(self, doc: str, file: str, sheet_id: str) -> str:
        xml = self.xml_path(doc, file)
        s = self.sheet(sheet_id)
        root = etree.parse(str(xml), _PARSER).getroot()
        if s.id == "proof":
            # line numbers let the page link each element back to the source
            for el in root.iter(etree.Element):
                if el.sourceline is not None:
                    el.set(f"{{{PV_NS}}}line", str(el.sourceline))
        # the DOCTYPE is left out: Saxon must not fetch the DTD from the network
        text = etree.tostring(root, encoding="unicode")
        return self._xslt.run(text, s.path, s.params, self.stamp(doc, file, sheet_id))

    def asset(self, sheet_id: str, doc: str, rel: str) -> tuple[bytes, str]:
        """A file linked from a rendered page: the sheet's own files first, then the
        conversion folder (``assets/…`` figures)."""
        s = self.sheet(sheet_id)
        if rel == "canonical.css" and s.id == "canonical":
            return self.canonical_css(doc).encode("utf-8"), "text/css"
        for base in (s.assets, s.path.parent, self.doc_dir(doc)):
            p = self.inside(base, rel)
            if p is not None:
                return p.read_bytes(), _mime(p)
        raise NotFound(rel)

    def file(self, doc: str, rel: str) -> tuple[bytes, str]:
        p = self.inside(self.doc_dir(doc), rel)
        if p is None:
            raise NotFound(rel)
        return p.read_bytes(), _mime(p)

    # ------------------------------------------------------------ checks
    def source(self, doc: str, file: str) -> str:
        return self.xml_path(doc, file).read_text(encoding="utf-8", errors="replace")

    def validate(self, doc: str, file: str) -> dict[str, Any]:
        xml = self.xml_path(doc, file)
        try:
            tree = etree.parse(str(xml), _PARSER)
        except etree.XMLSyntaxError as e:
            return {"schema": "well-formedness", "errors": [
                {"line": e.lineno, "message": str(e.msg)}]}
        tag = etree.QName(tree.getroot()).localname
        if tag == "document":
            from pdf2xml.xml.serialize import validate as rng_validate
            schema, errors = "canonical (RelaxNG)", rng_validate(tree.getroot())
        else:
            from pdf2xml.publishing.schemas import detect, validate
            schema = detect(tree.getroot(), tree.docinfo.public_id)
            errors = validate(tree.getroot(), schema, limit=500)
        out = []
        for err in errors:
            m = re.match(r"line (\d+): (.*)", err, re.S)
            out.append({"line": int(m.group(1)) if m else None,
                        "message": m.group(2) if m else err})
        return {"schema": schema, "errors": out}

    def report(self, doc: str) -> dict[str, Any]:
        p = self.doc_dir(doc) / "report.json"
        if not p.exists():
            return {}
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data

    def pages(self, doc: str) -> dict[str, Any]:
        """PDF pages with a QA image, and printed page number → PDF page."""
        d = self.doc_dir(doc)
        nums = sorted(int(m.group(1)) for f in (d / "qa").glob("page-*.png")
                      if (m := re.fullmatch(r"page-(\d+)\.png", f.name)))
        return {"pages": nums, "folios": self._cached(doc, "folios", self._folios)}

    def _folios(self, d: Path) -> dict[str, int]:
        from pdf2xml.model import Artifact, Document, Meta, PageInfo
        from pdf2xml.publishing import Metadata, Profile
        from pdf2xml.publishing.semantic import _Builder

        p = d / "20_canonical.json"
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        doc = Document(meta=Meta.model_validate(data["meta"]),
                       pages=[PageInfo.model_validate(x) for x in data.get("pages", [])],
                       artifacts=[Artifact.model_validate(x) for x in data.get("artifacts", [])])
        b = _Builder(doc, Profile(), Metadata())
        b._folios()
        return {folio: page for page, folio in b.sem.folios.items()}

    def canonical_css(self, doc: str) -> str:
        def build(d: Path) -> str:
            from pdf2xml.export.css import build_css
            from pdf2xml.model import Document

            p = d / "20_canonical.json"
            if not p.exists():
                return ""
            return build_css(Document.model_validate_json(p.read_text(encoding="utf-8")))

        css: str = self._cached(doc, "css", build)
        return css

    def _cached(self, doc: str, what: str, fn: Any) -> Any:
        d = self.doc_dir(doc)
        src = d / "20_canonical.json"
        stamp = src.stat().st_mtime_ns if src.exists() else 0
        hit = self._cache.get((doc, what))
        if hit is None or hit[0] != stamp:
            hit = (stamp, fn(d))
            self._cache[(doc, what)] = hit
        return hit[1]


def _mime(p: Path) -> str:
    return mimetypes.guess_type(p.name)[0] or "application/octet-stream"


def _error_page(message: str) -> str:
    esc = (message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return ("<!doctype html><meta charset='utf-8'><title>Preview error</title>"
            "<body style='font:14px/1.5 system-ui,sans-serif;padding:2rem;color:#1d232b'>"
            "<h2 style='color:#b3261e'>The preview could not be rendered</h2>"
            f"<pre style='white-space:pre-wrap;background:#fdecea;padding:1rem'>{esc}</pre>"
            "<p>Fix the XML or the stylesheet and save: the preview reloads by itself.</p>")


# ---------------------------------------------------------------- HTTP

def _parse_upload(ctype: str, body: bytes, q: dict[str, str],
                  header_name: str | None) -> tuple[bytes, str, dict[str, str]]:
    """The PDF, its file name and the options of a ``POST /api/convert``: either the raw PDF
    as the body (``?filename=…`` or an ``X-Filename`` header) or a multipart form with a
    ``file`` field. Options come from the query string and, for a form, its other fields."""
    fields = dict(q)
    if ctype.lower().startswith("multipart/form-data"):
        msg = BytesParser(policy=email_policy).parsebytes(
            b"MIME-Version: 1.0\r\nContent-Type: " + ctype.encode("latin-1") + b"\r\n\r\n"
            + body)
        data: bytes | None = None
        filename = ""
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition") or ""
            if part.get_filename() is not None or name == "file":
                payload = part.get_payload(decode=True)
                data = payload if isinstance(payload, bytes) else b""
                filename = part.get_filename() or ""
            elif name:
                fields[str(name)] = str(part.get_content()).strip()
        if data is None:
            raise JobError(400, "no_file", "the form has no 'file' field")
        return data, filename, fields
    filename = q.get("filename") or unquote(header_name or "")
    return body, filename, fields


def _handler(viewer: Viewer, jobs: ConversionQueue | None = None,
             cors: Sequence[str] = ()) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pdf2xml-preview"

        def log_message(self, format: str, *args: Any) -> None:   # quiet: polling is chatty
            return

        # ---------------------------------------------------- origin checks and CORS
        def _allowed_origin(self) -> str | None:
            """The request's Origin when it may use this server: its own page, or an origin
            given with ``--cors-origin``. None for another site."""
            origin = self.headers.get("Origin")
            if not origin:
                return None
            host = self.headers.get("Host", "")
            if origin in (f"http://{host}", f"https://{host}") or origin in cors or "*" in cors:
                return origin
            return None

        def _foreign(self) -> bool:
            """A write from another web site (a form or script on some page the user has
            open): refused, so no site can upload to or cancel on this local server."""
            return bool(self.headers.get("Origin")) and self._allowed_origin() is None

        def _send(self, status: int, body: bytes | str, ctype: str,
                  headers: dict[str, str] | None = None) -> None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype + ("; charset=utf-8"
                                                       if ctype.startswith("text/")
                                                       or ctype.endswith("json") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            origin = self._allowed_origin()
            if origin and cors:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Expose-Headers", "Location")
                self.send_header("Vary", "Origin")
            for k, val in (headers or {}).items():
                self.send_header(k, val)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, data: Any, status: int = 200,
                  headers: dict[str, str] | None = None) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False), "application/json",
                       headers)

        def _error(self, status: int, code: str, message: str) -> None:
            self._json({"error": code, "message": message}, status)

        def _parts(self) -> tuple[list[str], dict[str, str]]:
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            return [unquote(p) for p in url.path.split("/") if p], q

        # ---------------------------------------------------- methods
        def do_OPTIONS(self) -> None:  # noqa: N802 (http.server naming)
            """CORS preflight for a frontend served from another origin."""
            if not cors or self._allowed_origin() is None:
                self._error(403, "origin", "this origin may not use the API")
                return
            self._send(204, b"", "text/plain", {
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-Filename",
                "Access-Control-Max-Age": "600"})

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            parts, q = self._parts()
            try:
                if parts[:2] == ["api", "jobs"]:
                    self._jobs_get(parts)
                else:
                    self._route(parts, q)
            except JobError as e:
                self._error(e.status, e.code, e.message)
            except NotFound as e:
                self._send(404, f"not found: {e}", "text/plain")
            except (KeyError, ValueError) as e:
                self._send(400, f"bad request: {e}", "text/plain")
            except Exception as e:                                  # keep the server up
                self._send(500, f"{type(e).__name__}: {e}", "text/plain")

        def do_POST(self) -> None:  # noqa: N802 (http.server naming)
            parts, q = self._parts()
            try:
                if self._foreign():
                    raise JobError(403, "origin", "requests from other web sites are refused")
                if parts == ["api", "convert"]:
                    self._convert(q)
                elif len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "cancel":
                    self._json(self._queue().cancel(parts[2]).to_json())
                else:
                    raise JobError(404, "not_found", f"no route POST {urlparse(self.path).path}")
            except JobError as e:
                self._error(e.status, e.code, e.message)
            except Exception as e:                                  # keep the server up
                self._error(500, "server_error", f"{type(e).__name__}: {e}")

        def do_DELETE(self) -> None:  # noqa: N802 (http.server naming)
            parts, _ = self._parts()
            try:
                if self._foreign():
                    raise JobError(403, "origin", "requests from other web sites are refused")
                if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                    self._json(self._queue().cancel(parts[2]).to_json())
                else:
                    raise JobError(404, "not_found",
                                   f"no route DELETE {urlparse(self.path).path}")
            except JobError as e:
                self._error(e.status, e.code, e.message)
            except Exception as e:                                  # keep the server up
                self._error(500, "server_error", f"{type(e).__name__}: {e}")

        # ---------------------------------------------------- conversion jobs
        def _queue(self) -> ConversionQueue:
            if jobs is None:
                raise JobError(503, "disabled", "conversions are not enabled on this server")
            return jobs

        def _convert(self, q: dict[str, str]) -> None:
            queue_ = self._queue()
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise JobError(411, "no_body", "send the PDF as the request body or a form")
            if length > queue_.max_upload + 1024 * 1024:            # a little room for a form
                self.close_connection = True
                raise JobError(413, "too_large", f"the upload is larger than "
                                                 f"{queue_.max_upload // (1024 * 1024)} MB")
            body = self.rfile.read(length)
            data, filename, fields = _parse_upload(self.headers.get("Content-Type", ""), body,
                                                   q, self.headers.get("X-Filename"))
            job = queue_.submit(data, filename, fields)
            self._json(job.to_json(), 202, {"Location": f"/api/jobs/{job.id}"})

        def _jobs_get(self, parts: list[str]) -> None:
            if len(parts) == 2:
                self._json({"jobs": [j.to_json() for j in self._queue().list()]})
            elif len(parts) == 3:
                self._json(self._queue().get(parts[2]).to_json())
            else:
                raise JobError(404, "not_found", "no such route")

        # ---------------------------------------------------- viewing (read-only)
        def _route(self, parts: list[str], q: dict[str, str]) -> None:
            v = viewer
            if not parts:
                self._send(200, (UI_DIR / "index.html").read_bytes(), "text/html")
            elif parts == ["upload"]:
                self._send(200, (UI_DIR / "upload.html").read_bytes(), "text/html")
            elif parts[0] == "api" and len(parts) == 2:
                name = parts[1]
                if name == "docs":
                    self._json({"root": str(v.root), "docs": v.docs(),
                                "sheets": v.sheet_list()})
                elif name == "stamp":
                    self._json({"stamp": v.stamp(q["doc"], q["file"], q["sheet"])})
                elif name == "source":
                    self._send(200, v.source(q["doc"], q["file"]), "text/plain")
                elif name == "validate":
                    self._json(v.validate(q["doc"], q["file"]))
                elif name == "report":
                    self._json(v.report(q["doc"]))
                elif name == "pages":
                    self._json(v.pages(q["doc"]))
                else:
                    raise NotFound(self.path)
            elif parts[0] == "view" and len(parts) >= 4:
                sheet, doc, rel = parts[1], parts[2], "/".join(parts[3:])
                if len(parts) == 4 and rel.lower().endswith(".xml"):
                    try:
                        html = v.render(doc, rel, sheet)
                    except NotFound:
                        raise
                    except Exception as e:
                        self._send(500, _error_page(str(e)), "text/html")
                        return
                    self._send(200, html, "text/html")
                else:
                    self._send(200, *v.asset(sheet, doc, rel))
            elif parts[0] == "files" and len(parts) >= 3:
                self._send(200, *v.file(parts[1], "/".join(parts[2:])))
            else:
                raise NotFound(self.path)

    return Handler


def make_server(viewer: Viewer, host: str = "127.0.0.1", port: int = 8765,
                jobs: ConversionQueue | None = None,
                cors: Sequence[str] = ()) -> ThreadingHTTPServer:
    """``jobs``: accept PDF uploads and convert them (``POST /api/convert``); without it the
    server only views existing conversions. ``cors``: origins of a frontend served elsewhere
    (e.g. ``http://localhost:5173``) that may call the API."""
    server = ThreadingHTTPServer((host, port), _handler(viewer, jobs, tuple(cors)))
    server.daemon_threads = True
    return server


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765,
          sheets: list[Path] | None = None, open_browser: bool = True,
          convert: bool = True, engine: str = "docling", max_upload_mb: int = 300,
          cors: Sequence[str] = ()) -> None:
    viewer = Viewer(root, sheets)
    jobs = (ConversionQueue(viewer.root, default_engine=engine, max_upload_mb=max_upload_mb)
            if convert else None)
    server = make_server(viewer, host, port, jobs, cors)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"pdf2xml preview: {url}  (folder {viewer.root}; Ctrl+C to stop)", flush=True)
    if jobs is not None:
        print(f"  upload and convert: {url}upload  (engine {engine}, "
              f"max {max_upload_mb} MB)", flush=True)
    for origin in cors:
        print(f"  CORS allowed for {origin}", flush=True)
    for s in viewer.sheets.values():
        if not s.builtin:
            print(f"  stylesheet {s.name}: {s.path}", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        viewer.close()
        if jobs is not None:
            jobs.close()
