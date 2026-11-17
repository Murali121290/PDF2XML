"""Tenant-scoped, content-addressed raw Docling cache with atomic, checked entries."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def identity(pdf_hash: str, pages: list[int] | None, scope: str, models: Path, signature: str) -> str:
    from pdf2xml.extract.structure_docling import REQUIRED_FILES, REQUIRED_MODELS

    # Deployments verify weights against the pinned manifest. File metadata also invalidates
    # the cache after local replacement without hashing all weights for every request.
    files = []
    for kind, folder in REQUIRED_MODELS.items():
        for name in REQUIRED_FILES[kind]:
            path = models / folder / name
            stat = path.stat()
            files.append((folder, name, stat.st_size, stat.st_mtime_ns))
    manifest = models / "manifest.json"
    value = [pdf_hash, sorted(set(pages)) if pages else None, scope, signature, files,
             manifest.read_text(encoding="utf-8") if manifest.exists() else ""]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load(folder: Path, key: str) -> dict[str, Any] | None:
    path = folder / f"{key}.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        payload = entry["payload"]
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if entry["key"] != key or entry["sha256"] != digest:
            raise ValueError("cache checksum mismatch")
        os.utime(path, None)
        return payload
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("ignoring_invalid_cache_entry")
        return None


def save(folder: Path, key: str, payload: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    entry = {"key": key, "sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
             "payload": payload}
    temporary = folder / f"{key}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        temporary.replace(folder / f"{key}.json")
    finally:
        temporary.unlink(missing_ok=True)
