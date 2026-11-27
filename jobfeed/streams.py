"""Extra free job streams beyond the per-company board APIs.

- Workable's public XML feed: every Workable customer's published jobs in one file, rebuilt hourly and
  documented for job boards (help.workable.com/hc/en-us/articles/4420464031767). One download per hour max,
  gzip required, job URLs must stay unchanged.
- Hacker News "Who is hiring?": the monthly thread via the free Algolia HN API. Startups, often small.
- USAJOBS: federal jobs, including student and recent-graduate hiring paths. Free key from developer.usajobs.gov.
- Board discovery: any Greenhouse, Lever or Ashby link seen in a stream (or in Common Crawl's public URL index)
  adds that company's board to the directory, so coverage grows on its own.
"""
from __future__ import annotations

import gzip
import html
import json
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Iterable, Iterator

import requests

from jobfeed.models import Job, html_to_text
from jobfeed.sources import USER_AGENT

log = logging.getLogger("jobfeed.streams")

# ---------- discovery ----------

_BOARD_URL = re.compile(
    r"https?://(?:(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?(?P<gh>[A-Za-z0-9_-]+)"
    r"|jobs\.(?:eu\.)?lever\.co/(?P<lv>[A-Za-z0-9._-]+)"
    r"|jobs\.ashbyhq\.com/(?P<ab>[A-Za-z0-9._%-]+)"
    r"|(?:jobs|careers)\.smartrecruiters\.com/(?P<sr>[A-Za-z0-9_-]+))", re.I)
_NOT_BOARD = {"embed", "jobs", "api", "v1", "static", "assets", "favicon.ico", "robots.txt", "sitemap.xml"}


_ANY_URL = re.compile(r"https?://[^\s\"'<>)]+", re.I)


def discover_boards(text: str) -> set[tuple[str, str]]:
    """Job boards and careers sites (Greenhouse, Lever, Ashby, Workday, iCIMS, Taleo, SuccessFactors, Oracle)
    linked anywhere in some text."""
    from jobfeed import smb
    from jobfeed.careers import board_from_link
    out = set()
    for u in _ANY_URL.findall(html.unescape(text or "")):
        if any(h in u for h in ("myworkdayjobs.com", "icims.com", "taleo.net", "jobs2web.com", "successfactors",
                                "oraclecloud.com")):
            found = board_from_link(u)
            if found:
                out.add(found)
        else:
            found = smb.board_from_link(u)
            if found:
                out.add(found)
    for m in _BOARD_URL.finditer(html.unescape(text or "")):
        for ats, group in (("greenhouse", "gh"), ("lever", "lv"), ("ashby", "ab"), ("smartrecruiters", "sr")):
            slug = m.group(group)
            if slug and slug.lower() not in _NOT_BOARD:
                out.add((ats, slug.strip(".").replace("%20", " ") if ats == "ashby" else slug.strip(".")))
    return out


# Public, community-kept internship and new-grad lists on GitHub. Only the employer links are read from them (which
# job boards and careers sites companies use); none of their rows, titles or wording is copied. Each role is then
# read from the employer's own board. Add or remove lists with LINK_LISTS (comma-separated raw JSON/Markdown URLs).
LINK_LISTS = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
)


def discover_link_lists(urls=None, session=None) -> set[tuple[str, str]]:
    import os
    urls = urls or [u for u in os.environ.get("LINK_LISTS", ",".join(LINK_LISTS)).split(",") if u.strip()]
    s = session or requests.Session()
    out: set[tuple[str, str]] = set()
    for u in urls:
        try:
            r = s.get(u.strip(), timeout=60, headers={"User-Agent": USER_AGENT})
            if r.status_code == 200:
                out |= discover_boards(r.text)
        except requests.RequestException:
            continue  # one missing list never stops discovery
    return out


