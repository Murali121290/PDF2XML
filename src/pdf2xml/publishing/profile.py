"""Export profiles (how to tag) and metadata (what the PDF can't tell us), loaded from YAML/JSON.

A profile captures a client's house rules; every field has a default that produces valid,
standard BITS 2.2 / JATS 1.4. Metadata comes from a sidecar file and overrides anything detected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- profile

class BitsOptions(_Cfg):
    # "auto": a whole book (printed contents page, or a copyright page with an ISBN) becomes
    # <book> with front matter, chapters and back matter; anything else one <book-part-wrapper>
    root: Literal["auto", "book-part-wrapper", "book"] = "auto"
    version: str = "2.2"                    # written to @dtd-version
    doctype: str = "official"               # "official" | "none" | full "<!DOCTYPE …>" text
    book_part_type: str = "chapter"


class JatsOptions(_Cfg):
    # Archiving is the default because it is the only tag set whose front matter is all optional;
    # Publishing needs journal-id + ISSN, Authoring needs contributors + an abstract.
    tag_set: Literal["archiving", "publishing", "authoring"] = "archiving"
    version: str = "1.4"
    doctype: str = "official"
    article_type: str = "research-article"


class SectionOptions(_Cfg):
    mode: Literal["auto", "style-map"] = "auto"
    map: dict[str, int] = {}                # catalogue style key → section depth (1 = top)
    caps_first: bool = True                 # ALL-CAPS heads outrank same-size mixed-case heads
    disp_level: str | None = None           # e.g. "level{n}" writes @disp-level
    sec_types: bool = True                  # Introduction → sec-type="intro", …
    split_labels: bool = True               # "1.1 Goals" → <label>1.1</label><title>Goals</title>


class IdOptions(_Cfg):
    """Patterns; fields: {n} running number, {chapter}, {level} (sections only), {folio} and
    {page} (page markers only: the printed page number, and the PDF page number), {kind}
    (front/back matter only: toc, pref, ack, gloss, idx, app, notes, bio …)."""

    part: str = "ch{chapter}"               # the one chapter of a book-part-wrapper
    chapter: str = "ch{n}"                  # chapters of a whole book
    book_part: str = "part{n}"              # parts of a whole book ("Part 2", holding chapters)
    matter: str = "{kind}{n}"               # front and back matter
    sec: str = "sec{n}"
    table: str = "tab{n}"
    fig: str = "fig{n}"
    ref: str = "ref{n}"
    fn: str = "fn{n}"
    formula: str = "eq{n}"
    box: str = "box{n}"
    page: str = "page_{folio}"


class QueryOptions(_Cfg):
    mode: Literal["drop", "comment", "pi", "keep"] = "drop"
    pattern: str = r"^\s*AQ\d*\s*(?:[:.]|$)"   # proof-stage author queries ("AQ:", "AQ16")


class Profile(_Cfg):
    name: str = "default"
    bits: BitsOptions = BitsOptions()
    jats: JatsOptions = JatsOptions()
    sections: SectionOptions = SectionOptions()
    ids: IdOptions = IdOptions()
    queries: QueryOptions = QueryOptions()
    labels: Literal["split", "keep"] = "split"          # "Table 1.2 Title" → label + title
    references: bool = True                             # detect the reference list
    reference_headings: list[str] = [
        "references", "reference", "bibliography", "works cited", "literature cited",
        "reference list", "cited literature", "literature",
    ]
    citation_links: Literal["none", "author-year", "numeric", "auto"] = "auto"
    # tag each reference's parts (authors, year, title, source, volume, pages, DOI …) inside
    # mixed-citation; references that cannot be read stay plain text
    granular_references: bool = True
    # where each printed page begins: <target target-type="page"/>, <?page-break N?>, or none
    page_markers: Literal["target", "pi", "none"] = "target"
    callout_links: bool = True                          # "Table 1.2" → <xref ref-type="table">
    ext_links: bool = True                              # URLs / DOIs → <ext-link>
    styling: Literal["semantic", "keep-visual"] = "semantic"
    front_matter: bool = True                           # detect Abstract / Keywords sections
    # detect the printed contents, front matter (title and copyright pages, preface …) and back
    # matter (appendices, glossary, index, endnotes, contributor notes …)
    matter: bool = True
    join_paragraphs: bool = True                        # rejoin paragraphs split by breaks
    drop_running_heads: bool = True                     # headings that repeat a page header
    min_figure_pt: float = 36.0                         # smaller "figures" are marks/icons
    list_labels: Literal["auto", "keep", "drop"] = "auto"


# ---------------------------------------------------------------- metadata

class Person(_Cfg):
    surname: str | None = None
    given: str | None = None
    name: str | None = None                 # a collaboration / organisation
    role: str = "author"
    orcid: str | None = None
    email: str | None = None
    aff: str | None = None


class DateParts(_Cfg):
    year: int | None = None
    month: int | None = None
    day: int | None = None


class BookMeta(_Cfg):
    title: str | None = None
    subtitle: str | None = None
    doi: str | None = None
    isbn: str | None = None                 # print
    eisbn: str | None = None                # electronic
    publisher: str | None = None
    publisher_loc: str | None = None
    edition: str | None = None
    pub_date: DateParts | None = None
    contributors: list[Person] = []         # editors / book authors


class JournalMeta(_Cfg):
    id: str | None = None                   # publisher journal id
    nlm_ta: str | None = None
    title: str | None = None
    issn: str | None = None                 # print
    eissn: str | None = None
    publisher: str | None = None


class PartMeta(_Cfg):
    """The chapter (BITS) or article (JATS) being converted."""

    label: str | None = None                # "11", "Chapter 11"
    number: str | None = None               # used in ID patterns as {chapter}
    title: str | None = None
    subtitle: str | None = None
    doi: str | None = None
    id: str | None = None                   # publisher id
    contributors: list[Person] = []
    pub_date: DateParts | None = None
    volume: str | None = None
    issue: str | None = None
    fpage: str | None = None
    lpage: str | None = None
    abstract: str | None = None
    keywords: list[str] = []
    copyright: str | None = None
    copyright_year: int | None = None
    license_url: str | None = None


class Metadata(_Cfg):
    book: BookMeta = BookMeta()
    journal: JournalMeta = JournalMeta()
    part: PartMeta = Field(default=PartMeta(),
                           validation_alias=AliasChoices("part", "chapter", "article"))
    lang: str | None = None


def _read(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


def load_profile(path: Path | None) -> Profile:
    return Profile.model_validate(_read(path)) if path else Profile()


def load_metadata(path: Path | None) -> Metadata:
    return Metadata.model_validate(_read(path)) if path else Metadata()
