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
    c.add_argument("--ats", default="greenhouse,ashby,lever")
    c.add_argument("--minutes", type=float, default=0, help="stop starting new boards after this long")
    sub.add_parser("streams")
    s = sub.add_parser("sponsors")
    s.add_argument("--force", action="store_true")
    sub.add_parser("discover")
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
        out = crawl(idx, ats=tuple(a.strip() for a in args.ats.split(",") if a.strip()), limit=args.limit, stop=stop)
        out["directory_added"] = added
        print(json.dumps(out))
        os._exit(0)  # don't wait on a pending stop timer
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
        found = streams.discover_common_crawl()
        for ats, b in found:
            idx.add_board(ats, b, origin="discovered")
        idx.set_meta("last_discover", {"at": now_iso()[:10], "found": len(found)})
        print(json.dumps({"found": len(found)}))
        return 0
    if args.cmd == "finish":
        started = time.monotonic()
        idx.refresh_board_stats()
        pruned = idx.prune()
        if idx.count_open() == 0:
            print("Nothing crawled; not publishing an empty index over yesterday's.")
            return 2
        idx.set_meta("published_at", now_iso())
        summary = {**idx.stats(), "pruned": pruned, "streams": idx.get_meta("last_streams"),
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