def discover_from_cdx_lines(lines: Iterable[str]) -> set[tuple[str, str]]:
    """Common Crawl CDX index lines (JSON per line, with a "url" field) -> boards."""
    found = set()
    for line in lines:
        try:
            url = json.loads(line).get("url", "")
        except (ValueError, AttributeError):
            continue
        found |= discover_boards(url)
    return found


CC_HOSTS = ("boards.greenhouse.io/*", "job-boards.greenhouse.io/*", "jobs.lever.co/*", "jobs.ashbyhq.com/*",
            "*.myworkdayjobs.com", "*.icims.com", "*.taleo.net", "*.jobs2web.com", "*.successfactors.com",
            "*.oraclecloud.com/hcmUI/CandidateExperience/*", "*.bamboohr.com", "*.recruitee.com",
            "*.jobs.personio.de", "*.breezy.hr", "*.teamtailor.com", "*.applytojob.com", "jobs.jobvite.com/*",
            "ats.rippling.com/*", "recruiting.paylocity.com/recruiting/jobs/*")


def discover_common_crawl(crawl_id: str | None = None, max_pages: int = 30, session=None) -> set[tuple[str, str]]:
    """Ask Common Crawl's free URL index which job boards it has seen. Slow but free; run weekly at most."""
    s = session or requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    if not crawl_id:
        crawl_id = s.get("https://index.commoncrawl.org/collinfo.json", timeout=30).json()[0]["id"]
    api = f"https://index.commoncrawl.org/{crawl_id}-index"
    found: set[tuple[str, str]] = set()
    for host in CC_HOSTS:
        params = {"url": host, "output": "json"}
        if host.startswith("*."):
            params = {"url": host[2:], "matchType": "domain", "output": "json"}
        try:
            pages = s.get(api, params={**params, "showNumPages": "true"}, timeout=60).json()
        except (requests.RequestException, ValueError):
            log.warning("Common Crawl index didn't answer for %s; skipping it this time", host)
            continue
        for page in range(min(int(pages.get("pages", 1)), max_pages)):
            r = s.get(api, params={**params, "fl": "url", "page": page}, timeout=120)
            if r.status_code != 200:
                break
            found |= discover_from_cdx_lines(r.text.splitlines())
    return found


# ---------- Web Data Commons: every site with job markup ----------
# Web Data Commons (University of Mannheim) extracts schema.org data from Common Crawl and publishes a
# JobPosting subset as N-Quads, where the 4th term of each line is the page the markup came from.

_QUAD = re.compile(r'^(\S+) <http://www\.w3\.org/1999/02/22-rdf-syntax-ns#type> <https?://schema\.org/JobPosting> <([^>]+)>')
_KNOWN = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "icims.com", "taleo.net", "bamboohr.com",
          "recruitee.com", "personio.de", "breezy.hr", "teamtailor.com", "workable.com", "linkedin.com", "indeed.",
          "glassdoor.", "ziprecruiter.", "monster.", "simplyhired.", "careerbuilder.")


def sites_from_wdc_lines(lines: Iterable[str], min_pages: int = 3) -> dict[str, int]:
    """Hosts that published job markup on at least `min_pages` pages (job boards and known systems excluded:
    those are read through their own feeds, and aggregators only copy employers' postings)."""
    from collections import Counter
    from urllib.parse import urlparse
    count: Counter = Counter()
    for line in lines:
        m = _QUAD.match(line)
        if not m:
            continue
        host = urlparse(m.group(2)).netloc.lower()
        if host and not any(k in host for k in _KNOWN):
            count[host] += 1
    return {h: n for h, n in count.items() if n >= min_pages}


def discover_wdc(url: str, max_lines: int = 50_000_000, session=None) -> dict[str, int]:
    """Stream a (gzipped) WDC JobPosting N-Quads file and return employer sites with job markup."""
    s = session or requests.Session()
    with s.get(url, stream=True, timeout=(30, 600), headers={"User-Agent": USER_AGENT}) as r:
        r.raise_for_status()
        raw = r.raw
        stream = gzip.GzipFile(fileobj=raw) if url.endswith(".gz") else raw

        def lines():
            for i, b in enumerate(stream):
                if i >= max_lines:
                    break
                yield b.decode("utf-8", "replace")
        return sites_from_wdc_lines(lines())


