"""One place that decides order: when a role was posted, how well it fits (AI), how closely the title
matches, and whether the experience it asks for matches yours."""
from __future__ import annotations

from datetime import datetime, timezone

SORTS = {"best": "Best match", "newest": "Newest posted", "fit": "Best fit (AI)"}

# Weights for "best match". Freshness fades to zero over FRESH_DAYS.
W_FIT, W_REL, W_FRESH, W_EXP = 0.40, 0.25, 0.20, 0.15
FRESH_DAYS = 30.0


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


def relevance_from_reasons(reasons: str) -> float:
    r = (reasons or "").lower()
    if r.startswith("title:") or "; title:" in r:
        return 1.0
    if "related title" in r:
        return 0.75
    return 0.5


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
    return expr, [h, h, h + (stretch or 0)]


def order_sql(sort: str, have, stretch) -> tuple[str, list]:
    if sort == "newest":
        return "COALESCE(NULLIF(posted, ''), first_seen) DESC, fit DESC", []
    if sort == "fit":
        expr, args = score_sql(have, stretch)
        return f"COALESCE(fit, 3) DESC, {expr} DESC", args
    expr, args = score_sql(have, stretch)
    return f"{expr} DESC", args
