"""Crawl every board in the index's directory through the boards' public APIs.

Politeness: a small worker pool per platform, Lever spaced one second apart (its robots.txt
crawl delay), an honest User-Agent, and dead or failing boards backed off for days.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from jobfeed.index import JobIndex, pretty_company
from jobfeed.careers import CAREER_ATS
from jobfeed.smb import ANY_ATS
from jobfeed.sources import FETCHERS, BoardError

log = logging.getLogger("jobfeed.crawler")

WORKERS = {"greenhouse": 5, "ashby": 4, "lever": 3, "workday": 12, "icims": 12, "successfactors": 4, "taleo": 4,
           "oracle": 4, "bamboohr": 8, "paylocity": 4, "recruitee": 4, "personio": 4, "breezy": 4, "teamtailor": 4,
           "jazzhr": 4, "jobvite": 4, "rippling": 4, "adp": 4, "ukg": 4, "paycom": 4, "dayforce": 4, "jsonld": 12}  # Lever is still paced to one request a second


class _Spacer:
    """At most one call per `gap` seconds across threads."""

    def __init__(self, gap: float):
        self.gap, self.lock, self.last = gap, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            delay = self.last + self.gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


ALL_ATS = ("greenhouse", "ashby", "lever", "workday", "icims", "successfactors", "taleo", "oracle",
           "bamboohr", "paylocity", "recruitee", "personio", "breezy", "teamtailor",
           "jazzhr", "jobvite", "rippling", "adp", "ukg", "paycom", "dayforce", "jsonld")


# Platform groups crawled side by side as separate jobs in the data workflow (each on its own machine).
GROUPS = {
    "boards": ("greenhouse", "lever"),
    "startups": ("ashby", "bamboohr", "paylocity", "recruitee", "personio", "breezy", "teamtailor"),
    "workday": ("workday",),
    "enterprise": ("icims", "successfactors", "taleo", "oracle"),
    "longtail": ("jazzhr", "jobvite", "rippling", "adp", "ukg", "paycom", "dayforce", "jsonld"),
}


def crawl(index: JobIndex, fetchers=FETCHERS, ats: tuple[str, ...] = ALL_ATS,
          limit: int | None = None, workers: dict | None = None, stop: threading.Event | None = None,
          spacing: dict | None = None, boards: list | None = None, record: bool = True) -> dict:
    """Fetch every due board and load it into the index. Platforms run side by side."""
    workers = {**WORKERS, **(workers or {})}
    spacing = {"lever": 1.0, **(spacing or {})}
    totals = {"boards": 0, "ok": 0, "failed": 0, "dead": 0, "postings": 0, "new": 0}
    from collections import Counter
    reasons: Counter = Counter()   # why boards failed, per platform: the first thing to read when counts drop
    per_platform: dict = {}
    tl = threading.Lock()
    started = time.monotonic()

    def run_platform(platform: str):
        fetch = fetchers.get(platform)
        if not fetch:
            return
        todo = [b for b in boards if b["ats"] == platform] if boards is not None \
            else index.due_boards((platform,), limit)
        spacer = _Spacer(spacing.get(platform, 0.0))
        session = requests.Session()

        def one(row):
            if stop is not None and stop.is_set():
                return row, None, "stopped"
            spacer.wait()
            try:
                if platform in CAREER_ATS or platform in ANY_ATS:  # pass what we know: pages aren't re-read
                    return row, fetch(row["board"], row["company"] or "", session=session,
                                      existing=index.existing_for(platform, row["board"])), None
                return row, fetch(row["board"], row["company"] or "", session=session), None
            except TypeError:  # fetchers in tests may not take a session
                try:
                    return row, fetch(row["board"], row["company"] or ""), None
                except BoardError as exc:
                    return row, None, str(exc)
            except BoardError as exc:
                return row, None, str(exc)
            except Exception as exc:  # one bad board never stops the crawl
                return row, None, f"unexpected: {exc!r}"

        with ThreadPoolExecutor(max_workers=max(1, workers.get(platform, 2))) as pool:
            for fut in as_completed([pool.submit(one, r) for r in todo]):
                row, jobs, err = fut.result()
                if err == "stopped":
                    continue
                if err is not None:
                    dead = "404" in err
                    index.board_result(platform, row["board"], ok=False, dead=dead)
                    with tl:
                        totals["boards"] += 1
                        totals["dead" if dead else "failed"] += 1
                        if not dead:
                            import re as _re
                            reasons[f"{platform}: {_re.sub(r'[0-9a-f-]{8,}|https?://\S+', '…', err)[:70]}"] += 1
                        pp = per_platform.setdefault(platform, {"ok": 0, "failed": 0, "dead": 0, "postings": 0})
                        pp["dead" if dead else "failed"] += 1
                    continue
                company = row["company"] or (jobs[0].company if jobs and jobs[0].company != row["board"] else "") \
                    or pretty_company(row["board"])
                jobs = [j if j.company and j.company != row["board"] else j.__class__(**{**j.__dict__, "company": company})
                        for j in jobs]
                new = index.ingest_board(platform, row["board"], jobs)
                index.board_result(platform, row["board"], ok=True, count=len(jobs), company=company)
                with tl:
                    totals["boards"] += 1
                    totals["ok"] += 1
                    totals["postings"] += len(jobs)
                    totals["new"] += len(new)
                    pp = per_platform.setdefault(platform, {"ok": 0, "failed": 0, "dead": 0, "postings": 0})
                    pp["ok"] += 1
                    pp["postings"] += len(jobs)

    from jobfeed.careers import reset_page_budget
    reset_page_budget()
    threads = [threading.Thread(target=run_platform, args=(p,), name=f"crawl-{p}") for p in ats]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    totals["seconds"] = round(time.monotonic() - started)
    totals["platforms"] = per_platform
    totals["top_failures"] = reasons.most_common(12)
    if record:
        index.set_meta("last_crawl", {**totals, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    log.info("crawl done: %s", totals)
    return totals