# ---------- Hacker News: Who is hiring? ----------

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM = "https://hn.algolia.com/api/v1/items/{id}"
_LOC_HINT = re.compile(r"remote|onsite|on-site|hybrid|\b[A-Z]{2}\b|,|new york|san francisco|london|nyc|sf\b|bay area|"
                       r"austin|boston|seattle|chicago|berlin|toronto|anywhere|worldwide|usa?\b", re.I)
_ROLE_HINT = re.compile(r"engineer|developer|analyst|designer|manager|lead|scientist|associate|head of|director|"
                        r"founding|intern|operations|ops\b|sales|marketing|growth|product|strategy|recruit|"
                        r"researcher|architect|sre\b|devops|account|business|finance|counsel|writer|support|roles?\b|"
                        r"positions?|openings|hiring", re.I)
_CLOSED = re.compile(r"no longer (?:taking|accepting|hiring)|positions? (?:have been )?filled|closed\b", re.I)


def _hn_header(text: str) -> str:
    first = re.split(r"<p>", text or "", maxsplit=1)[0]
    return html_to_text(first)


def parse_hn_comment(c: dict, month: str = "") -> Job | None:
    text = c.get("text") or ""
    header = _hn_header(text)
    if not header or "|" not in header or _CLOSED.search(html_to_text(text)[:300]):
        return None
    parts = [p.strip() for p in header.split("|") if p.strip()]
    company = re.sub(r"\s*\((?:YC [A-Z]\d+|Series [A-Z]|seed|[^)]{0,30})\)\s*$", "", parts[0], flags=re.I).strip()
    rest = parts[1:]
    role = next((p for p in rest if _ROLE_HINT.search(p) and not re.fullmatch(r"(?i)remote|onsite|hybrid", p)), "")
    places = [p for p in rest if p != role and _LOC_HINT.search(p) and len(p) < 80
              and not re.fullmatch(r"(?i)\s*(remote|onsite|on-site|hybrid|in[- ]office)\s*", p)]
    remote = any(re.search(r"\bremote\b", p, re.I) for p in rest)
    if not company or len(company) > 80:
        return None
    cid = str(c.get("id"))
    return Job(source="hn", board=re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-") or "unknown", job_id=cid,
               company=company, title=(role or "Several roles")[:140], url=f"https://news.ycombinator.com/item?id={cid}",
               location="; ".join(places)[:120], remote=remote, posted_at=c.get("created_at") or "",
               description=html_to_text(text)[:6000], extra={"thread": month})


def parse_hn_thread(item: dict) -> tuple[list[Job], set[tuple[str, str]]]:
    jobs, boards = [], set()
    month = item.get("title", "")
    for c in item.get("children") or []:
        if not c.get("text"):
            continue
        boards |= discover_boards(c["text"])
        job = parse_hn_comment(c, month)
        if job:
            jobs.append(job)
    return jobs, boards


def fetch_hn(session=None) -> tuple[list[Job], set[tuple[str, str]]]:
    s = session or requests.Session()
    r = s.get(HN_SEARCH, params={"tags": "story,author_whoishiring", "hitsPerPage": 5}, timeout=30,
              headers={"User-Agent": USER_AGENT})
    story = next((h for h in r.json().get("hits", []) if h.get("title", "").startswith("Ask HN: Who is hiring")), None)
    if not story:
        return [], set()
    item = s.get(HN_ITEM.format(id=story["objectID"]), timeout=60, headers={"User-Agent": USER_AGENT}).json()
    return parse_hn_thread(item)


# ---------- Workable feed ----------

WORKABLE_FEED_URL = "https://www.workable.com/boards/workable.xml"


