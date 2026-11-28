"""Company careers sites on Workday, iCIMS, SAP SuccessFactors, Oracle Taleo and Oracle Recruiting Cloud.

None of these has a public job-board API, but every one of them publishes what search engines need: a sitemap
listing each job page, and job pages with schema.org JobPosting markup (that's how they appear in Google's job
search). This module reads exactly that, the same way a search engine does:

- robots.txt is honored (paths it disallows are never fetched; the sitemap it names is used),
- one request per second per site, an honest User-Agent, no proxies and no bot-check workarounds,
- job pages are fetched once (when first seen), then only the sitemap is re-read each day.

If a site answers with a bot check, the board is marked failing and retried in a few days; it's never forced.
"""
from __future__ import annotations

import gzip
import html
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse
from urllib.robotparser import RobotFileParser

import requests

from jobfeed.models import Job, html_to_text
from jobfeed.sources import USER_AGENT, BoardError

CAREER_ATS = ("workday", "icims", "successfactors", "taleo", "oracle")
DETAIL_BUDGET = 120          # job pages read per site per run (the rest next day; titles come from the sitemap)
MAX_SITEMAPS = 40
TIMEOUT = 25

_pages = {"left": int(__import__("os").environ.get("CAREER_PAGES_PER_RUN", "25000"))}  # job pages per crawl, all sites
_pages_lock = threading.Lock()


def reset_page_budget(n: int | None = None):
    import os
    with _pages_lock:
        _pages["left"] = n if n is not None else int(os.environ.get("CAREER_PAGES_PER_RUN", "25000"))


def _take_page() -> bool:
    with _pages_lock:
        if _pages["left"] <= 0:
            return False
        _pages["left"] -= 1
        return True


_robots: dict[str, RobotFileParser | None] = {}
_robots_lock = threading.Lock()
_host_next: dict[str, float] = {}
_host_lock = threading.Lock()


# Workday rate-limits by the caller across a whole cluster (wd1, wd5...), not per company site. So besides one
# request a second per site, each cluster has its own pace: it starts at CLUSTER_GAP seconds between requests,
# doubles (up to CLUSTER_GAP_MAX) every time Workday says "too many requests", waits out any Retry-After, and eases
# back toward the start pace after successes. Only after CLUSTER_STOP "slow down" replies in a row at the slowest
# pace does the cluster rest until the next run; its boards aren't marked as failing (they just stay due).
CLUSTER_GAP = 1.0
CLUSTER_GAP_MAX = 8.0
CLUSTER_STOP = 12
PAUSED = "paused until the next run"
_cluster_429: dict[str, int] = {}
_cluster_gap: dict[str, float] = {}


def _cluster(url: str) -> str:
    m = re.search(r"\.(wd\d+)\.myworkdayjobs\.com$", urlparse(url).netloc.lower())
    return f"workday-{m.group(1)}" if m else ""


def reset_run_limits():
    with _host_lock:
        _cluster_429.clear()
        _cluster_gap.clear()


def _polite(url: str, gap: float = 1.0, key: str = ""):
    host = key or urlparse(url).netloc
    with _host_lock:
        now = time.monotonic()
        at = max(now, _host_next.get(host, 0.0))
        _host_next[host] = at + gap
    if at > now:
        time.sleep(at - now)


