"""How the crawl learns from itself.

1. Every board failure gets a class (rate_limited, bot_check, robots, timeout, server_error, not_found, no_jobs_markup,
   parse, network, other). The class and message are stored on the board.
2. Backoff follows the class: blocked by a bot check or robots.txt waits 14 days (no hammering a site that said no),
   rate limits wait a day, timeouts and server errors back off exponentially.
3. Each platform's connection count is tuned from the last run: many rate limits or timeouts -> fewer connections next
   time; a clean run -> a few more (up to twice the default). Saved in the index, so it carries over day to day.
4. After each run, an audit compares it with the last one: platforms whose open roles dropped, error classes that grew,
   boards that newly failed or recovered, tuning changes, and plain-language next steps. The last 30 audits are kept.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

CLASSES = ("rate_limited", "bot_check", "robots", "timeout", "server_error", "not_found", "no_jobs_markup", "parse",
           "network", "other")

BACKOFF_DAYS = {"bot_check": 14, "robots": 14, "rate_limited": 1, "not_found": 14}


def classify(err: str) -> str:
    e = (err or "").lower()
    if "429" in e or "too many" in e or "rate" in e and "limit" in e:
        return "rate_limited"
    if "bot check" in e or "captcha" in e or "cloudflare" in e or "access denied" in e or "http 403" in e:
        return "bot_check"
    if "robots" in e:
        return "robots"
    if "timeout" in e or "timed out" in e or "readtimeout" in e:
        return "timeout"
    if re.search(r"http 5\d\d", e):
        return "server_error"
    if "404" in e or "not found" in e or "http 401" in e or "was not json" in e:
        return "not_found"  # 401 / an HTML page instead of the feed: that company switched its job page off
    if "no job listings" in e or "markup" in e or "job list not found" in e:
        return "no_jobs_markup"
    if "json" in e or "unexpected" in e or "readable" in e or "parse" in e:
        return "parse"
    if "connection" in e or "request failed" in e or "ssl" in e or "name resolution" in e:
        return "network"
    return "other"


def tune(defaults: dict[str, int], previous: dict[str, int], platforms: dict, minutes_left: bool = True) -> tuple[dict, list]:
    """New per-platform connection counts from one run's results. Returns (tuning, changes)."""
    tuned, changes = dict(previous), []
    for p, r in (platforms or {}).items():
        base = defaults.get(p, 3)
        cur = previous.get(p, base)
        tried = (r.get("ok", 0) + r.get("failed", 0)) or 0
        if tried < 20:
            continue
        cls = r.get("classes") or {}
        rl, to = cls.get("rate_limited", 0) / tried, cls.get("timeout", 0) / tried
        fail = r.get("failed", 0) / tried
        new = cur
        if rl > 0.05:
            new = max(1, int(cur * 0.6))
        elif to > 0.15:
            new = max(1, int(cur * 0.8))
        elif fail < 0.03 and minutes_left:
            new = min(base * 2, cur + max(1, cur // 4))
        if new != cur and p != "lever":  # Lever is paced by its own crawl delay
            tuned[p] = new
            why = (f"{rl:.0%} rate-limited" if rl > 0.05 else f"{to:.0%} timeouts" if to > 0.15 else
                   f"only {fail:.0%} failed")
            changes.append(f"{p}: {cur} → {new} connections ({why})")
    return tuned, changes


def audit(stats: dict, crawl: dict, previous: dict | None, tuning_changes: list[str], status_counts: dict) -> dict:
    """Compare this run with the last audit and say what to do about it."""
    prev = previous or {}
    by_now = {k: (v.get("open", 0) if isinstance(v, dict) else v) for k, v in (stats.get("by_source") or {}).items()}
    by_prev = prev.get("by_source") or {}
    drops = [f"{k}: {by_prev[k]:,} → {by_now.get(k, 0):,} open roles" for k in by_prev
             if by_prev[k] >= 200 and by_now.get(k, 0) < by_prev[k] * 0.7]
    gains = [f"{k}: {by_prev.get(k, 0):,} → {v:,}" for k, v in by_now.items() if v > by_prev.get(k, 0) * 1.3 + 100]
    cls_now: dict[str, int] = {}
    for p, r in (crawl.get("platforms") or {}).items():
        for c, n in (r.get("classes") or {}).items():
            cls_now[c] = cls_now.get(c, 0) + n
    cls_prev = prev.get("error_classes") or {}
    growing = [f"{c}: {cls_prev.get(c, 0):,} → {n:,}" for c, n in cls_now.items()
               if n >= 50 and n > cls_prev.get(c, 0) * 1.5]
    advice = []
    if cls_now.get("rate_limited", 0) > 100:
        advice.append("Rate limits are high: connection counts were lowered automatically; if they stay high, move "
                      "that platform to its own parallel group.")
    if cls_now.get("bot_check", 0) > 100:
        advice.append("Many sites show bot checks to the cloud: those are paused for 14 days. The laptop fallback "
                      "(home_run.sh) can read them from a home connection.")
    if cls_now.get("no_jobs_markup", 0) > 200:
        advice.append("Many long-tail sites have no job markup: they're skipped on purpose. Consider a site-specific "
                      "reader for the biggest ones.")
    if cls_now.get("parse", 0) > 50:
        advice.append("Parse errors grew: a platform may have changed its format. Check the failing examples.")
    if drops:
        advice.append("Some sources dropped sharply: check their top failures below before the next run.")
    unknown = status_counts.get("unknown", 0)
    if unknown > 1000:
        advice.append(f"{unknown:,} boards haven't been tried yet: they'll be reached as page budgets allow.")
    return {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "open": stats.get("open"),
            "companies": stats.get("companies"), "by_source": by_now, "error_classes": cls_now,
            "drops": drops, "gains": gains, "growing_errors": growing, "tuning": tuning_changes,
            "top_failures": crawl.get("top_failures") or [], "boards": status_counts, "advice": advice}


def to_markdown(a: dict) -> str:
    lines = [f"### Crawl audit: {a.get('open') or 0:,} open roles from {a.get('companies') or 0:,} companies"]
    for title, key in (("Dropped", "drops"), ("Grew", "gains"), ("Errors growing", "growing_errors"),
                       ("Tuning for next run", "tuning"), ("Next steps", "advice")):
        if a.get(key):
            lines.append(f"**{title}**")
            lines += [f"- {x}" for x in a[key]]
    if a.get("error_classes"):
        lines.append("**Failures by kind:** " + ", ".join(f"{k} {v:,}" for k, v in
                                                          sorted(a["error_classes"].items(), key=lambda x: -x[1])))
    if a.get("top_failures"):
        lines.append("**Top failure messages**")
        lines += [f"- {m} ×{n}" for m, n in a["top_failures"][:10]]
    return "\n".join(lines)
