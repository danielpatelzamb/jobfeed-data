"""One place that decides order: when a role was posted, how well it fits (AI), how closely the title
matches, and whether the experience it asks for matches yours."""
from __future__ import annotations

import re

from datetime import datetime, timezone

SORTS = {"best": "Best match", "newest": "Newest posted", "fit": "Best fit (AI)"}

# Weights for "best match". Freshness fades to zero over FRESH_DAYS.
W_FIT, W_REL, W_FRESH, W_EXP = 0.40, 0.25, 0.20, 0.15
FRESH_DAYS = 30.0
W_LEARN = 0.20


def to_utc(stamp) -> str:
    """Board timestamps ('2026-09-09T10:50:29-04:00', '...753+00:00', '...Z', '2026-11-20') -> '...Z' in UTC."""
    if not stamp:
        return ""
    s = str(stamp).strip()
    try:
        if len(s) == 10:
            dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_FILLER = {"i", "ii", "iii", "junior", "jr", "senior", "sr", "entry", "level", "new", "grad", "graduate", "remote",
           "hybrid", "us", "usa", "full", "time", "the", "and", "of", "for", "a", "in", "at", "with", "to", "or", "f",
           "m", "w", "d", "x", "intern", "internship", "summer", "2026", "2027", "2028"}


def relevance_from_reasons(reasons: str, title: str = "") -> float:
    """How well a matched title fits the target phrase: exact titles first, long padded titles lower."""
    r = (reasons or "").lower()
    if r.startswith("title:") or "; title:" in r:
        base, said = 1.0, r.split("title:", 1)[1].split(";")[0]
    elif "related title" in r:
        base, said = 0.75, r.split("related title:", 1)[1].split(";")[0]
    else:
        return 0.5
    if not title:
        return base
    target = set(re.findall(r"[a-z0-9]+", said.split(",")[0]))
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in _FILLER]
    extra = len([w for w in words if w not in target])
    return round(base * (1.0 - min(0.35, 0.06 * extra)), 3)


def exp_fit(years_min, have, stretch) -> float:
    if years_min is None or have is None:
        return 0.8
    if years_min <= have:
        return 1.0
    if years_min <= have + stretch:
        return 0.6
    return 0.0


def score_sql(have, stretch) -> tuple[str, list]:
    """SQL expression (and its args) for the best-match score of a row in a user's jobs table."""
    h = -1 if have is None else have
    expr = (f"({W_FIT} * COALESCE((fit - 1) / 4.0, 0.55)"
            f" + {W_REL} * COALESCE(relevance, 0.5)"
            f" + {W_FRESH} * MAX(0.0, 1.0 - (julianday('now') - julianday(COALESCE(NULLIF(posted, ''), first_seen)))"
            f" / {FRESH_DAYS})"
            f" + {W_EXP} * (CASE WHEN years_min IS NULL OR ? < 0 THEN 0.8 WHEN years_min <= ? THEN 1.0"
            f" WHEN years_min <= ? THEN 0.6 ELSE 0.0 END))"
            f" * (CASE WHEN ghost = 1 THEN 0.85 ELSE 1.0 END)")
    # what people like you saved, applied to and got interviews from (learn.py), once a model is trusted
    expr = f"(CASE WHEN learned IS NULL THEN {expr} ELSE {1 - W_LEARN} * {expr} + {W_LEARN} * learned END)"
    return expr, [h, h, h + (stretch or 0)] * 2


def order_sql(sort: str, have, stretch) -> tuple[str, list]:
    if sort == "newest":
        return "COALESCE(NULLIF(posted, ''), first_seen) DESC, fit DESC", []
    if sort == "fit":
        expr, args = score_sql(have, stretch)
        return f"COALESCE(fit, 3) DESC, {expr} DESC", args
    expr, args = score_sql(have, stretch)
    return f"{expr} DESC", args
