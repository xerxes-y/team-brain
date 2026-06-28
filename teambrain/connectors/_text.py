"""Text helpers shared by the connectors.

Keeps chunking/slugging in one place so Jira and PR ingestion behave like
Confluence (heading-bounded chunks, fixed-size sub-splits) without each
connector re-deriving it. Confluence keeps its own storage-format (XHTML)
chunker since that input is HTML, not markdown.
"""
from __future__ import annotations

import re


def slug(value) -> str:
    """Make a tag-safe token: no spaces (memento tags are space/comma-delimited,
    and ``store._tags_of`` splits on both)."""
    return re.sub(r"[\s,]+", "-", str(value or "").strip())


def chunk_fixed(text: str, max_chars: int = 1500):
    """Yield non-empty fixed-size pieces of ``text`` so one long blob never
    becomes one giant memory."""
    text = (text or "").strip()
    for i in range(0, len(text), max_chars):
        piece = text[i:i + max_chars].strip()
        if piece:
            yield piece


def chunk_markdown(text: str, max_chars: int = 1500):
    """Split markdown/plain text into heading-bounded chunks, sub-splitting long
    sections. Yields ``(heading, body)`` to mirror the Confluence chunker."""
    text = text or ""
    parts = re.split(r"(?m)^(#{1,3}\s+.*$)", text)
    if len(parts) == 1:
        for piece in chunk_fixed(text, max_chars):
            yield ("", piece)
        return
    heading = ""
    for seg in parts:
        if re.match(r"^#{1,3}\s+", seg or ""):
            heading = seg.strip("# ").strip()
        elif seg and seg.strip():
            for piece in chunk_fixed(seg, max_chars):
                yield (heading, piece)