def _rfc822(text: str) -> str:
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def parse_workable_feed(stream, countries: set[str] | None = None) -> Iterator[Job]:
    """Stream <job> elements out of the feed. One posting can appear once per location; the first wins.
    `countries` (ISO codes like {"US"}) keeps only those countries plus remote jobs."""
    seen = set()
    root = None
    for event, elem in ET.iterparse(stream, events=("start", "end")):
        if event == "start":
            if root is None:
                root = elem
            continue
        if elem.tag != "job":
            continue
        f = {child.tag: (child.text or "").strip() for child in elem}
        elem.clear()
        if root is not None:
            try:
                root.remove(elem)
            except ValueError:
                pass
        url, title, company = f.get("url", ""), f.get("title", ""), f.get("company", "")
        if not url.startswith("https://") or not title or not company or url in seen:
            continue
        country = (f.get("country") or "").upper()
        remote = (f.get("remote") or "").lower() == "true"
        if countries and country not in countries and not remote:
            continue
        seen.add(url)
        place = ", ".join(p for p in (f.get("city"), f.get("state"), country) if p)
        yield Job(source="workable", board=re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-"),
                  job_id=f.get("referencenumber") or url.rstrip("/").rsplit("/", 1)[-1], company=company,
                  title=title, url=url, location=place or ("Remote" if remote else ""), remote=remote,
                  department=f.get("category", ""), posted_at=_rfc822(f.get("date", "")),
                  description=html_to_text(f.get("description"))[:8000])


def fetch_workable(countries: set[str] | None = None, tmp_dir: str | None = None) -> Iterator[Job]:
    """Download the feed (about 900 MB of XML, sent gzipped) to a temp file, then parse it. At most once an hour."""
    with tempfile.TemporaryFile(dir=tmp_dir) as raw:
        with requests.get(WORKABLE_FEED_URL, stream=True, timeout=(30, 300), allow_redirects=False,
                          headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}) as r:
            if r.status_code != 200:
                raise RuntimeError(f"Workable feed: HTTP {r.status_code}")
            for chunk in r.raw.stream(1 << 20, decode_content=False):
                raw.write(chunk)
            encoded = r.headers.get("Content-Encoding", "")
        raw.seek(0)
        stream = gzip.GzipFile(fileobj=raw) if "gzip" in encoded else raw
        yield from parse_workable_feed(stream, countries)


# ---------- USAJOBS ----------

USAJOBS_URL = "https://data.usajobs.gov/api/Search"


def parse_usajobs(data: dict) -> list[Job]:
    jobs = []
    for item in (data.get("SearchResult") or {}).get("SearchResultItems") or []:
        d = item.get("MatchedObjectDescriptor") or {}
        details = ((d.get("UserArea") or {}).get("Details") or {})
        duties = details.get("MajorDuties") or []
        desc = " ".join([d.get("QualificationSummary") or "", " ".join(duties) if isinstance(duties, list) else str(duties),
                         details.get("Education") or ""])
        pay = (d.get("PositionRemuneration") or [{}])[0]
        salary = ""
        if pay.get("MinimumRange"):
            salary = f"${float(pay['MinimumRange']):,.0f} – ${float(pay.get('MaximumRange') or pay['MinimumRange']):,.0f}"
        jobs.append(Job(
            source="usajobs", board=re.sub(r"[^a-z0-9]+", "-", (d.get("OrganizationName") or "federal").lower()).strip("-"),
            job_id=str(d.get("PositionID") or item.get("MatchedObjectId")), company=d.get("OrganizationName") or "",
            title=(d.get("PositionTitle") or "").strip(), url=d.get("PositionURI") or "",
            location=d.get("PositionLocationDisplay") or "; ".join(
                l.get("LocationName", "") for l in (d.get("PositionLocation") or [])[:3]),
            department=d.get("DepartmentName") or "", posted_at=d.get("PublicationStartDate") or "",
            salary=salary, description=re.sub(r"\s+", " ", desc).strip()[:8000],
            remote="remote" in (d.get("PositionLocationDisplay") or "").lower()))
    return jobs


