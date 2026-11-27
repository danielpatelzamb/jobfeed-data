"""Which employers sponsor work visas, from the Department of Labor's public H-1B (LCA) disclosure data.

The Office of Foreign Labor Certification posts a new LCA disclosure file each quarter on its Performance Data
page. The app checks that page daily, downloads a file only when a newer one appears, and keeps a summary per
employer: certified filings, typical pay, and pay for common job titles. (USCIS's H-1B Employer Data Hub went
offline in March 2026, so DOL is the dependable source.)
"""
from __future__ import annotations

import csv
import logging
import os
import re
import statistics
import tempfile
from collections import defaultdict
from datetime import datetime, timezone

import requests

from jobfeed.network import normalize_company
from jobfeed.sources import USER_AGENT

log = logging.getLogger("jobfeed.sponsors")

PERFORMANCE_PAGE = "https://www.dol.gov/agencies/eta/foreign-labor/performance"
FILE_BASE = "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/"
_HREF = re.compile(r"""href=["']([^"']*LCA_Disclosure_Data_FY(\d{4})_Q(\d)[^"']*\.(?:xlsx|csv))["']""", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sponsors (
    name_norm   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    certified   INTEGER NOT NULL,
    fy          TEXT,
    median_wage INTEGER
);
CREATE TABLE IF NOT EXISTS sponsor_titles (
    name_norm   TEXT NOT NULL,
    title       TEXT NOT NULL,
    n           INTEGER NOT NULL,
    median_wage INTEGER,
    PRIMARY KEY (name_norm, title)
);
"""

_HEADERS = {
    "status": ["CASE_STATUS", "STATUS"],
    "employer": ["EMPLOYER_NAME", "EMPLOYER_BUSINESS_NAME", "LCA_CASE_EMPLOYER_NAME"],
    "visa": ["VISA_CLASS", "PROGRAM"],
    "title": ["JOB_TITLE", "LCA_CASE_JOB_TITLE"],
    "wage": ["WAGE_RATE_OF_PAY_FROM", "WAGE_RATE_OF_PAY", "LCA_CASE_WAGE_RATE_FROM"],
    "unit": ["WAGE_UNIT_OF_PAY", "LCA_CASE_WAGE_RATE_UNIT"],
    "full_time": ["FULL_TIME_POSITION"],
}
_PER_YEAR = {"year": 1, "hour": 2080, "week": 52, "bi-weekly": 26, "month": 12}


def find_latest(page_html: str) -> tuple[str, str] | None:
    """(url, label like 'FY2026 Q4') of the newest LCA disclosure file linked from the page."""
    best = None
    for m in _HREF.finditer(page_html or ""):
        href, fy, q = m.group(1), int(m.group(2)), int(m.group(3))
        url = href if href.startswith("http") else "https://www.dol.gov" + (href if href.startswith("/") else "/" + href)
        if best is None or (fy, q) > best[0]:
            best = ((fy, q), url)
    return (best[1], f"FY{best[0][0]} Q{best[0][1]}") if best else None


def guess_urls(today: datetime | None = None) -> list[tuple[str, str]]:
    """If the page can't be read, try the file names OFLC uses, newest quarter first."""
    today = today or datetime.now(timezone.utc)
    fy = today.year + (1 if today.month >= 10 else 0)
    out = []
    for y in (fy, fy - 1):
        for q in (4, 3, 2, 1):
            name = f"LCA_Disclosure_Data_FY{y}_Q{q}.xlsx"
            out.append(("https://www.dol.gov/media/" + name, f"FY{y} Q{q}"))  # where OFLC posts them since 2026
            out.append((FILE_BASE + name, f"FY{y} Q{q}"))
            out.append((FILE_BASE + f"FY{str(y)[2:]}Q{q}/" + name, f"FY{y} Q{q}"))
    return out


def _annual(wage, unit) -> float | None:
    try:
        w = float(str(wage).replace("$", "").replace(",", "").split("-")[0].strip())
    except (TypeError, ValueError):
        return None
    mult = _PER_YEAR.get(str(unit or "year").strip().lower(), 1)
    v = w * mult
    return v if 15000 <= v <= 1500000 else None


def _rows_from_file(path: str):
    """Dict rows from an .xlsx (streamed) or .csv disclosure file."""
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            yield from csv.DictReader(f)
        return
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = [str(h or "").strip().upper() for h in next(it)]
    for values in it:
        yield dict(zip(header, values))
    wb.close()


def aggregate(rows) -> tuple[dict, dict]:
    """Certified H-1B filings per employer, with typical pay overall and per job title."""
    col: dict[str, str] = {}
    emp: dict[str, dict] = {}
    titles: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if not col:
            keys = {k.strip().upper(): k for k in r.keys() if k}
            for want, options in _HEADERS.items():
                col[want] = next((keys[o] for o in options if o in keys), "")
            if not col["employer"] or not col["status"]:
                raise ValueError("unexpected file layout: no employer or status column")
        status = str(r.get(col["status"]) or "").lower()
        if not status.startswith("certified"):
            continue
        visa = str(r.get(col["visa"]) or "H-1B") if col["visa"] else "H-1B"
        if "h-1b" not in visa.lower():
            continue
        name = str(r.get(col["employer"]) or "").strip()
        norm = normalize_company(name)
        if not norm:
            continue
        e = emp.setdefault(norm, {"name": name, "n": 0, "wages": []})
        e["n"] += 1
        wage = _annual(r.get(col["wage"]), r.get(col["unit"])) if col["wage"] else None
        if wage and len(e["wages"]) < 5000:
            e["wages"].append(wage)
        title = re.sub(r"\s+", " ", str(r.get(col["title"]) or "")).strip().title()[:80] if col["title"] else ""
        if title and wage and len(titles[(norm, title)]) < 500:
            titles[(norm, title)].append(wage)
    return emp, titles


def store_summary(db, emp: dict, titles: dict, fy: str):
    db.executescript(SCHEMA)
    db.execute("DELETE FROM sponsors")
    db.execute("DELETE FROM sponsor_titles")
    db.executemany("INSERT INTO sponsors (name_norm, name, certified, fy, median_wage) VALUES (?,?,?,?,?)",
                   [(k, v["name"], v["n"], fy, int(statistics.median(v["wages"])) if v["wages"] else None)
                    for k, v in emp.items()])
    db.executemany("INSERT INTO sponsor_titles (name_norm, title, n, median_wage) VALUES (?,?,?,?)",
                   [(k[0], k[1], len(w), int(statistics.median(w))) for k, w in titles.items() if len(w) >= 2])
    db.commit()


def refresh(index, session=None, force: bool = False) -> dict:
    """Daily: look for a newer disclosure file; import it only if it's new. Never raises."""
    from jobfeed.index import now_iso
    meta = index.get_meta("sponsors") or {}
    meta["checked_at"] = now_iso()
    s = session or requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    try:
        override = os.environ.get("SPONSORS_URL", "")
        latest = (override, "custom") if override else None
        if not latest:
            try:
                latest = find_latest(s.get(PERFORMANCE_PAGE, timeout=60).text)
            except requests.RequestException:
                latest = None
        if not latest:
            for url, label in guess_urls():
                try:
                    code = s.head(url, timeout=30, allow_redirects=True).status_code
                    if code in (403, 405):  # some servers refuse HEAD; ask for the first byte instead
                        with s.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=30) as g:
                            code = 200 if g.status_code in (200, 206) else g.status_code
                    if code == 200:
                        latest = (url, label)
                        break
                except requests.RequestException:
                    continue
        if not latest:
            meta["error"] = "Couldn't find the Department of Labor's file today; trying again tomorrow"
            index.set_meta("sponsors", meta)
            return meta
        url, label = latest
        if url == meta.get("url") and not force:
            meta.pop("error", None)
            index.set_meta("sponsors", meta)
            return meta
        suffix = ".csv" if url.lower().endswith(".csv") else ".xlsx"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            with s.get(url, stream=True, timeout=(30, 600)) as r:
                r.raise_for_status()
                for chunk in r.iter_content(1 << 20):
                    tmp.write(chunk)
            path = tmp.name
        try:
            emp, titles = aggregate(_rows_from_file(path))
        finally:
            os.unlink(path)
        with index.lock:
            store_summary(index.db, emp, titles, label.replace("FY", "").split()[0] if label != "custom" else "")
        meta.update({"url": url, "file": label, "employers": len(emp), "imported_at": now_iso()})
        meta.pop("error", None)
    except Exception as exc:  # a bad quarter never breaks the daily run
        log.exception("sponsor data refresh failed")
        meta["error"] = f"Import failed: {str(exc)[:120]}"
    index.set_meta("sponsors", meta)
    return meta


