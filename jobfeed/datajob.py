"""The daily data job: crawl every company board, read the extra feeds, refresh sponsorship data, compute
signals, and publish one compressed index file.

Runs on a schedule in GitHub Actions (free for a public repo; runners have open internet), or on any server:

    python -m jobfeed.datajob crawl      # every due Greenhouse / Lever / Ashby board
    python -m jobfeed.datajob streams    # Workable feed, HN Who is hiring, USAJOBS (if keyed)
    python -m jobfeed.datajob sponsors   # DOL H-1B file, imported only when a new quarter appears
    python -m jobfeed.datajob discover   # new boards from Common Crawl (weekly is plenty)
    python -m jobfeed.datajob finish --out index.db.gz   # momentum, prune, health summary, export

Only public postings and public datasets live in the index; no user data ever goes near it.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from jobfeed.index import JobIndex, now_iso


def _index(path: str | None) -> JobIndex:
    return JobIndex(path or os.path.join(os.environ.get("JOBFEED_DATA", "data"), "index.db"))


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="jobfeed.datajob")
    p.add_argument("--db", help="index file (default data/index.db)")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crawl")
    c.add_argument("--limit", type=int, default=None)
    c.add_argument("--ats", default="", help="comma-separated platforms (default: all of them)")
    c.add_argument("--minutes", type=float, default=0, help="stop starting new boards after this long")
    c.add_argument("--group", default="", help="one platform group from crawler.GROUPS (for parallel jobs)")
    c.add_argument("--workers", type=int, default=0, help="connections per platform (default: built-in)")
    sh = sub.add_parser("shard")
    sh.add_argument("--group", required=True)
    sh.add_argument("--out", required=True)
    mg = sub.add_parser("merge")
    mg.add_argument("shards", nargs="*")
    sub.add_parser("streams")
    s = sub.add_parser("sponsors")
    s.add_argument("--force", action="store_true")
    dsc = sub.add_parser("discover")
    dsc.add_argument("--wdc", default=os.environ.get("WDC_JOBPOSTING_URLS", ""),
                     help="comma-separated Web Data Commons JobPosting N-Quads file URLs")
    au = sub.add_parser("audit")
    au.add_argument("--out", default="audit.json")
    au.add_argument("--md", default="audit.md")
    f = sub.add_parser("finish")
    f.add_argument("--out", default="index.db.gz")
    f.add_argument("--summary", default="summary.json")
    args = p.parse_args(argv)
    idx = _index(args.db)

    if args.cmd == "crawl":
        import threading

        from jobfeed.crawler import crawl
        added = idx.load_directory()
        stop = threading.Event()
        if args.minutes:
            threading.Timer(args.minutes * 60, stop.set).start()
        from jobfeed.crawler import ALL_ATS, GROUPS
        ats = tuple(a.strip() for a in args.ats.split(",") if a.strip()) or GROUPS.get(args.group) or ALL_ATS
        out = crawl(idx, ats=ats, limit=args.limit, stop=stop,
                    workers={a: args.workers for a in ats} if args.workers else None)
        out["directory_added"] = added
        print(json.dumps(out))
        os._exit(0)  # don't wait on a pending stop timer
    if args.cmd == "shard":
        from jobfeed.crawler import GROUPS
        print(json.dumps(idx.export_shard(args.out, GROUPS[args.group])))
        return 0
    if args.cmd == "merge":
        totals = {"boards": 0, "ok": 0, "failed": 0, "dead": 0, "postings": 0, "new": 0, "seconds": 0,
                  "platforms": {}, "top_failures": [], "groups": {}}
        for path in args.shards:
            try:
                r = idx.merge_shard(path)
            except Exception as exc:  # a missing or broken shard keeps yesterday's data for those platforms
                print(f"shard {path} skipped: {exc}")
                continue
            c = r.get("crawl") or {}
            for k in ("boards", "ok", "failed", "dead", "postings", "new"):
                totals[k] += c.get(k, 0)
            totals["seconds"] = max(totals["seconds"], c.get("seconds", 0))
            totals["platforms"].update(c.get("platforms") or {})
            totals["top_failures"] += c.get("top_failures") or []
            totals.setdefault("tuning", []).extend(c.get("tuning") or [])
            totals["groups"][",".join(r["sources"])] = r["open"]
        totals["top_failures"] = sorted(totals["top_failures"], key=lambda x: -x[1])[:15]
        totals["at"] = now_iso()
        idx.set_meta("last_crawl", totals)
        print(json.dumps(totals, default=str))
        return 0
    if args.cmd == "streams":
        from jobfeed import streams
        print(json.dumps(streams.run_streams(idx), default=str))
        return 0
    if args.cmd == "sponsors":
        from jobfeed import sponsors
        print(json.dumps(sponsors.refresh(idx, force=args.force), default=str))
        return 0
    if args.cmd == "discover":
        from jobfeed import streams
        try:
            found = streams.discover_common_crawl()
        except Exception as exc:
            print(f"Common Crawl skipped: {exc}")
            found = set()
        for ats, b in found:
            idx.add_board(ats, b, origin="discovered")
        sites = {}
        for u in [x.strip() for x in args.wdc.split(",") if x.strip()]:
            try:
                sites.update(streams.discover_wdc(u))
            except Exception as exc:
                print(f"WDC file skipped: {exc}")
        for host in sites:
            idx.add_board("jsonld", f"https://{host}", origin="discovered")
        idx.set_meta("last_discover", {"at": now_iso()[:10], "found": len(found), "sites": len(sites)})
        print(json.dumps({"found": len(found), "sites_with_job_markup": len(sites)}))
        return 0
    if args.cmd == "audit":
        from jobfeed import crawlaudit
        stats = idx.stats()
        crawl = idx.get_meta("last_crawl") or {}
        history = idx.get_meta("audit_history") or []
        a = crawlaudit.audit(stats, crawl, history[-1] if history else None, crawl.get("tuning") or [],
                             stats.get("boards") or {})
        a["examples"] = {c: [dict(r) for r in idx._q(
            "SELECT ats, board, last_error FROM boards WHERE error_class = ? AND last_tried >= ? LIMIT 5",
            (c, now_iso()[:10]))] for c in (a.get("error_classes") or {})}
        idx.set_meta("audit_history", (history + [{k: v for k, v in a.items() if k != "examples"}])[-30:])
        with open(args.out, "w") as fh:
            json.dump(a, fh, indent=2, default=str)
        with open(args.md, "w") as fh:
            fh.write(crawlaudit.to_markdown(a))
        print(crawlaudit.to_markdown(a))
        return 0
    if args.cmd == "finish":
        started = time.monotonic()
        idx.refresh_board_stats()
        pruned = idx.prune()
        from jobfeed import names
        renamed = names.fix_index(idx)  # "davispolk" -> "Davis Polk" where the index itself shows the real name
        print(f"readable company names: {renamed}")
        if idx.count_open() == 0:
            print("Nothing crawled; not publishing an empty index over yesterday's.")
            return 2
        idx.set_meta("published_at", now_iso())
        summary = {**idx.stats(), "pruned": pruned, "streams": idx.get_meta("last_streams"),
                   "tuning": idx.get_meta("crawl_tuning"),
                   "sponsors": idx.get_meta("sponsors"), "discover": idx.get_meta("last_discover"),
                   "published_at": idx.get_meta("published_at")}
        size = idx.export(args.out)
        summary["export_bytes"] = size
        with open(args.summary, "w") as fh:
            json.dump(summary, fh, indent=2, default=str)
        print(json.dumps({**summary, "seconds": round(time.monotonic() - started)}, default=str))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
