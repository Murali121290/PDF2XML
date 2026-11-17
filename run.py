"""Run this application folder, independently of other installed checkouts."""
from __future__ import annotations
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ["PDF2XML_MODELS_DIR"] = str(ROOT / "models")
os.environ.setdefault("PDF2XML_DATA", str(ROOT / "out"))

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "--help"
    if command in {"api", "preview"}:
        helper = ROOT / "tools" / ("run_api.py" if command == "api" else "run_preview.py")
        sys.argv = [str(helper), *sys.argv[2:]]
        runpy.run_path(str(helper), run_name="__main__")
    else:
        from pdf2xml.cli import app
        app(args=sys.argv[1:])
