"""Measure real conversion and reference checks; report failures rather than hiding them."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path, help="JSON manifest with cases and optional expected_text/table checks")
    parser.add_argument("--out", type=Path, default=Path("out/benchmark"))
    parser.add_argument("--engine", choices=("docling", "heuristic"), default="docling")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--cache", action="store_true")
    parser.add_argument("--table-mode", choices=("accurate", "fast"), default="accurate")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    from importlib.metadata import version
    import psutil
    from pdf2xml.config import Config
    from pdf2xml.model import Table, iter_blocks
    from pdf2xml.pipeline import convert
    from pdf2xml.qa.coverage import document_text

    manifest = json.loads(args.corpus.read_text(encoding="utf-8"))
    records = []
    source_hash = hashlib.sha256()
    for path in sorted((ROOT / "src" / "pdf2xml").rglob("*.py")):
        source_hash.update(str(path.relative_to(ROOT)).replace("\\", "/").encode())
        source_hash.update(path.read_bytes())
    args.out.mkdir(parents=True, exist_ok=True)
    for case_index, case in enumerate(manifest["cases"]):
        source = (args.corpus.parent / case["pdf"]).resolve()
        for repeat in range(args.repeat):
            peak = [0]
            stop = threading.Event()

            def sample():
                process = psutil.Process()
                while not stop.is_set():
                    peak[0] = max(peak[0], process.memory_info().rss)
                    stop.wait(0.02)

            monitor = threading.Thread(target=sample, daemon=True)
            monitor.start()
            started = time.perf_counter()
            record = {"pdf": case["pdf"], "repeat": repeat, "first_conversion_in_process": not records}
            try:
                result = convert(source, args.out / f"case-{case_index}-run-{repeat}", Config(
                    engine=args.engine, targets=["json", "xml", "content"], overlays=False,
                    table_mode=args.table_mode,
                    debug_artifacts=False, reuse_cache=args.cache,
                    cache_dir=args.out / "_cache" if args.cache else None),
                    progress=lambda stage: print(json.dumps({"pdf": case["pdf"], "repeat": repeat,
                                                             "stage": stage}), flush=True))
                text = " ".join(document_text(result.document).split())
                expected = case.get("expected_text", [])
                positions = [text.find(" ".join(value.split())) for value in expected]
                tables = [b for b in iter_blocks(result.document.body) if isinstance(b, Table)]
                cells = [" ".join("".join(r.text for r in c.runs).split())
                         for table in tables for row in table.rows for c in row]
                checks = {"coverage": result.coverage.ok, "xml_valid": not result.xml_errors,
                          "content_valid": not any(i["code"] == "content_schema" for i in result.quality["issues"]),
                          "structural_quality": result.quality.get("errors", 0) == 0,
                          "reference_text_present": all(p >= 0 for p in positions) if expected else None,
                          "reference_order": positions == sorted(positions) and all(p >= 0 for p in positions) if expected else None,
                          "table_count": len(tables) == case["expected_tables"] if "expected_tables" in case else None,
                          "table_cells": all(value in cells for value in case["table_cells"]) if case.get("table_cells") else None}
                record.update(pages=len(result.document.pages), timings_s=result.timings, checks=checks,
                              quality=result.quality, passed=all(value for value in checks.values() if value is not None))
            except Exception as error:
                record.update(passed=False, error=f"{type(error).__name__}: {error}")
            finally:
                stop.set()
                monitor.join()
            record.update(elapsed_s=time.perf_counter() - started, peak_rss_bytes=peak[0])
            if record.get("pages"):
                record["pages_per_second"] = record["pages"] / record["elapsed_s"]
            records.append(record)
            print(json.dumps({k: record[k] for k in ("pdf", "repeat", "elapsed_s", "passed")}), flush=True)
            (args.out / "benchmark.json").write_text(json.dumps({
                "python": platform.python_version(), "platform": platform.platform(),
                "source_sha256": source_hash.hexdigest(), "cpu_count": psutil.cpu_count(),
                "docling": version("docling"), "engine": args.engine,
                "synthetic": manifest.get("synthetic", False), "cache_enabled": args.cache,
                "table_mode": args.table_mode,
                "records": records}, indent=2), encoding="utf-8")
    return 0 if records and all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
