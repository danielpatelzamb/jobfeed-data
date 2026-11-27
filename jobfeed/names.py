"""Readable company names for boards that only give us a web address ("pipersandler" -> "Piper Sandler").

Words and their frequencies come from the index itself: every company name that already has spaces, and every job
title. A slug is split into the most likely run of known words (Viterbi over word frequencies), and only accepted
when the split is confident; otherwise it's left alone (title-cased). Runs once per data job, on the whole index."""
from __future__ import annotations

import math
import re
from collections import Counter

# boards we already show nicely, or whose names come from the employer
_SLUG = re.compile(r"^[a-z0-9]{2,40}$")
_KEEP_UPPER = re.compile(r"^[a-z]{2,3}$")


def known_names(index) -> dict[str, str]:
    """'goldmansachs' -> 'Goldman Sachs' for every proper (spaced) company name already in the index."""
    out: dict[str, str] = {}
    for row in index._q("SELECT DISTINCT company FROM postings WHERE company LIKE '% %'"):
        name = (row["company"] or "").strip()
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if len(key) >= 5:
            out.setdefault(key, name)
    return out


def vocab(index, limit: int = 400_000) -> Counter:
    """Words from proper company names only (titles split brands into job words: 'Gold Man Sachs')."""
    c: Counter = Counter()
    for row in index._q("""SELECT company FROM postings WHERE open = 1 AND company LIKE '% %' LIMIT ?""", (limit,)):
        c.update(w for w in re.findall(r"[a-z]+", (row["company"] or "").lower()) if len(w) > 1)
    for w in ("group", "holdings", "capital", "partners", "financial", "global", "technical", "technologies",
              "systems", "health", "studios", "society", "planet", "the", "and", "labs", "solutions", "services",
              "consulting", "energy", "bank", "media", "foods", "brands", "logistics", "network", "insurance"):
        c[w] += 50
    return c


def split(slug: str, freq: Counter) -> list[str] | None:
    """Most likely words for a lowercase slug, or None if any piece is unknown/too short to trust."""
    s = slug.lower()
    total = sum(freq.values()) or 1
    n = len(s)
    best = [(0.0, -1)] + [(-math.inf, -1)] * n
    for i in range(1, n + 1):
        for j in range(max(0, i - 20), i):
            w = s[j:i]
            if w.isdigit():
                cost = -2.0
            elif freq.get(w, 0) and len(w) >= 2:
                cost = math.log(freq[w] / total)
            else:
                continue
            if best[j][0] + cost > best[i][0]:
                best[i] = (best[j][0] + cost, j)
    if best[n][0] == -math.inf:
        return None
    out, i = [], n
    while i > 0:
        j = best[i][1]
        out.append(s[j:i])
        i = j
    words = out[::-1]
    small = ("of", "the", "and")
    if len(words) < 2 or any(len(w) < 4 and not w.isdigit() and w not in small for w in words):
        return None
    if any(freq.get(w, 0) < 10 for w in words if not w.isdigit() and w not in small):
        return None  # only common business words ("group", "capital", "studios"), never rare fragments
    # glue digits onto the word before ("peak", "6" -> "peak6")
    merged: list[str] = []
    for w in words:
        if w.isdigit() and merged:
            merged[-1] += w
        else:
            merged.append(w)
    return merged


def pretty(slug: str, freq: Counter, known: dict[str, str] | None = None) -> str:
    if not _SLUG.match(slug or ""):
        return slug
    if known and slug in known:
        return known[slug]
    if _KEEP_UPPER.match(slug):
        return slug.upper()  # "mcw" -> "MCW"
    if len(slug) < 6:
        return slug[:1].upper() + slug[1:]
    words = split(slug, freq)
    if not words:
        return slug[:1].upper() + slug[1:]
    return " ".join(w if w.isdigit() else w[:1].upper() + w[1:] for w in words)


def fix_index(index) -> int:
    """Rename slug-only companies across the index. Returns how many distinct names changed."""
    freq, known = vocab(index), known_names(index)
    rows = index._q("""SELECT DISTINCT company FROM postings WHERE open = 1 AND company NOT LIKE '% %'""")
    changed = 0
    with index.lock:
        for r in rows:
            old = r["company"] or ""
            new = pretty(old.lower(), freq, known) if _SLUG.match(old.lower()) and (old == old.lower() or old[1:] == old[1:].lower()) else old
            if new and new != old:
                index.db.execute("UPDATE postings SET company = ? WHERE company = ?", (new, old))
                changed += 1
        index.db.commit()
    return changed