def _get(session, url: str, accept: str = "*/*") -> requests.Response:
    cluster = _cluster(url)
    if cluster:
        if _cluster_429.get(cluster, 0) >= CLUSTER_STOP:
            raise BoardError(f"HTTP 429: Workday asked us to slow down; {PAUSED}")
        _polite(url, _cluster_gap.get(cluster, CLUSTER_GAP), key=cluster)
    _polite(url)
    try:
        r = (session or requests).get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": accept})
    except requests.RequestException as exc:
        raise BoardError(f"request failed: {exc}") from exc
    if cluster:
        wait = 0.0
        with _host_lock:
            gap = _cluster_gap.get(cluster, CLUSTER_GAP)
            if r.status_code == 429:
                _cluster_gap[cluster] = min(gap * 2, CLUSTER_GAP_MAX)
                if gap >= CLUSTER_GAP_MAX:  # only count refusals once we're already at the slowest pace
                    _cluster_429[cluster] = _cluster_429.get(cluster, 0) + 1
                try:
                    wait = min(float(r.headers.get("Retry-After") or 0), 60.0)
                except ValueError:
                    wait = 0.0
                _host_next[cluster] = max(_host_next.get(cluster, 0.0), time.monotonic() + wait)
            else:
                _cluster_429[cluster] = 0
                _cluster_gap[cluster] = max(CLUSTER_GAP, gap * 0.95)
    if r.status_code == 404:
        raise BoardError("board not found (404)")
    if r.status_code in (403, 429, 503) and ("cf-mitigated" in {k.lower() for k in r.headers}
                                            or "captcha" in r.text[:3000].lower() or "challenge" in r.text[:3000].lower()):
        raise BoardError(f"HTTP {r.status_code}: the site asked for a bot check; not forcing it")
    if r.status_code != 200:
        raise BoardError(f"HTTP {r.status_code}")
    return r


def robots_for(session, base: str) -> RobotFileParser | None:
    root = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
    with _robots_lock:
        if root in _robots:
            return _robots[root]
    rp = RobotFileParser()
    try:
        r = _get(session, root + "/robots.txt", "text/plain")
        rp.parse(r.text.splitlines())
    except BoardError:
        rp = None  # no robots.txt: everything public is allowed
    with _robots_lock:
        _robots[root] = rp
    return rp


def allowed(session, url: str) -> bool:
    rp = robots_for(session, url)
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


# ---------- sitemaps ----------

_NS = re.compile(r"\{[^}]+\}")


def parse_sitemap(content: bytes) -> tuple[list[tuple[str, str]], list[str]]:
    """(urls with lastmod, child sitemaps) from a sitemap or sitemap index (plain or gzipped)."""
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise BoardError("sitemap isn't valid XML") from exc
    urls, children = [], []
    for el in root:
        tag = _NS.sub("", el.tag)
        loc = lastmod = ""
        for ch in el:
            t = _NS.sub("", ch.tag)
            if t == "loc":
                loc = (ch.text or "").strip()
            elif t == "lastmod":
                lastmod = (ch.text or "").strip()
        if not loc:
            continue
        (children if tag == "sitemap" else urls).append((loc, lastmod) if tag != "sitemap" else loc)
    return urls, children


def read_sitemaps(session, start: list[str], keep=lambda u: True) -> list[tuple[str, str]]:
    seen, queue, out = set(), list(start), {}
    while queue and len(seen) < MAX_SITEMAPS:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            urls, kids = parse_sitemap(_get(session, sm, "application/xml").content)
        except BoardError:
            if len(seen) == 1 and not queue:
                raise
            continue
        queue += [k for k in kids if k not in seen and ("job" in k.lower() or "posting" in k.lower() or len(kids) < 6)]
        for u, lm in urls:
            if keep(u):
                out[u] = lm
    return list(out.items())


# ---------- job pages ----------

_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
_META = re.compile(r"<meta\s+[^>]*?(?:property|name|itemprop)=[\"']([^\"']+)[\"'][^>]*?content=[\"']([^\"']*)[\"']",
                   re.I)
_META_REV = re.compile(r"<meta\s+[^>]*?content=[\"']([^\"']*)[\"'][^>]*?(?:property|name|itemprop)=[\"']([^\"']+)[\"']",
                       re.I)
_ITEMPROP = re.compile(r"itemprop=[\"'](title|datePosted|addressLocality|addressRegion|addressCountry|description|"
                       r"employmentType|name)[\"'][^>]*>(.*?)</", re.S | re.I)


def _jobposting(obj):
    if isinstance(obj, list):
        for o in obj:
            r = _jobposting(o)
            if r:
                return r
    elif isinstance(obj, dict):
        t = obj.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            return obj
        if "@graph" in obj:
            return _jobposting(obj["@graph"])
    return None


def _text(v) -> str:
    """Markup values come as text, {"name": ...} objects or lists of either; always give back plain text."""
    if isinstance(v, list):
        v = next((x for x in v if x), "")
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value") or ""
    return v.strip() if isinstance(v, str) else ""