def import_file(index, path: str, label: str = "") -> dict:
    """Import a disclosure file the owner downloaded in a browser (DOL blocks automated downloads)."""
    from jobfeed.index import now_iso
    meta = index.get_meta("sponsors") or {}
    try:
        emp, titles = aggregate(_rows_from_file(path))
        if not emp:
            raise ValueError("no certified H-1B rows found; is this the LCA disclosure file?")
        m = re.search(r"FY(\d{4})", label or os.path.basename(path), re.I)
        q = re.search(r"Q([1-4])", label or os.path.basename(path), re.I)
        with index.lock:
            store_summary(index.db, emp, titles, m.group(1) if m else "")
        meta.update({"url": "upload", "file": (f"FY{m.group(1)}" + (f" Q{q.group(1)}" if q else "")) if m else "uploaded",
                     "employers": len(emp), "imported_at": now_iso()})
        meta.pop("error", None)
    except Exception as exc:
        log.exception("sponsor file import failed")
        meta["error"] = f"Import failed: {str(exc)[:160]}"
    index.set_meta("sponsors", meta)
    return meta


def lookup(index, company: str):
    norm = normalize_company(company)
    if not norm:
        return None
    try:
        r = index._q("SELECT * FROM sponsors WHERE name_norm = ?", (norm,), one=True)
    except Exception:
        return None
    return {"certified": r["certified"], "fy": r["fy"], "median_wage": r["median_wage"], "name": r["name"]} if r else None


def pay_for_title(index, company: str, title: str):
    """Typical pay this employer filed for the closest matching job title, if any."""
    norm = normalize_company(company)
    want = {w for w in re.findall(r"[a-z]+", title.lower()) if len(w) > 2}
    if not norm or not want:
        return None
    try:
        rows = index._q("SELECT title, n, median_wage FROM sponsor_titles WHERE name_norm = ?", (norm,))
    except Exception:
        return None
    best, score = None, 0.0
    for r in rows:
        have = {w for w in re.findall(r"[a-z]+", r["title"].lower()) if len(w) > 2}
        j = len(want & have) / len(want | have) if have else 0
        if j > score:
            best, score = r, j
    return {"title": best["title"], "n": best["n"], "median_wage": best["median_wage"]} if best and score >= 0.4 else None


def sponsor_names(index) -> set[str]:
    try:
        return {r["name_norm"] for r in index._q("SELECT name_norm FROM sponsors")}
    except Exception:
        return set()