def fetch_usajobs(key: str, email: str, days: int = int(os.environ.get("USAJOBS_DAYS", "30")), paths: str = "",
                  session=None) -> list[Job]:
    """Everything posted in the last few days (USAJOBS caps a query at 10,000 rows)."""
    s = session or requests.Session()
    headers = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    out, page = [], 1
    while page <= 20:
        params = {"DatePosted": days, "ResultsPerPage": 500, "Page": page, "Fields": "Full"}
        if paths:
            params["HiringPath"] = paths
        r = s.get(USAJOBS_URL, params=params, headers=headers, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"USAJOBS: HTTP {r.status_code}")
        batch = parse_usajobs(r.json())
        out += batch
        if len(batch) < 500:
            break
        page += 1
    return out


# ---------- run them all into the index ----------

def ingest_stream(index, source: str, jobs: Iterable[Job], close: bool = True) -> dict:
    """Group a stream's jobs by company and load them; close that source's postings that vanished."""
    by_board: dict[str, list[Job]] = {}
    for j in jobs:
        by_board.setdefault(j.board, []).append(j)
    new = 0
    for board, js in by_board.items():
        new += len(index.ingest_board(source, board, js))
    keep = {j.key for js in by_board.values() for j in js}
    closed = index.close_missing(source, keep) if (by_board and close) else 0
    return {"companies": len(by_board), "postings": sum(len(v) for v in by_board.values()), "new": new,
            "closed": closed}


def enabled_streams() -> dict[str, bool]:
    return {"workable": os.environ.get("STREAM_WORKABLE", "1") not in ("0", "false"),
            "hn": os.environ.get("STREAM_HN", "1") not in ("0", "false"),
            "usajobs": bool(os.environ.get("USAJOBS_KEY") and os.environ.get("USAJOBS_EMAIL"))
            and os.environ.get("STREAM_USAJOBS", "1") not in ("0", "false")}


def run_streams(index, fetch_hn_fn=fetch_hn, fetch_workable_fn=fetch_workable, fetch_usajobs_fn=fetch_usajobs,
                which: dict[str, bool] | None = None) -> dict:
    which = which if which is not None else enabled_streams()
    out: dict[str, dict] = {}
    countries = {c.strip().upper() for c in os.environ.get("STREAM_COUNTRIES", "US").split(",") if c.strip()} or None
    if which.get("hn"):
        try:
            jobs, boards = fetch_hn_fn()
            for ats, b in boards:
                index.add_board(ats, b, origin="discovered")
            out["hn"] = {**ingest_stream(index, "hn", jobs), "boards_found": len(boards)}
        except Exception as exc:  # a stream failing never stops the rest
            out["hn"] = {"error": str(exc)[:200]}
    if which.get("workable"):
        try:
            out["workable"] = ingest_stream(index, "workable", fetch_workable_fn(countries))
        except Exception as exc:
            out["workable"] = {"error": str(exc)[:200]}
    if which.get("usajobs"):
        try:
            out["usajobs"] = ingest_stream(index, "usajobs", fetch_usajobs_fn(
                os.environ["USAJOBS_KEY"], os.environ["USAJOBS_EMAIL"]), close=False)
        except Exception as exc:
            out["usajobs"] = {"error": str(exc)[:200]}
    from jobfeed.index import now_iso
    prev = index.get_meta("last_streams") or {}
    for name, res in out.items():
        res["at"] = now_iso()
        if "error" in res and prev.get(name, {}).get("postings") is not None:  # keep yesterday's counts visible
            res = {**prev[name], "error": res["error"], "at": res["at"]}
            out[name] = res
    index.set_meta("last_streams", {**prev, **out})
    log.info("streams: %s", out)
    return out
