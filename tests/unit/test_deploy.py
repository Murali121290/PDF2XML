"""The offline deploy package (``tools/build_deploy.py``) leaves modules out. The conversion
must run without them: they may only be imported inside the functions that use them."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import build_deploy  # noqa: E402


def _blocked_modules() -> list[str]:
    mods = ["pdf2xml." + m.removesuffix(".py").replace("/", ".")
            for m in build_deploy.SKIP_MODULES if m != "cli.py"]
    return [*mods, "typer", "saxonche", "docx"]          # libraries the package leaves out


def test_conversion_runs_without_the_modules_the_package_leaves_out(tmp_path: Path) -> None:
    code = textwrap.dedent(f"""
        import importlib.abc, sys
        BLOCKED = {_blocked_modules()!r}

        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name in BLOCKED:
                    raise ImportError("left out of the deploy package: " + name)
                return None

        sys.meta_path.insert(0, Block())
        from pathlib import Path
        from pdf2xml.config import Config
        from pdf2xml.model import Document, Heading, Meta, PageInfo, Paragraph, Run, Style
        from pdf2xml.pipeline import publish
        from pdf2xml.xml.serialize import to_xml, validate

        box = (50.0, 100.0, 550.0, 120.0)
        doc = Document(meta=Meta(source="t.pdf", sha256="0" * 64, pages=1),
                       styles={{"body": Style(font="Times", size=10.0, role="body"),
                                "h1": Style(font="Arial", size=18.0, bold=True,
                                            role="heading", level=1)}},
                       pages=[PageInfo(n=1, width=600, height=800)],
                       body=[Heading(id="b1", page=1, bbox=box, level=1, style="h1",
                                     runs=[Run(text="Title")]),
                             Paragraph(id="b2", page=1, bbox=box, style="body",
                                       runs=[Run(text="Text (Lee, 2018).")])])
        assert not validate(to_xml(doc))
        out = publish(doc, Path({str(tmp_path)!r}),
                      Config(targets=["json", "xml", "bits", "jats"], grobid_url=None))
        assert all(r.valid for r in out.values()), {{k: r.errors for k, r in out.items()}}
        import pdf2xml.extract.structure_docling, pdf2xml.export.assets, pdf2xml.triage
    """)
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=ROOT, timeout=300)
    assert res.returncode == 0, res.stderr[-3000:]


def test_api_server_runs_without_the_modules_the_docker_image_leaves_out() -> None:
    import build_docker

    blocked = ["pdf2xml." + m.removesuffix(".py").replace("/", ".")
               for m in build_docker.DOCKER_SKIP if m != "cli.py"]
    code = textwrap.dedent(f"""
        import importlib.abc, json, sys, tempfile, threading, urllib.request
        BLOCKED = {[*blocked, "typer", "saxonche", "docx"]!r}

        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name in BLOCKED:
                    raise ImportError("left out of the docker image: " + name)
                return None

        sys.meta_path.insert(0, Block())
        from pathlib import Path
        from pdf2xml.jobs import ConversionQueue
        from pdf2xml.preview import Viewer, make_server

        root = Path(tempfile.mkdtemp())
        jobs = ConversionQueue(root, engines=("docling",), targets=("json", "xml", "bits",
                               "jats"), qa=False, views=False)
        server = make_server(Viewer(root), "127.0.0.1", 0, jobs, allowed_hosts=("*",),
                             api_only=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{{port}}/api/health",
                                     headers={{"Host": "pdf2xml-api:8765"}})
        assert json.load(urllib.request.urlopen(req, timeout=30))["status"] == "ok"
        server.shutdown()
        jobs.close()
    """)
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=ROOT, timeout=300)
    assert res.returncode == 0, res.stderr[-3000:]


def test_the_package_command_line_parses_its_options() -> None:
    sys.path.insert(0, str(ROOT / "deploy"))
    try:
        import cli as deploy_cli
    finally:
        sys.path.pop(0)
    try:
        deploy_cli.main(["--to", "json,epub", "x.pdf"])
    except SystemExit as e:
        assert e.code == 2                                  # epub is not offered
    else:
        raise AssertionError("--to epub was accepted")
