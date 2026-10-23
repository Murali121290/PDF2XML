"""Bundled NLM DTDs (``resources/schemas/nlm``) and offline validation of BITS/JATS output."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path

from lxml import etree


@dataclass(frozen=True)
class Schema:
    key: str
    roots: tuple[str, ...]
    folder: str
    main: str
    public_id: str
    system_url: str
    version: str             # default @dtd-version

    @property
    def path(self) -> Path:
        return Path(str(resources.files("pdf2xml") / "resources" / "schemas" / "nlm"
                        / self.folder / self.main))

    def doctype(self, root: str) -> str:
        return f'<!DOCTYPE {root} PUBLIC "{self.public_id}" "{self.system_url}">'


SCHEMAS: dict[str, Schema] = {s.key: s for s in (
    Schema("bits", ("book-part-wrapper", "book"), "bits-2.2", "BITS-book2-2.dtd",
           "-//NLM//DTD BITS Book Interchange DTD v2.2 20250930//EN",
           "https://jats.nlm.nih.gov/extensions/bits/2.2/BITS-book2-2.dtd", "2.2"),
    Schema("jats-archiving", ("article",), "jats-archiving-1.4",
           "JATS-archivearticle1-4-mathml3.dtd",
           "-//NLM//DTD JATS (Z39.96) Journal Archiving and Interchange DTD with MathML3 v1.4 "
           "20241031//EN",
           "https://jats.nlm.nih.gov/archiving/1.4/JATS-archivearticle1-4-mathml3.dtd", "1.4"),
    Schema("jats-publishing", ("article",), "jats-publishing-1.4",
           "JATS-journalpublishing1-4-mathml3.dtd",
           "-//NLM//DTD JATS (Z39.96) Journal Publishing DTD with MathML3 v1.4 20241031//EN",
           "https://jats.nlm.nih.gov/publishing/1.4/JATS-journalpublishing1-4-mathml3.dtd", "1.4"),
    Schema("jats-authoring", ("article",), "jats-authoring-1.4",
           "JATS-articleauthoring1-4-mathml3.dtd",
           "-//NLM//DTD JATS (Z39.96) Article Authoring DTD with MathML3 v1.4 20241031//EN",
           "https://jats.nlm.nih.gov/articleauthoring/1.4/JATS-articleauthoring1-4-mathml3.dtd",
           "1.4"),
)}


@cache
def _dtd(key: str) -> etree.DTD:
    # Loading a full JATS/BITS DTD takes ~0.5 s; the modules sit next to the main file.
    return etree.DTD(str(SCHEMAS[key].path))


def validate(root: etree._Element, key: str, limit: int = 50) -> list[str]:
    """Validate against a bundled DTD; returns error messages (empty when valid)."""
    dtd = _dtd(key)
    if dtd.validate(root):
        return []
    return [f"line {e.line}: {e.message}" for e in list(dtd.error_log)[:limit]]


def detect(root: etree._Element, public_id: str | None = None) -> str:
    """Pick the schema for a document from its root element and DOCTYPE public identifier."""
    tag = etree.QName(root).localname
    if tag in ("book", "book-part-wrapper"):
        return "bits"
    pid = (public_id or "").lower()
    if "publishing" in pid:
        return "jats-publishing"
    if "authoring" in pid:
        return "jats-authoring"
    return "jats-archiving"


def validate_file(path: Path, key: str | None = None) -> tuple[str, list[str]]:
    """Validate an XML file on disk (offline: its DOCTYPE is not fetched)."""
    parser = etree.XMLParser(load_dtd=False, no_network=True, resolve_entities=False)
    tree = etree.parse(str(path), parser)
    key = key or detect(tree.getroot(), tree.docinfo.public_id)
    return key, validate(tree.getroot(), key)
