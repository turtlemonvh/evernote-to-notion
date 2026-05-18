"""Shared ENEX parsing helpers used across phases.

Streams notes one at a time with lxml.iterparse so memory stays bounded even
for the multi-hundred-MB notebook exports.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from lxml import etree

# Evernote internal links look like:
#   evernote:///view/<userid>/<shardid>/<note-guid>/<note-guid>/
# The two trailing GUIDs are typically the same. We capture the first one.
INTERNAL_LINK_RE = re.compile(
    r'evernote:///view/\d+/[^/]+/([a-f0-9-]{36})/', re.IGNORECASE
)

_HTML_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def iter_notes(enex_path: str | Path) -> Iterator[etree._Element]:
    """Yield each <note> element from an ENEX file, freeing memory between notes."""
    # huge_tree=True lifts lxml's default text-length cap; required for ENEX
    # files containing very large base64-encoded attachments.
    context = etree.iterparse(
        str(enex_path), events=('end',), tag='note', huge_tree=True
    )
    for _, elem in context:
        yield elem
        elem.clear()
        # Drop already-processed siblings so the parser doesn't accumulate them.
        while elem.getprevious() is not None:
            del elem.getparent()[0]
    del context


def child_text(note: etree._Element, tag: str) -> str:
    el = note.find(tag)
    return el.text if el is not None and el.text else ''


def extract_tags(note: etree._Element) -> list[str]:
    return [el.text for el in note.findall('tag') if el.text]


def extract_content(note: etree._Element) -> str:
    """Inner CDATA string of <content>, or empty string."""
    el = note.find('content')
    return el.text if el is not None and el.text else ''


def word_count(content_html: str) -> int:
    if not content_html:
        return 0
    text = _HTML_TAG_RE.sub(' ', content_html)
    text = _WS_RE.sub(' ', text).strip()
    return len(text.split()) if text else 0


def find_internal_links(content_html: str) -> list[str]:
    """Target GUIDs referenced by evernote:/// internal links."""
    if not content_html:
        return []
    return INTERNAL_LINK_RE.findall(content_html)


def resource_info(note: etree._Element) -> list[tuple[str, int, str]]:
    """Return [(mime, decoded_bytes_estimate, filename), ...] per <resource>.

    Size is estimated from base64 string length (avoids decoding the binary into
    memory). Within ~1% of the true decoded size — good enough for flagging
    the Notion 5 MB threshold.
    """
    out: list[tuple[str, int, str]] = []
    for res in note.findall('resource'):
        mime_el = res.find('mime')
        mime = mime_el.text if mime_el is not None and mime_el.text else ''

        data_el = res.find('data')
        if data_el is not None and data_el.text:
            b64 = _WS_RE.sub('', data_el.text)
            decoded_size = len(b64) * 3 // 4
        else:
            decoded_size = 0

        fname = ''
        attrs = res.find('resource-attributes')
        if attrs is not None:
            fn_el = attrs.find('file-name')
            if fn_el is not None and fn_el.text:
                fname = fn_el.text

        out.append((mime, decoded_size, fname))
    return out
