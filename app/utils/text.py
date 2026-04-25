"""Text normalization & hashing helpers used across the news pipeline."""
from __future__ import annotations

import hashlib
import re
import unicodedata


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s-]", flags=re.UNICODE)


def normalize(text: str) -> str:
    """Lower-case, strip accents, collapse whitespace and drop punctuation."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    cleaned = _PUNCT.sub(" ", ascii_only)
    return _WS.sub(" ", cleaned).strip().lower()


def stable_hash(text: str) -> str:
    """Deterministic hash for dedup — 40-char sha1 of the normalized string."""
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()


def contains_any(text: str, needles: list[str]) -> bool:
    haystack = normalize(text)
    return any(n.lower() in haystack for n in needles if n)


def topic_slug(text: str, max_len: int = 48) -> str:
    """Short deterministic slug used for 'similar-market' dedup."""
    norm = normalize(text)
    tokens = [t for t in norm.split() if len(t) > 3]
    slug = "-".join(tokens[:6])
    return slug[:max_len]
