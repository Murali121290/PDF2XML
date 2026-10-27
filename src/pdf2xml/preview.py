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

import contextlib
import ipaddress
import json
import mimetypes
import multiprocessing
import re
import threading
import webbrowser
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from email.message import Message
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

    def __init__(self, timeout: float = 300) -> None:
        self._pool: ProcessPoolExecutor | None = None
        self._lock = threading.Lock()
        self.timeout = timeout

    def run(self, text: str, sheet: Path, params: dict[str, str], stamp: str) -> str:
        result: str = self.call(_transform, text, str(sheet), params, stamp)
        return result

    def call(self, fn: Any, *args: Any) -> Any:
        """``fn(*args)`` in the worker process (``fn`` must be picklable)."""
        with self._lock:
            if self._pool is None:
                self._pool = ProcessPoolExecutor(
                    max_workers=1, mp_context=multiprocessing.get_context("spawn"))
            pool = self._pool
            fut = pool.submit(fn, *args)
        try:
            return fut.result(timeout=self.timeout)
        except TimeoutError as e:               # a runaway transform would block every later one
            self._reset(pool)
            raise RuntimeError(f"the XSLT transform took longer than {self.timeout:g} s "
                               "and was stopped") from e
        except BrokenProcessPool as e:          # the worker died: start a new one next time
            self._reset(pool)
            raise RuntimeError(f"XSLT worker stopped: {e}") from e

    def _reset(self, pool: ProcessPoolExecutor) -> None:
        """Stop ``pool``'s worker; the next call starts a new one. Only the pool that failed
        is dropped: another thread may already have replaced it."""
        with self._lock:
            if self._pool is pool:
                self._pool = None
        # the executor has no public way to stop a busy worker
        procs: dict[int, Any] = getattr(pool, "_processes", None) or {}
        for proc in list(procs.values()):
            with contextlib.suppress(OSError, ValueError):
                proc.kill()
        pool.shutdown(wait=False, cancel_futures=True)

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

def _form_parts(ctype: str, body: bytes) -> Iterator[tuple[Message, memoryview]]:
    """The parts of a ``multipart/form-data`` body: headers, and the content as a view into
    ``body``. Nothing is copied, so a 300 MB upload costs 300 MB, not several times that
    (the ``email`` parser decodes the whole body to text and copies it again)."""
    head = Message()
    head["Content-Type"] = ctype
    boundary = head.get_param("boundary")
    if not isinstance(boundary, str) or not boundary:
        raise JobError(400, "bad_form", "the multipart form has no boundary")
    delim = b"--" + boundary.encode("latin-1")
    view = memoryview(body)
    pos = body.find(delim)
    if pos < 0:
        raise JobError(400, "bad_form", "the multipart form is empty")
    pos += len(delim)
    while not body.startswith(b"--", pos):          # "--boundary--" closes the form
        if not body.startswith(b"\r\n", pos):
            raise JobError(400, "bad_form", "the multipart form is malformed")
        end_head = body.find(b"\r\n\r\n", pos)
        end = body.find(b"\r\n" + delim, end_head + 4) if end_head >= 0 else -1
        if end < 0:
            raise JobError(400, "bad_form", "the multipart form is cut off")
        # browsers send non-ASCII file names as raw UTF-8
        headers = BytesParser(policy=email_policy).parsebytes(
            bytes(body[pos + 2:end_head + 4]), headersonly=True)
        yield headers, view[end_head + 4:end]
        pos = end + 2 + len(delim)


def _parse_upload(ctype: str, body: bytes, q: dict[str, str],
                  header_name: str | None) -> tuple[bytes | memoryview, str, dict[str, str]]:
    """The PDF, its file name and the options of a ``POST /api/convert``: either the raw PDF
    as the body (``?filename=…`` or an ``X-Filename`` header) or a multipart form with a
    ``file`` field. Options come from the query string and, for a form, its other fields."""
    fields = dict(q)
    if ctype.lower().startswith("multipart/form-data"):
        data: memoryview | None = None
        filename = ""
        for headers, content in _form_parts(ctype, body):
            name = headers.get_param("name", header="content-disposition") or ""
            if headers.get_filename() is not None or name == "file":
                data, filename = content, headers.get_filename() or ""
            elif name:
                fields[str(name)] = bytes(content).decode("utf-8", "replace").strip()
        if data is None:
            raise JobError(400, "no_file", "the form has no 'file' field")
        return data, filename, fields
    filename = q.get("filename") or unquote(header_name or "")
    return body, filename, fields


def host_allowed(host_header: str, names: Sequence[str] = ()) -> bool:
    """Whether a request's ``Host`` names this server: an IP address, ``localhost``, or a name
    the server was started with. Anything else is DNS rebinding: a web site whose name was
    pointed at 127.0.0.1 would otherwise count as this server's own origin, and its scripts
    could read every conversion and upload PDFs. ``"*"`` in ``names`` accepts any name: for a
    server behind a reverse proxy or in a container, where the proxy checks the name."""
    host = host_header.strip().lower()
    if host.startswith("["):                          # [::1]:8765
        host = host[1:host.find("]")] if "]" in host else ""
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    if not host:
        return False
    if "*" in names:
        return True
    try:
        ipaddress.ip_address(host)
        return True                                   # rebinding needs a name, not an address
    except ValueError:
        pass
    return host == "localhost" or host.endswith(".localhost") or host in names


# routes of the proof viewer (web pages, XSLT rendering, QA page images); an API-only server
# (a deployment that only converts) answers 404 for them
_VIEW_ONLY = {"", "upload", "view", "api/stamp", "api/pages"}