def _place(jp) -> str:
    locs = jp.get("jobLocation") or []
    out = []
    for loc in (locs if isinstance(locs, list) else [locs])[:3]:
        if not isinstance(loc, dict):
            continue
        a = loc.get("address") or {}
        if isinstance(a, list):
            a = next((x for x in a if isinstance(x, dict)), {})
        if isinstance(a, str):
            out.append(a)
            continue
        if not isinstance(a, dict):
            continue
        country = _text(a.get("addressCountry"))
        part = ", ".join(x for x in (_text(a.get("addressLocality")), _text(a.get("addressRegion")), country) if x)
        if part and part not in out:
            out.append(part)
    s = "; ".join(out)
    if jp.get("jobLocationType") == "TELECOMMUTE":
        s = (s + " (Remote)").strip() if s else "Remote"
    return s


def parse_job_page(page: str) -> dict:
    """title, company, location, posted, description, remote from JSON-LD, microdata, or meta tags."""
    for block in _LD.findall(page or ""):
        try:
            jp = _jobposting(json.loads(html.unescape(block.strip()) if block.strip().startswith("&") else block))
        except ValueError:
            continue
        if jp and jp.get("title"):
            org = jp.get("hiringOrganization") or {}
            salary = ""
            bs = jp.get("baseSalary") or {}
            v = bs.get("value") if isinstance(bs, dict) else None
            if isinstance(v, dict) and (v.get("minValue") or v.get("value")):
                lo, hi = v.get("minValue") or v.get("value"), v.get("maxValue")
                unit = (v.get("unitText") or "").lower()
                try:
                    salary = f"${float(lo):,.0f}" + (f" – ${float(hi):,.0f}" if hi else "") + (f" / {unit}" if unit and unit != "year" else "")
                except (TypeError, ValueError):
                    salary = ""
            return {"title": html.unescape(str(jp["title"])).strip(),
                    "company": (org.get("name") if isinstance(org, dict) else str(org)) or "",
                    "location": _place(jp), "posted": jp.get("datePosted") or "",
                    "description": html_to_text(jp.get("description") or ""),
                    "remote": jp.get("jobLocationType") == "TELECOMMUTE", "salary": salary,
                    "type": jp.get("employmentType") or ""}
    meta = {k.lower(): html.unescape(v) for k, v in _META.findall(page or "")}
    meta.update({k.lower(): html.unescape(v) for v, k in _META_REV.findall(page or "") if k.lower() not in meta})
    micro = {}
    for k, v in _ITEMPROP.findall(page or ""):
        micro.setdefault(k.lower(), html_to_text(v))
    title = micro.get("title") or meta.get("og:title") or meta.get("title") or ""
    loc = ", ".join(x for x in (micro.get("addresslocality"), micro.get("addressregion"), micro.get("addresscountry"))
                    if x)
    return {"title": title.split(" | ")[0].strip(), "company": meta.get("og:site_name", ""), "location": loc,
            "posted": micro.get("dateposted") or meta.get("article:published_time", "") or meta.get("dateposted", ""),
            "description": micro.get("description") or meta.get("og:description") or meta.get("description", ""),
            "remote": False, "salary": "", "type": micro.get("employmenttype", "")}


# ---------- platforms ----------

def _unslug(s: str) -> str:
    s = unquote(s)
    s = s.replace("---", "\x00").replace("--", "\x01").replace("-", " ").replace("_", " ")
    s = s.replace("\x00", " - ").replace("\x01", ", ")
    return re.sub(r"\s+", " ", s).strip()


def workday_parts(board: str) -> tuple[str, str]:
    tenant, wd, site = (board.split("|") + ["", ""])[:3]
    return f"https://{tenant}.{wd}.myworkdayjobs.com/{site}", tenant


def workday_from_url(url: str) -> dict | None:
    # https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Senior-Solutions-Architect--NVIDIA-Cloud-Partners_JR2028857
    m = re.search(r"/job/([^/]+)/([^/?#]+?)_([A-Za-z0-9-]+)(?:[/?#]|$)", url)
    if not m:
        return None
    loc = m.group(1).split("-")
    if len(loc) >= 3 and len(loc[0]) == 2 and len(loc[1]) == 2 and loc[0].isupper():
        place = f"{' '.join(loc[2:])}, {loc[1]}, {loc[0]}"
    else:
        place = _unslug(m.group(1))
    return {"id": m.group(3), "title": _unslug(m.group(2)), "location": place}


