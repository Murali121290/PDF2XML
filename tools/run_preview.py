"""Start this checkout's PDF/XML layout inspector and upload workflow."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='Output root containing conversion folders')
    parser.add_argument('--pdf-root', action='append', type=Path, default=[],
                        help='Folder containing original source PDFs; repeatable')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not args.root.is_dir() or any(not p.is_dir() for p in args.pdf_root):
        parser.error('Output root and PDF source folders must exist')
    from pdf2xml.preview import serve

    print(f'Layout QA: http://{args.host}:{args.port}/inspect', flush=True)
    serve(args.root, host=args.host, port=args.port, source_roots=args.pdf_root,
          open_browser=not args.no_browser, layout=True)


if __name__ == '__main__':
    main()
