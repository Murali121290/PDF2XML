"""Page geometry and truthful XML links for the interactive layout inspector."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any
from xml.parsers import expat

from lxml import etree

from pdf2xml.model import Box, Document, Figure, ListBlock, ListItem
from pdf2xml.qa.overlay import _COLORS


def xml_ranges(source: str) -> tuple[dict[str, dict[str, int]], list[dict[str, Any]]]:
    """Line spans are measured in the original XML, including multiline attributes."""
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    stack: list[tuple[str, dict[str, str], int, list[str]]] = []
    ids: dict[str, dict[str, int]] = {}
    ambiguous_ids: set[str] = set()
    texts: list[dict[str, Any]] = []

    def start(tag: str, attrs: dict[str, str]) -> None:
        stack.append((tag, attrs, parser.CurrentLineNumber, []))

    def chars(text: str) -> None:
        if stack:
            stack[-1][3].append(text)

    def end(tag: str) -> None:
        name, attrs, line, chunks = stack.pop()
        text = ''.join(chunks)
        span = {'start': line, 'end': parser.CurrentLineNumber}
        if attrs.get('id'):
            ident = attrs['id']
            if ident in ids:
                ids.pop(ident)
                ambiguous_ids.add(ident)
            elif ident not in ambiguous_ids:
                ids[ident] = span
        if (name in {'p', 'title', 'label', 'alt-text', 'code', 'tex-math', 'td', 'th'}
                and text.strip()):
            texts.append({'text': text, **span})
        if stack:
            stack[-1][3].append(text)

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = chars
    parser.Parse(source, True)
    return ids, texts


def _key(text: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def page_blocks(doc: Document, page: int, source: str, canonical: bool) -> dict[str, Any]:
    """Numbers describe the recovered canonical sequence, never a new geometry sort."""
    ids, texts = xml_ranges(source)
    by_text: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in texts:
        by_text[_key(entry['text'])].append(entry)
    blocks = []
    order = 0
    global_order = 0

    def walk(items: Any, parent: str | None = None) -> None:
        nonlocal order, global_order
        for b in items:
            container = isinstance(b, Box | ListBlock)
            decorative = isinstance(b, Figure) and b.decorative is True
            readable = not container and not decorative
            if readable:
                global_order += 1
            if b.page == page:
                if readable:
                    order += 1
                text = ''.join(r.text for r in getattr(b, 'runs', []))
                if isinstance(b, Figure):
                    text = b.alt or ''
                if b.type == 'table':
                    text = ' | '.join(''.join(r.text for r in c.runs)
                                      for row in b.rows for c in row)
                span = ids.get(b.id) if canonical else None
                link = 'exact' if span else None
                if not canonical and text:
                    candidates = by_text.get(_key(text), [])
                    if len(candidates) == 1:
                        span = {k: candidates[0][k] for k in ('start', 'end')}
                        link = 'unique-text'
                blocks.append({'id': b.id, 'type': b.type, 'bbox': list(b.bbox),
                               'order': order if readable else None,
                               'global_order': global_order if readable else None,
                               'parent_id': parent, 'container': container,
                               'decorative': decorative, 'text': text[:1600],
                               'style': b.style, 'source_ref': b.source_ref,
                               'region_id': b.region_id, 'xml': span, 'link': link})
            if isinstance(b, Box):
                walk(b.children, b.id)
            elif isinstance(b, ListBlock):
                walk(b.items, b.id)
            elif isinstance(b, ListItem):
                walk(b.children, b.id)

    for part in (doc.body, doc.footnotes, doc.references):
        walk(part)
    artifacts = [{'id': f'artifact-{page}-{i}', 'type': 'artifact', 'bbox': list(a.bbox),
                  'text': a.text[:1600], 'kind': a.kind, 'order': None, 'container': False,
                  'decorative': False, 'xml': None, 'link': None}
                 for i, a in enumerate(doc.artifacts) if a.page == page]
    palette = {kind: '#' + ''.join(f'{v:02x}' for v in color) for kind, color in _COLORS.items()}
    tree = etree.fromstring(source.encode('utf-8'), etree.XMLParser(
        load_dtd=False, no_network=True, resolve_entities=False, huge_tree=True))
    return {'blocks': blocks, 'artifacts': artifacts, 'palette': palette,
            'xml_kind': etree.QName(tree).localname, 'line_count': len(source.splitlines()),
            'reading_order_note': 'Per-page numbers follow the canonical block sequence after '
            'layout recovery. Containers, decorative graphics and artifacts are not numbered.',
            'mapping_note': 'Exact canonical block IDs.' if canonical else
            'Only unique text matches are linked. Ambiguous or transformed text stays unlinked.'}
