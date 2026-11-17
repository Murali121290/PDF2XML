"""Start this checkout's API even when another checkout shares the Python environment."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8087)
    parser.add_argument("--check", action="store_true",
                        help="Report versions/source without serving")
    args = parser.parse_args()

    from importlib.metadata import version

    import pdf2xml

    print(f"PDF2XML {pdf2xml.__version__} from {pdf2xml.__file__}; "
          f"Docling {version('docling')}", flush=True)
    if not args.check:
        import uvicorn

        uvicorn.run("api:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