def _handler(viewer: Viewer, jobs: ConversionQueue | None = None,
             cors: Sequence[str] = (), hosts: Sequence[str] = (),
             api_only: bool = False) -> type[BaseHTTPRequestHandler]:
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

        def _bad_host(self, as_json: bool = True) -> bool:
            """Refuse a request whose Host is not this server (see ``host_allowed``)."""
            if host_allowed(self.headers.get("Host", ""), hosts):
                return False
            self._discard_body()
            if as_json:
                self._error(403, "host", "unknown Host header; open the server by its address "
                                         "or as localhost")
            else:
                self._send(403, "unknown Host header", "text/plain")
            return True

        def _discard_body(self) -> None:
            """Read the body of a request refused before it was read, so the client gets the
            answer: closing a socket with unread data resets the connection (on Windows), and
            the client sees a connection error instead of the 403. A large body is not read;
            the connection closes after the answer."""
            if getattr(self, "_body_read", False):
                return
            self._body_read = True
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if 0 < n <= 1 << 20:
                self.rfile.read(n)
            elif n:
                self.close_connection = True

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
            """The request's path and query; called first by every method, so it also starts
            the request's state (one handler serves every request of a kept-alive connection)."""
            self._body_read = False
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            return [unquote(p) for p in url.path.split("/") if p], q

        # ---------------------------------------------------- methods
        def do_OPTIONS(self) -> None:  # noqa: N802 (http.server naming)
            """CORS preflight for a frontend served from another origin."""
            if self._bad_host():
                return
            if not cors or self._allowed_origin() is None:
                self._error(403, "origin", "this origin may not use the API")
                return
            self._send(204, b"", "text/plain", {
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, X-Filename",
                "Access-Control-Max-Age": "600"})

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            parts, q = self._parts()
            if self._bad_host(as_json=parts[:2] == ["api", "jobs"]):
                return
            try:
                if parts[:2] == ["api", "jobs"]:
                    self._jobs_get(parts)
                elif parts == ["api", "health"]:
                    self._json(self._health())
                elif api_only and ("/".join(parts[:2]) in _VIEW_ONLY
                                   or (parts[:1] or [""])[0] in _VIEW_ONLY):
                    raise NotFound(f"{self.path} (this server only converts)")
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
            if self._bad_host():
                return
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
                self._discard_body()
                self._error(e.status, e.code, e.message)
            except Exception as e:                                  # keep the server up
                self._discard_body()
                self._error(500, "server_error", f"{type(e).__name__}: {e}")

        def do_DELETE(self) -> None:  # noqa: N802 (http.server naming)
            parts, _ = self._parts()
            if self._bad_host():
                return
            try:
                if self._foreign():
                    raise JobError(403, "origin", "requests from other web sites are refused")
                if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                    self._json(self._queue().cancel(parts[2]).to_json())
                else:
                    raise JobError(404, "not_found",
                                   f"no route DELETE {urlparse(self.path).path}")
            except JobError as e:
                self._discard_body()
                self._error(e.status, e.code, e.message)
            except Exception as e:                                  # keep the server up
                self._error(500, "server_error", f"{type(e).__name__}: {e}")

        # ---------------------------------------------------- conversion jobs
        def _health(self) -> dict[str, Any]:
            """For load balancers and container health checks: the server answers, and how
            busy the queue is."""
            from pdf2xml import __version__

            states = [j.state for j in jobs.list()] if jobs is not None else []
            return {"status": "ok", "version": __version__, "converts": jobs is not None,
                    "queued": states.count("queued"), "running": states.count("running")}

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
            self._body_read = True
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
                jobs: ConversionQueue | None = None, cors: Sequence[str] = (),
                allowed_hosts: Sequence[str] = (), api_only: bool = False
                ) -> ThreadingHTTPServer:
    """``jobs``: accept PDF uploads and convert them (``POST /api/convert``); without it the
    server only views existing conversions. ``cors``: origins of a frontend served elsewhere
    (e.g. ``http://localhost:5173``) that may call the API. Requests must name the server by
    an IP address, ``localhost``, ``host`` or one of ``allowed_hosts`` (``"*"``: any; see
    ``host_allowed``). ``api_only``: no viewer pages or XSLT rendering, only the API."""
    names = tuple(n.strip().lower() for n in (host, *allowed_hosts) if n.strip())
    server = ThreadingHTTPServer((host, port), _handler(viewer, jobs, tuple(cors), names,
                                                        api_only))
    server.daemon_threads = True
    return server


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765,
          sheets: list[Path] | None = None, open_browser: bool = True,
          convert: bool = True, engine: str = "docling", max_upload_mb: int = 300,
          cors: Sequence[str] = (), models_dir: Path | None = None,
          grobid_url: str | None = None, allowed_hosts: Sequence[str] = (),
          api_only: bool = False, jobs: ConversionQueue | None = None) -> None:
    """``jobs``: a ready queue (the deploy package configures its own); else one is made
    when ``convert`` is set."""
    viewer = Viewer(root, sheets)
    if jobs is None and convert:
        jobs = ConversionQueue(viewer.root, default_engine=engine, max_upload_mb=max_upload_mb,
                               models_dir=models_dir, grobid_url=grobid_url)
    server = make_server(viewer, host, port, jobs, cors, allowed_hosts, api_only)
    url = f"http://{host}:{server.server_address[1]}/"
    kind = "API" if api_only else "preview"
    print(f"pdf2xml {kind}: {url}  (folder {viewer.root}; Ctrl+C to stop)", flush=True)
    if jobs is not None:
        page = "api/convert" if api_only else "upload"
        print(f"  upload and convert: {url}{page}  (engine {jobs.default_engine}, "
              f"max {jobs.max_upload // (1024 * 1024)} MB)", flush=True)
    for name in allowed_hosts:
        print(f"  Host allowed: {name}", flush=True)
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
