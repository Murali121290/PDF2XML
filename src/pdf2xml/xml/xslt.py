"""Run XSLT 3.0 stylesheets from ``resources/xslt`` with Saxon-HE (saxonche)."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from lxml import etree


def stylesheet(name: str) -> Path:
    return Path(str(resources.files("pdf2xml") / "resources" / "xslt" / name))


@lru_cache(maxsize=1)
def _processor() -> Any:
    from saxonche import PySaxonProcessor

    # One processor per process: saxonche does not support creating several.
    return PySaxonProcessor(license=False)


@lru_cache(maxsize=16)
def _compiled(path: str) -> Any:
    proc = _processor()
    xslt = proc.new_xslt30_processor()
    exe = xslt.compile_stylesheet(stylesheet_file=path)
    if exe is None:
        raise RuntimeError(f"XSLT compile failed: {path}: {xslt.error_message}")
    return exe


_file_sheets: dict[str, tuple[object, Any]] = {}


def transform_file(
    text: str, sheet: Path, params: dict[str, str] | None = None, stamp: object = None
) -> str:
    """Transform XML ``text`` with the stylesheet at ``sheet`` (any path, XSLT 1.0–3.0).

    The compiled stylesheet is reused until ``stamp`` changes, so a caller that passes the
    modification times of the sheet and the files it imports gets its edits picked up.
    """
    proc = _processor()
    key = str(sheet.resolve())
    cached = _file_sheets.get(key)
    if cached is None or cached[0] != stamp:
        xslt = proc.new_xslt30_processor()
        try:
            exe = xslt.compile_stylesheet(stylesheet_file=key)
        except Exception as e:  # saxonche raises PySaxonApiError
            raise RuntimeError(f"XSLT compile failed: {sheet.name}: {e}") from e
        if exe is None:
            raise RuntimeError(f"XSLT compile failed: {sheet.name}: {xslt.error_message}")
        _file_sheets[key] = cached = (stamp, exe)
    exe = cached[1]
    exe.clear_parameters()
    for k, v in (params or {}).items():
        exe.set_parameter(k, proc.make_string_value(v))
    try:
        node = proc.parse_xml(xml_text=text)
        out = exe.transform_to_string(xdm_node=node)
    except Exception as e:
        raise RuntimeError(f"XSLT {sheet.name} failed: {e}") from e
    if out is None:
        raise RuntimeError(f"XSLT {sheet.name} failed: {exe.error_message}")
    return str(out)


def transform(
    source: etree._Element | Path, sheet: str, params: dict[str, str] | None = None
) -> str:
    """Transform ``source`` (lxml element or XML file) with ``resources/xslt/<sheet>``."""
    proc = _processor()
    exe = _compiled(str(stylesheet(sheet)))
    exe.clear_parameters()
    for k, v in (params or {}).items():
        exe.set_parameter(k, proc.make_string_value(v))
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8")
    else:
        text = etree.tostring(source, encoding="unicode")
    node = proc.parse_xml(xml_text=text)
    out = exe.transform_to_string(xdm_node=node)
    if out is None:
        raise RuntimeError(f"XSLT {sheet} failed: {exe.error_message}")
    return str(out)
