"""The shared job index: every posting from every board the app crawls, searchable with SQLite FTS5.

Holds public postings only, never anything about a user. Lives at data/index.db.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import zlib
from datetime import datetime, timedelta, timezone

from jobfeed.experience import years_required
from jobfeed.levels import is_us, level_of
from jobfeed.scoring import to_utc
from jobfeed.models import Job

DIRECTORY_DIR = os.path.join(os.path.dirname(__file__), "directory")
DESC_CAP = 8000  # characters of description kept per posting (compressed)

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
    ats        TEXT NOT NULL,
    board      TEXT NOT NULL,
    company    TEXT,
    origin     TEXT NOT NULL DEFAULT 'directory',
    status     TEXT NOT NULL DEFAULT 'unknown',   -- unknown | ok | empty | dead | failing
    fails      INTEGER NOT NULL DEFAULT 0,
    next_try   TEXT,
    last_ok    TEXT,
    last_count INTEGER,
    PRIMARY KEY (ats, board)
);
CREATE TABLE IF NOT EXISTS postings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    source      TEXT NOT NULL,
    board       TEXT NOT NULL,
    job_id      TEXT NOT NULL,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    location    TEXT,
    department  TEXT,
    remote      INTEGER NOT NULL DEFAULT 0,
    us          INTEGER,              -- 1 US, 0 elsewhere, NULL unknown
    level       TEXT,
    posted_at   TEXT,
    salary      TEXT,
    description BLOB,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    open        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS postings_board ON postings(source, board);
CREATE INDEX IF NOT EXISTS postings_first_seen ON postings(first_seen);
CREATE VIRTUAL TABLE IF NOT EXISTS postings_fts USING fts5(
    title, company, department, location, tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pack(text: str) -> bytes | None:
    return zlib.compress(text[:DESC_CAP].encode("utf-8"), 6) if text else None


def unpack(blob) -> str:
    if not blob:
        return ""
    return zlib.decompress(blob).decode("utf-8") if isinstance(blob, (bytes, bytearray)) else str(blob)


_SLUG_NOISE = re.compile(r"(?:[-_.]?(?:careers?|jobs|hiring|usa|us|inc|hq|talent|recruiting|team)|[-_.]?\d+)$", re.I)


def pretty_company(board: str) -> str:
    """Best guess at a company name from a board slug ('doordashusa' -> 'Doordash'). Greenhouse sends real names."""
    base = board
    for _ in range(3):
        trimmed = _SLUG_NOISE.sub("", base)
        if len(trimmed) < 2 or trimmed == base:
            break
        base = trimmed
    words = [w for w in re.split(r"[-_.]+", base) if w]
    if len(words) > 1 and words[-1].lower() in ("co", "io", "com", "ai", "app", "hq"):
        words = words[:-1] if words[-1].lower() != "ai" else words
    return " ".join("AI" if w.lower() == "ai" else w.capitalize() for w in words)


_WORD = re.compile(r"[\w][\w'+#&.-]*", re.UNICODE)


def fts_phrase(text: str) -> str:
    """Quote user text so FTS treats it as a phrase, never as query syntax."""
    words = _WORD.findall(text or "")
    return '"' + " ".join(w.replace('"', "") for w in words) + '"' if words else ""


def _role(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


GHOST_DAYS = 60


def days_since(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        dt = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


def signals(row) -> dict:
    """What a posting's history says: how long it's been up, how often it was reposted, ghost-job risk."""
    keys = row.keys()
    first = (row["first_posted"] if "first_posted" in keys else None) or (row["posted"] if "posted" in keys else None)
    age = days_since(first)
    reposts = (row["reposts"] if "reposts" in keys else 0) or 0
    ghost = (age is not None and age > GHOST_DAYS) or reposts >= 2
    why = []
    if age is not None and age > GHOST_DAYS:
        why.append(f"open {age} days")
    if reposts >= 1:
        why.append(f"reposted {reposts}×" if reposts > 1 else "reposted once")
    return {"age": age, "reposts": reposts, "ghost": ghost, "why": ", ".join(why)}


def same_role_key(row) -> tuple:
    """Companies sometimes post one role on two boards (e.g. 'pathward' and 'pathwardcareerfair')."""
    co = re.sub(r"[^a-z0-9]", "", (row["company"] or "").lower())[:6]
    return (row["title"].strip().lower(), (row["location"] or "").strip().lower(), co)


def dedupe(rows) -> list:
    seen, out = set(), []
    for r in rows:
        k = same_role_key(r)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


class JobIndex:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        from jobfeed.sponsors import SCHEMA as SPONSOR_SCHEMA
        self.db.executescript(SPONSOR_SCHEMA)
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(postings)")}
        for col, typ in (("posted", "TEXT"), ("years_min", "INTEGER"), ("reposts", "INTEGER DEFAULT 0"),
                         ("first_posted", "TEXT")):
            if col not in have:
                self.db.execute(f"ALTER TABLE postings ADD COLUMN {col} {typ}")
        self.db.execute("CREATE INDEX IF NOT EXISTS postings_posted ON postings(posted)")
        have_b = {r["name"] for r in self.db.execute("PRAGMA table_info(boards)")}
        for col in ("new_30", "new_prev_30", "open_now"):
            if col not in have_b:
                self.db.execute(f"ALTER TABLE boards ADD COLUMN {col} INTEGER")
        for col in ("last_error", "error_class", "last_tried"):
            if col not in have_b:
                self.db.execute(f"ALTER TABLE boards ADD COLUMN {col} TEXT")
        self.db.commit()

    def _q(self, sql: str, args=(), one: bool = False):
        with self.lock:  # one connection shared by crawler threads and web requests
            cur = self.db.execute(sql, args)
            return cur.fetchone() if one else cur.fetchall()

    # ---------- boards ----------

    def load_directory(self, directory_dir: str = DIRECTORY_DIR) -> int:
        """Add the bundled company list (boards already known keep their state)."""
        added = 0
        with self.lock:
            for ats in ("greenhouse", "lever", "ashby", "workday", "icims", "bamboohr", "paylocity"):
                path = os.path.join(directory_dir, f"{ats}.json")
                if not os.path.exists(path):
                    continue
                with open(path, encoding="utf-8") as f:
                    boards = json.load(f)
                rows = [(ats, b["board"], b.get("company") or None) if isinstance(b, dict) else (ats, b, None)
                        for b in boards]
                cur = self.db.executemany("INSERT OR IGNORE INTO boards (ats, board, company) VALUES (?, ?, ?)", rows)
                added += cur.rowcount
            self.db.commit()
        return added

    def add_board(self, ats: str, board: str, company: str = "", origin: str = "user"):
        with self.lock:
            self.db.execute("""INSERT INTO boards (ats, board, company, origin, status, next_try) VALUES (?,?,?,?,'unknown',NULL)
                               ON CONFLICT(ats, board) DO UPDATE SET company = COALESCE(NULLIF(excluded.company,''), company),
                               origin = CASE WHEN boards.origin = 'directory' THEN excluded.origin ELSE boards.origin END,
                               next_try = CASE WHEN boards.status IN ('dead','failing') THEN NULL ELSE boards.next_try END""",
                            (ats, board, company, origin))
            self.db.commit()

    def relevel(self, like: str = "%manager%") -> int:
        """Recompute stored levels for titles matching `like` after the level rules change (re-crawls do it anyway;
        this catches roles on boards that weren't due today)."""
        rows = self._q("SELECT id, title, level FROM postings WHERE open = 1 AND lower(title) LIKE ?", (like,))
        changed = [(lv, r["id"]) for r in rows if (lv := level_of(r["title"])) != r["level"]]
        with self.lock:
            self.db.executemany("UPDATE postings SET level = ? WHERE id = ?", changed)
            self.db.commit()
        return len(changed)

    def due_boards(self, ats: tuple[str, ...] | None = None, limit: int | None = None):
        if ats is None:
            from jobfeed.crawler import ALL_ATS
            ats = ALL_ATS
        q = (f"SELECT ats, board, company FROM boards WHERE ats IN ({','.join('?' * len(ats))}) "
             "AND (next_try IS NULL OR next_try <= ? OR (status = 'failing' AND error_class IS NULL)) "
             # failures recorded before errors were classified (and before polite retries) get one fresh try
             # least recently tried first, so boards a slowed-down run didn't reach go first next time
             "ORDER BY origin != 'user', last_tried IS NOT NULL, last_tried, status != 'ok', ats, board")
        args = [*ats, now_iso()]
        if limit:
            q += " LIMIT ?"
            args.append(limit)
        return self._q(q, args)

    def board_result(self, ats: str, board: str, ok: bool, count: int = 0, dead: bool = False, company: str = "",
                     error: str = ""):
        """Record a crawl of one board, why it failed (if it did), and when to try it next."""
        from jobfeed.crawlaudit import BACKOFF_DAYS, classify
        cls = classify(error) if not ok else None
        now = datetime.now(timezone.utc)
        with self.lock:
            row = self.db.execute("SELECT fails FROM boards WHERE ats=? AND board=?", (ats, board)).fetchone()
            fails = (row["fails"] if row else 0)
            if ok:
                status, fails, nxt = ("ok" if count else "empty"), 0, None
                if not count:  # empty boards rarely change; check twice a week
                    nxt = (now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                fails += 1
                status = "dead" if dead else "failing"
                days = 14 if dead else BACKOFF_DAYS.get(cls or "", min(2 ** fails, 14))
                nxt = (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.db.execute("""INSERT INTO boards (ats, board, company, status, fails, next_try, last_ok, last_count)
                               VALUES (?,?,?,?,?,?,?,?)
                               ON CONFLICT(ats, board) DO UPDATE SET status=excluded.status, fails=excluded.fails,
                               next_try=excluded.next_try, last_ok=COALESCE(excluded.last_ok, boards.last_ok),
                               last_count=COALESCE(excluded.last_count, boards.last_count),
                               company=COALESCE(NULLIF(excluded.company,''), boards.company)""",
                            (ats, board, company, status, fails, nxt, now_iso() if ok else None,
                             count if ok else None))
            self.db.execute("UPDATE boards SET last_error = ?, error_class = ?, last_tried = ? WHERE ats = ? AND board = ?",
                            (None if ok else (error or "")[:300], cls, now_iso(), ats, board))
            self.db.commit()

    # ---------- postings ----------

    def _fts_put(self, rowid: int, job: Job):
        # Titles, companies, teams and places are indexed; descriptions are stored (compressed) for
        # filtering and AI ranking but kept out of the index to stay small on a 1-5 GB disk.
        self.db.execute("DELETE FROM postings_fts WHERE rowid = ?", (rowid,))
        self.db.execute("INSERT INTO postings_fts (rowid, title, company, department, location) VALUES (?,?,?,?,?)",
                        (rowid, job.title, job.company, job.department, job.location))

    def ingest_board(self, ats: str, board: str, jobs: list[Job], seen_at: str | None = None) -> list[int]:
        """Upsert one board's postings and close the ones that disappeared. Returns ids of new postings."""
        seen_at = seen_at or now_iso()
        new_ids = []
        with self.lock:
            rows = self.db.execute("""SELECT id, key, title, location, reposts, first_posted, posted, first_seen
                                      FROM postings WHERE source=? AND board=?""", (ats, board)).fetchall()
            existing = {r["key"]: (r["id"], r["title"], r["location"]) for r in rows}
            # The same role under a new id = a repost. Remember the earliest date it was ever posted.
            roles: dict[tuple, tuple] = {}  # role -> (reposts, first ever posted, latest posted)
            for r in rows:
                k = (_role(r["title"]), _role(r["location"]))
                first = min(x for x in (r["first_posted"], r["posted"], r["first_seen"]) if x)
                last = r["posted"] or r["first_seen"]
                prev = roles.get(k)
                roles[k] = (max(r["reposts"] or 0, prev[0] if prev else 0), min(first, prev[1]) if prev else first,
                            max(last, prev[2]) if prev else last)
            keep = set()
            for job in jobs:
                if not job.title or not job.url:
                    continue
                desc = job.description or ""
                lvl = level_of(job.title)
                posted = to_utc(job.posted_at) or job.extra.get("first_seen") or seen_at
                years = years_required(desc)[0]
                us = is_us(job.location)
                us_val = None if us is None else int(us)
                if job.key in existing:
                    pid, old_title, old_loc = existing[job.key]
                    keep.add(job.key)
                    keep_old = bool(job.extra.get("keep_desc")) and not desc  # page not re-read: keep what we had
                    self.db.execute("""UPDATE postings SET title=?, company=?, location=?, department=?, remote=?, us=?,
                                       level=?, salary=COALESCE(NULLIF(?, ''), salary),
                                       description=CASE WHEN ? THEN description ELSE ? END, last_seen=?, open=1,
                                       years_min=CASE WHEN ? THEN years_min ELSE ? END,
                                       posted=COALESCE(?, posted) WHERE id=?""",
                                    (job.title, job.company, job.location, job.department, int(job.remote), us_val,
                                     lvl, job.salary, int(keep_old), _pack(desc), seen_at, int(keep_old), years,
                                     to_utc(job.posted_at) or None, pid))
                    if old_title != job.title or old_loc != job.location:
                        self._fts_put(pid, job)
                    continue
                before = roles.get((_role(job.title), _role(job.location)))
                # Several openings posted together aren't reposts; the same role coming back a week+ later is.
                if before and days_since(before[2]) is not None and (days_since(before[2]) or 0) - \
                        (days_since(posted) or 0) >= 7:
                    reposts, first_posted = before[0] + 1, min(before[1], posted)
                else:
                    reposts, first_posted = (before[0], min(before[1], posted)) if before else (0, posted)
                cur = self.db.execute(
                    """INSERT INTO postings (key, source, board, job_id, company, title, url, location, department, remote,
                       us, level, posted_at, salary, description, first_seen, last_seen, posted, years_min, reposts,
                       first_posted)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen, open=1""",
                    (job.key, job.source, job.board, job.job_id, job.company, job.title, job.url, job.location,
                     job.department, int(job.remote), us_val, lvl, job.posted_at, job.salary, _pack(desc),
                     job.extra.get("first_seen") or seen_at, seen_at, posted, years, reposts, first_posted))
                roles[(_role(job.title), _role(job.location))] = (reposts, first_posted,
                                                                  max(posted, before[2]) if before else posted)
                pid = cur.lastrowid
                keep.add(job.key)
                self._fts_put(pid, job)
                new_ids.append(pid)
            gone = [v[0] for k, v in existing.items() if k not in keep]
            if gone:
                self.db.executemany("UPDATE postings SET open = 0 WHERE id = ?", [(i,) for i in gone])
            self.db.commit()
        return new_ids

    def existing_for(self, ats: str, board: str) -> dict:
        """url -> what the index already knows, so careers-site pages aren't re-read every day."""
        return {r["url"]: {"title": r["title"], "location": r["location"], "has_desc": bool(r["has_desc"])}
                for r in self._q("""SELECT url, title, location, description IS NOT NULL AND length(description) > 0
                                    AS has_desc FROM postings WHERE source = ? AND board = ?""", (ats, board))}

    def close_missing(self, source: str, keep: set[str]) -> int:
        """Close a stream's open postings that its latest full read didn't include."""
        with self.lock:
            gone = [(r["id"],) for r in self.db.execute("SELECT id, key FROM postings WHERE source = ? AND open = 1",
                                                        (source,)) if r["key"] not in keep]
            self.db.executemany("UPDATE postings SET open = 0 WHERE id = ?", gone)
            self.db.commit()
            return len(gone)

    def hot_boards(self, days: int = 7, ats: tuple[str, ...] = ("greenhouse", "ashby", "lever"), limit: int = 3000):
        """Boards worth re-reading during the day: followed by someone, or posted something this week."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._q(f"""SELECT b.ats, b.board, b.company FROM boards b
                           WHERE b.ats IN ({','.join('?' * len(ats))}) AND b.status IN ('ok', 'unknown')
                             AND (b.origin = 'user' OR EXISTS (SELECT 1 FROM postings p WHERE p.source = b.ats
                                  AND p.board = b.board AND p.first_seen >= ?))
                           ORDER BY b.origin != 'user', b.last_count DESC LIMIT ?""", (*ats, since, limit))

    def refresh_board_stats(self):
        """Hiring momentum per company: roles posted in the last 30 days vs the 30 before, and open now."""
        now = datetime.now(timezone.utc)
        d30 = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        d60 = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            self.db.execute("UPDATE boards SET new_30 = 0, new_prev_30 = 0, open_now = 0")
            self.db.execute("""UPDATE boards SET
                new_30 = (SELECT COUNT(*) FROM postings p WHERE p.source = boards.ats AND p.board = boards.board
                          AND p.reposts = 0 AND COALESCE(p.posted, p.first_seen) >= ?),
                new_prev_30 = (SELECT COUNT(*) FROM postings p WHERE p.source = boards.ats AND p.board = boards.board
                          AND p.reposts = 0 AND COALESCE(p.posted, p.first_seen) >= ? AND COALESCE(p.posted, p.first_seen) < ?),
                open_now = (SELECT COUNT(*) FROM postings p WHERE p.source = boards.ats AND p.board = boards.board
                          AND p.open = 1)
                WHERE EXISTS (SELECT 1 FROM postings p WHERE p.source = boards.ats AND p.board = boards.board)""",
                            (d30, d60, d30))
            # stream sources (Workable, HN, USAJOBS) have no boards rows; add them so momentum covers them too
            self.db.execute("""INSERT OR IGNORE INTO boards (ats, board, company, origin, status)
                               SELECT DISTINCT source, board, company, 'stream', 'ok' FROM postings
                               WHERE source IN ('workable', 'hn', 'usajobs', 'capture')""")
            self.db.commit()

    def momentum(self, source: str, board: str) -> dict | None:
        r = self._q("SELECT new_30, new_prev_30, open_now FROM boards WHERE ats = ? AND board = ?", (source, board),
                    one=True)
        if not r or r["new_30"] is None:
            return None
        new, prev = r["new_30"] or 0, r["new_prev_30"] or 0
        trend = "up" if new > prev * 1.5 and new >= 3 else ("down" if prev > new * 1.5 and prev >= 3 else "flat")
        return {"new_30": new, "prev_30": prev, "open": r["open_now"] or 0, "trend": trend}

    def lookup(self, keys: list[str]) -> dict:
        """Index rows for a list of posting keys (for showing signals on a user's saved roles)."""
        out = {}
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            for r in self._q(f"SELECT * FROM postings WHERE key IN ({','.join('?' * len(chunk))})", chunk):
                out[r["key"]] = r
        return out

    def prune(self, days: int = 120) -> int:
        """Forget postings that closed long ago, so the index stays small."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM postings WHERE open = 0 AND last_seen < ?", (cutoff,))]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                self.db.execute(f"DELETE FROM postings_fts WHERE rowid IN ({marks})", chunk)
                self.db.execute(f"DELETE FROM postings WHERE id IN ({marks})", chunk)
            self.db.commit()
        return len(ids)

    def export(self, out_path: str) -> int:
        """A compact, consistent copy of the whole index, gzipped, for publishing."""
        import gzip
        import shutil
        tmp = out_path + ".tmp.db"
        if os.path.exists(tmp):
            os.unlink(tmp)
        with self.lock:
            self.db.execute("VACUUM INTO ?", (tmp,))
        with open(tmp, "rb") as src, gzip.open(out_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        os.unlink(tmp)
        return os.path.getsize(out_path)

    def export_shard(self, out_path: str, sources) -> dict:
        """Only these platforms' boards and postings, plus the crawl summary, for merging into the day's index."""
        sources = tuple(sources)
        marks = ",".join("?" * len(sources))
        if os.path.exists(out_path):
            os.unlink(out_path)
        with self.lock:
            self.db.commit()
            self.db.execute("ATTACH DATABASE ? AS shard", (out_path,))
            try:
                self.db.execute("CREATE TABLE shard.boards AS SELECT * FROM boards WHERE 0")
                self.db.execute("CREATE TABLE shard.postings AS SELECT * FROM postings WHERE 0")
                self.db.execute("CREATE TABLE shard.meta (key TEXT PRIMARY KEY, value TEXT)")
                self.db.execute(f"INSERT INTO shard.boards SELECT * FROM boards WHERE ats IN ({marks})", sources)
                self.db.execute(f"INSERT INTO shard.postings SELECT * FROM postings WHERE source IN ({marks})", sources)
                self.db.execute("INSERT INTO shard.meta SELECT key, value FROM meta WHERE key IN ('last_crawl', "
                                "'crawl_tuning')")
                self.db.execute("INSERT INTO shard.meta VALUES ('sources', ?)", (json.dumps(sources),))
                self.db.commit()
                n = self.db.execute("SELECT COUNT(*) FROM shard.postings").fetchone()[0]
            finally:
                self.db.execute("DETACH DATABASE shard")
        return {"postings": n, "sources": sources}

    def merge_shard(self, path: str) -> dict:
        """Replace these platforms' boards and postings with a shard's, matching postings by key so ids (and the
        search index) stay stable for roles that were already here."""
        src = sqlite3.connect(path)
        try:
            sources = tuple(json.loads(src.execute("SELECT value FROM meta WHERE key = 'sources'").fetchone()[0]))
            crawl = src.execute("SELECT value FROM meta WHERE key = 'last_crawl'").fetchone()
            tuning = src.execute("SELECT value FROM meta WHERE key = 'crawl_tuning'").fetchone()
        finally:
            src.close()
        marks = ",".join("?" * len(sources))
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(postings)") if r[1] != "id"]
        cl = ", ".join(cols)
        upd = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "key")
        with self.lock:
            self.db.commit()
            self.db.execute("ATTACH DATABASE ? AS shard", (path,))
            try:
                scols = {r[1] for r in self.db.execute("PRAGMA shard.table_info(postings)")}
                sel = ", ".join(c if c in scols else "NULL" for c in cols)
                self.db.execute(f"DELETE FROM postings_fts WHERE rowid IN (SELECT id FROM postings WHERE source IN ({marks}))",
                                sources)
                self.db.execute(f"DELETE FROM postings WHERE source IN ({marks}) AND key NOT IN "
                                f"(SELECT key FROM shard.postings)", sources)
                self.db.execute(f"INSERT INTO postings ({cl}) SELECT {sel} FROM shard.postings WHERE true "
                                f"ON CONFLICT(key) DO UPDATE SET {upd}")
                self.db.execute(f"INSERT INTO postings_fts (rowid, title, company, department, location) SELECT id, title, "
                                f"company, COALESCE(department, ''), COALESCE(location, '') FROM postings "
                                f"WHERE source IN ({marks})", sources)
                self.db.execute(f"DELETE FROM boards WHERE ats IN ({marks})", sources)
                bcols = [r[1] for r in self.db.execute("PRAGMA shard.table_info(boards)")]
                self.db.execute(f"INSERT OR REPLACE INTO boards ({', '.join(bcols)}) SELECT {', '.join(bcols)} FROM shard.boards")
                self.db.commit()
                n = self.db.execute(f"SELECT COUNT(*) FROM postings WHERE source IN ({marks}) AND open = 1",
                                    sources).fetchone()[0]
            finally:
                self.db.execute("DETACH DATABASE shard")
        if tuning and tuning[0]:  # each machine tuned its own platforms; keep those numbers
            t = self.get_meta("crawl_tuning") or {}
            t.update({k: v for k, v in json.loads(tuning[0]).items() if k in sources})
            self.set_meta("crawl_tuning", t)
        return {"sources": sources, "open": n, "crawl": json.loads(crawl[0]) if crawl and crawl[0] else None}

    def _carry_sponsors(self, new_db_path: str):
        """The published index can't include the visa-sponsor tables (the owner imports that file by hand), so keep
        ours when the new one has none."""
        try:
            mine = self._q("SELECT COUNT(*) AS n FROM sponsors", one=True)["n"]
        except Exception:
            return
        if not mine:
            return
        from jobfeed.sponsors import SCHEMA as SPONSOR_SCHEMA
        new = sqlite3.connect(new_db_path)
        try:
            new.executescript(SPONSOR_SCHEMA)
            if new.execute("SELECT COUNT(*) FROM sponsors").fetchone()[0]:
                return
            new.execute("ATTACH DATABASE ? AS old", (self.path,))
            new.execute("INSERT INTO sponsors SELECT * FROM old.sponsors")
            new.execute("INSERT INTO sponsor_titles SELECT * FROM old.sponsor_titles")
            meta = new.execute("SELECT value FROM old.meta WHERE key = 'sponsors'").fetchone()
            if meta:
                new.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('sponsors', ?)", (meta[0],))
            new.commit()
            new.execute("DETACH DATABASE old")
        finally:
            new.close()

    # ---------- roles members saved from big employers' own sites ----------
    # Google, Amazon, Meta, Apple and Microsoft post on their own sites, which we don't crawl. When a member saves a
    # public posting from one of them, its link, title, company and place go into the index for everyone it fits
    # (never who saved it). They expire SHARED_DAYS after the last save.
    SHARED_DAYS = 30

    def add_shared(self, job: Job) -> bool:
        """Add or refresh one shared posting. Returns True when it's new."""
        board = job.board
        rows = self._q("SELECT * FROM postings WHERE source = 'shared' AND board = ? AND open = 1", (board,))
        others = [self.as_job(r) for r in rows if r["key"] != job.key]
        for o, r in zip(others, [r for r in rows if r["key"] != job.key]):
            o.extra["first_seen"] = r["first_seen"]
            o.extra["keep_desc"] = True
        before = {r["key"] for r in rows}
        self.ingest_board("shared", board, others + [job])
        with self.lock:  # others keep their own last-saved date; only this one is refreshed
            for r in rows:
                if r["key"] != job.key:
                    self.db.execute("UPDATE postings SET last_seen = ? WHERE key = ?", (r["last_seen"], r["key"]))
            self.db.commit()
        return job.key not in before

    def expire_shared(self, days: int | None = None) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days or self.SHARED_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.lock:
            n = self.db.execute("UPDATE postings SET open = 0 WHERE source = 'shared' AND open = 1 AND last_seen < ?",
                                (cutoff,)).rowcount
            self.db.commit()
        return n

    def replace_with(self, new_db_path: str):
        """Swap in a freshly downloaded index (atomic rename), then reopen."""
        test = sqlite3.connect(new_db_path)
        try:
            ok = test.execute("PRAGMA quick_check").fetchone()[0] == "ok" and \
                test.execute("SELECT COUNT(*) FROM postings").fetchone()[0] > 0
        finally:
            test.close()
        if not ok:
            raise ValueError("downloaded index failed its integrity check")
        self._carry_sponsors(new_db_path)
        try:  # members' shared saves live only on this server: bring them into the new index
            shared = [(self.as_job(r), r["first_seen"], r["last_seen"]) for r in
                      self._q("SELECT * FROM postings WHERE source = 'shared' AND open = 1")]
        except Exception:
            shared = []
        with self.lock:
            self.db.close()
            for ext in ("-wal", "-shm"):
                if os.path.exists(self.path + ext):
                    os.unlink(self.path + ext)
            os.replace(new_db_path, self.path)
            self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
        by_board: dict[str, list] = {}
        for job, first, last in shared:
            job.extra["first_seen"] = first
            by_board.setdefault(job.board, []).append((job, last))
        for board, items in by_board.items():
            self.ingest_board("shared", board, [j for j, _ in items])
            with self.lock:
                self.db.executemany("UPDATE postings SET last_seen = ? WHERE key = ?", [(l, j.key) for j, l in items])
                self.db.commit()

    def get(self, posting_id: int):
        return self._q("SELECT * FROM postings WHERE id = ?", (posting_id,), one=True)

    def as_job(self, row) -> Job:
        return Job(source=row["source"], board=row["board"], job_id=row["job_id"], company=row["company"],
                   title=row["title"], url=row["url"], location=row["location"] or "", department=row["department"] or "",
                   remote=bool(row["remote"]), posted_at=row["posted"] or row["posted_at"] or "",
                   salary=row["salary"] or "",
                   description=unpack(row["description"]))

    def stats(self) -> dict:
        r = self._q("""SELECT COUNT(*) AS n, SUM(open) AS open, COUNT(DISTINCT source || ':' || board) AS boards
                       FROM postings""", one=True)
        b = self._q("SELECT status, COUNT(*) AS n FROM boards GROUP BY status")
        by_source = {x["source"]: {"open": x["open"], "companies": x["companies"]} for x in self._q(
            "SELECT source, SUM(open) AS open, COUNT(DISTINCT board) AS companies FROM postings GROUP BY source")}
        return {"postings": r["n"] or 0, "open": r["open"] or 0, "companies": r["boards"] or 0,
                "boards": {x["status"]: x["n"] for x in b}, "last_crawl": self.get_meta("last_crawl"),
                "by_source": by_source}

    def get_meta(self, key: str, default=None):
        row = self._q("SELECT value FROM meta WHERE key = ?", (key,), one=True)
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))
            self.db.commit()

    # ---------- search ----------

    def search(self, terms: list[str] | None = None, *, query: str = "", us_only: bool = False,
               levels: list[str] | None = None, remote_only: bool = False, since: str | None = None,
               where: str = "", limit: int = 50, offset: int = 0, title_only: bool = True,
               posted_since: str | None = None, max_years: int | None = None, order: str = "relevance"):
        """Find open postings. `terms` are title phrases OR'd together (from the questionnaire);
        `query` is free text from the search box. Ranked by relevance, then freshness."""
        clauses, args = ["p.open = 1"], []
        match_parts = []
        phrases = [fts_phrase(t) for t in (terms or []) if fts_phrase(t)]
        if phrases:
            col = "title" if title_only else "{title department}"
            match_parts.append(f"{col} : ({' OR '.join(phrases)})")
        if query.strip():
            words = [fts_phrase(w) for w in _WORD.findall(query)]
            match_parts.append(" AND ".join(w for w in words if w))
        if where.strip():
            match_parts.append("location : " + fts_phrase(where))
        if us_only:
            clauses.append("(p.us = 1 OR (p.us IS NULL AND p.remote = 1))")
        if levels:
            clauses.append(f"p.level IN ({','.join('?' * len(levels))})")
            args += levels
        if remote_only:
            clauses.append("p.remote = 1")
        if since:
            clauses.append("p.first_seen >= ?")
            args.append(since)
        if posted_since:
            clauses.append("COALESCE(p.posted, p.first_seen) >= ?")
            args.append(posted_since)
        if max_years is not None:
            clauses.append("(p.years_min IS NULL OR p.years_min <= ?)")
            args.append(max_years)
        age = "(julianday('now') - julianday(COALESCE(p.posted, p.first_seen)))"
        if match_parts:
            sql = (f"""SELECT p.*, bm25(postings_fts, 10.0, 3.0, 2.0, 1.0) AS rank FROM postings_fts
                       JOIN postings p ON p.id = postings_fts.rowid
                       WHERE postings_fts MATCH ? AND {' AND '.join(clauses)}
                       ORDER BY {"COALESCE(p.posted, p.first_seen) DESC, rank" if order == "newest"
                                 else f"rank + {age} * 0.05"} LIMIT ? OFFSET ?""")
            args = [" AND ".join(f"({m})" for m in match_parts), *args, limit, offset]
        else:
            sql = f"""SELECT p.*, 0 AS rank FROM postings p WHERE {' AND '.join(clauses)}
                      ORDER BY COALESCE(p.posted, p.first_seen) DESC LIMIT ? OFFSET ?"""
            args += [limit, offset]
        return self._q(sql, args)

    def open_at(self, source: str, board: str, limit: int = 60):
        """Open postings on one company's board, newest first."""
        return self._q("""SELECT id, title, location, url, posted_at, first_seen, company FROM postings
                          WHERE source = ? AND board = ? AND open = 1
                          ORDER BY COALESCE(posted_at, first_seen) DESC LIMIT ?""", (source, board, limit))

    def count_open(self) -> int:
        return self._q("SELECT COUNT(*) FROM postings WHERE open = 1", one=True)[0]