def icims_from_url(url: str) -> dict | None:
    # https://careers-patternenergy.icims.com/jobs/3506/lead-accountant-%28operators-%26-operations%29/job
    m = re.search(r"/jobs/(\d+)/([^/]+)/job", url)
    return {"id": m.group(1), "title": _unslug(m.group(2)).title(), "location": ""} if m else None


def generic_from_url(url: str) -> dict | None:
    # SuccessFactors: /job/<City-Title>/<id>/ ; Taleo: jobdetail.ftl?job=<id> ; Oracle: /job/<id> or /requisitions/preview/<id>
    for rx in (r"/job/([^/]+)/(\d{4,})/?", r"/jobs/(\d{6,})/([^/]+)/?", r"jobdetail\.ftl\?job=([A-Za-z0-9-]+)",
               r"/requisitions/preview/(\d+)", r"/job/(\d+)(?:[/?]|$)"):
        m = re.search(rx, url)
        if m:
            g = m.groups()
            if len(g) == 2:
                ident, slug = (g[1], g[0]) if g[1].isdigit() else (g[0], g[1])
                return {"id": ident, "title": _unslug(slug), "location": ""}
            return {"id": g[0], "title": "", "location": ""}
    return None


def sitemap_starts(ats: str, board: str) -> tuple[str, list[str]]:
    """(site base URL, sitemap URLs to try) for a board."""
    if ats == "workday":
        base, _ = workday_parts(board)
        return base, [base + "/siteMap.xml"]
    if ats == "icims":
        base = f"https://careers-{board}.icims.com" if "." not in board else f"https://{board}"
        return base, [base + "/sitemap.xml"]
    base = board if board.startswith("http") else f"https://{board}"
    base = base.rstrip("/")
    return base, [base + "/sitemap.xml", base + "/sitemap_index.xml"]


_JOBISH = re.compile(r"/job/|/jobs/\d|jobdetail\.ftl|/requisitions/preview/|/job-detail/", re.I)
_JOBISH_ANY = re.compile(r"/(?:jobs?|careers?|positions?|openings?|vacanc(?:y|ies)|requisitions?|job-detail)/[^?#]*"
                         r"(?:\d|[a-z]-[a-z])", re.I)


def fetch_careersite(ats: str, board: str, company: str = "", session=None, existing: dict | None = None,
                     budget: int = DETAIL_BUDGET) -> list[Job]:
    """All open jobs on one careers site. `existing` maps job URL -> index row (so known pages aren't re-read)."""
    s = session or requests.Session()
    base, starts = sitemap_starts(ats, board)
    rp = robots_for(s, base)
    if rp is not None:
        declared = [u for u in (rp.site_maps() or []) if urlparse(u).netloc == urlparse(base).netloc]
        if ats == "workday":  # one tenant can host several sites; keep this site's sitemap
            declared = [u for u in declared if urlparse(u).path.lower().startswith(urlparse(base).path.lower())]
        starts = declared + [u for u in starts if u not in declared]
    if not allowed(s, starts[0]):
        raise BoardError("robots.txt doesn't allow reading this site's sitemap")
    rx = _JOBISH if ats in ("workday", "icims", "successfactors", "taleo", "oracle") else _JOBISH_ANY
    entries = read_sitemaps(s, starts, keep=lambda u: bool(rx.search(u)) and not u.endswith("/jobs/intro"))
    parse_url = {"workday": workday_from_url, "icims": icims_from_url}.get(ats, generic_from_url)
    existing = existing or {}
    name = company or _company_from_board(ats, board)
    jobs, fetched = [], 0
    # read US job pages first (location is in Workday URLs); the rest come from their URLs until a later run
    from jobfeed.levels import is_us
    entries.sort(key=lambda e: 0 if is_us((parse_url(e[0]) or {}).get("location", "")) else 1)
    for url, lastmod in entries:
        info = parse_url(url) or {"id": url.rstrip("/").rsplit("/", 1)[-1][:80], "title": "", "location": ""}
        old = existing.get(url)
        detail = {}
        need = old is None or not old["has_desc"]
        if need and fetched < budget and allowed(s, url) and _take_page():
            page_url = url + ("?in_iframe=1" if ats == "icims" else "")
            try:
                detail = parse_job_page(_get(s, page_url, "text/html").text)
                fetched += 1
            except BoardError as exc:
                if "bot check" in str(exc):
                    budget = fetched  # stop reading pages from this site today
        title = detail.get("title") or info["title"] or (old["title"] if old else "")
        if not title or (ats == "jsonld" and not old and not detail.get("description")):
            continue  # an arbitrary site: only pages with real job markup count
        jobs.append(Job(
            source=ats, board=board, job_id=str(info["id"]), company=(detail.get("company") or name).strip()[:120],
            title=title[:200], url=url, location=(detail.get("location") or info["location"]
                                                  or (old["location"] if old else ""))[:200],
            remote=bool(detail.get("remote")) or "remote" in (detail.get("location") or info["location"]).lower(),
            posted_at=detail.get("posted") or lastmod or "", salary=detail.get("salary", ""),
            description=detail.get("description", ""), extra={"keep_desc": old is not None and not detail}))
    return jobs


def _company_from_board(ats: str, board: str) -> str:
    if ats == "workday":
        return board.split("|")[0]
    if ats == "icims":
        return re.sub(r"^careers-", "", board)
    host = urlparse(board if board.startswith("http") else "https://" + board).netloc
    parts = [p for p in host.split(".") if p not in ("www", "careers", "jobs", "career", "com", "net", "org", "io",
                                                      "taleo", "jobs2web", "successfactors", "oraclecloud")]
    return parts[0] if parts else host


# Workday URLs already carry the title, place and job id, so the daily crawl reads only robots.txt and the sitemap
# (two or three requests per company instead of a hundred); describe.py fetches a job page later, only for roles
# that land in someone's feed. That is what lets every Workday company fit in one polite run.
SITEMAP_ONLY = {"workday"}


def make_fetcher(ats: str):
    def fetch(board: str, company: str = "", session=None, existing=None):
        return fetch_careersite(ats, board, company, session=session, existing=existing,
                                budget=0 if ats in SITEMAP_ONLY else DETAIL_BUDGET)
    return fetch


CAREER_FETCHERS = {ats: make_fetcher(ats) for ats in CAREER_ATS}


def _register():
    from jobfeed import sources
    sources.FETCHERS.update(CAREER_FETCHERS)


_register()


# ---------- recognizing a careers link someone pastes ----------

def board_from_link(link: str) -> tuple[str, str] | None:
    """(ats, board) for a careers-site URL, or None."""
    u = urlparse(link.strip() if "://" in link else "https://" + link.strip())
    host, path = u.netloc.lower(), u.path
    m = re.match(r"(wd\d+)\.myworkdaysite\.com", host)  # wd5.myworkdaysite.com/recruiting/<tenant>/<site>/...
    if m:
        segs = [p for p in path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
        if len(segs) >= 3 and segs[0] == "recruiting":
            return "workday", f"{segs[1].lower()}|{m.group(1)}|{segs[2]}"
    m = re.match(r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com", host)
    if m:
        segs = [p for p in path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
        if segs:
            return "workday", f"{m.group(1)}|{m.group(2)}|{segs[0]}"
    m = re.match(r"careers-([a-z0-9-]+)\.icims\.com", host)
    if m:
        return "icims", m.group(1)
    if host.endswith(".icims.com"):
        return "icims", host
    if host.endswith(".taleo.net"):
        return "taleo", f"https://{host}"
    if "oraclecloud.com" in host and "CandidateExperience" in path:
        site = re.search(r"(/hcmUI/CandidateExperience/[a-z]{2}/sites/[^/]+)", path)
        return "oracle", f"https://{host}" + (site.group(1) if site else "")
    if "successfactors" in host or "jobs2web" in host or re.search(r"/job/[^/]+/\d{6,}/?", path):
        return "successfactors", f"https://{host}"
    return None
