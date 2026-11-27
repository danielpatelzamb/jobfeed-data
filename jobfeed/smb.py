"""Small-business hiring systems and payroll companies' career sites.

Each reader uses the public feed or page the system publishes for job seekers and embeddable job widgets:

- BambooHR    https://{co}.bamboohr.com/careers/list                      (JSON the careers page loads)
- Paylocity   https://recruiting.paylocity.com/recruiting/jobs/All/{id}/ (job list in the public page)
- Recruitee   https://{co}.recruitee.com/api/offers/                      (documented Careers Site API)
- Personio    https://{co}.jobs.personio.de/xml                           (documented XML job feed)
- Breezy      https://{co}.breezy.hr/json                                 (public widget JSON)
- Teamtailor  https://{site}/jobs.rss                                     (public RSS feed)
- JazzHR, Jobvite, Rippling, ADP, UKG, Paycom, Dayforce, and any other site with job markup: the
  sitemap + schema.org JobPosting reader in careers.py, with a fallback to the site's job-list page.

Same rules as careers.py: robots.txt honored, one request a second per site, no bot-check workarounds.
"""
from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

from jobfeed import careers
from jobfeed.models import Job, html_to_text
from jobfeed.sources import BoardError


def _json(session, url):
    if not careers.allowed(session, url):
        raise BoardError("robots.txt doesn't allow this feed")
    r = careers._get(session, url, "application/json")
    try:
        return r.json()
    except ValueError as exc:
        raise BoardError("response was not JSON") from exc


def _xml(session, url):
    if not careers.allowed(session, url):
        raise BoardError("robots.txt doesn't allow this feed")
    try:
        return ET.fromstring(careers._get(session, url, "application/xml").content)
    except ET.ParseError as exc:
        raise BoardError("feed isn't valid XML") from exc


def _rfc(s):
    try:
        return parsedate_to_datetime(s).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


def _place(*parts) -> str:
    out = []
    for p in parts:
        p = (p or "").strip() if isinstance(p, str) else ""
        if p and p not in out:
            out.append(p)
    return ", ".join(out)


# ---------- BambooHR ----------

def parse_bamboohr(data: dict, board: str, company: str) -> list[Job]:
    jobs = []
    for j in data.get("result") or []:
        loc = j.get("location") or {}
        place = _place(loc.get("city"), loc.get("state")) if isinstance(loc, dict) else str(loc or "")
        if j.get("isRemote") or (j.get("locationType") in ("1", 1, "remote")):
            place = (place + " (Remote)").strip() if place else "Remote"
        jobs.append(Job(source="bamboohr", board=board, job_id=str(j.get("id")), company=company or board,
                        title=(j.get("jobOpeningName") or "").strip(),
                        url=f"https://{board}.bamboohr.com/careers/{j.get('id')}", location=place,
                        department=j.get("departmentLabel") or "", remote="remote" in place.lower(),
                        extra={"type": j.get("employmentStatusLabel") or ""}))
    return jobs


def fetch_bamboohr(board, company="", session=None, existing=None):
    data = _json(session, f"https://{board}.bamboohr.com/careers/list")
    return parse_bamboohr(data, board, company or careers._company_from_board("bamboohr", board))


# ---------- Paylocity ----------

_PAGEDATA = re.compile(r"window\.pageData\s*=\s*(\{.*?\});\s*</script>", re.S)


def parse_paylocity(page: str, board: str, company: str) -> list[Job]:
    m = _PAGEDATA.search(page or "")
    if not m:
        raise BoardError("job list not found on the page")
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        raise BoardError("job list wasn't readable") from exc
    jobs = []
    for j in data.get("Jobs") or []:
        loc = j.get("JobLocation") or {}
        place = html.unescape(_place(loc.get("City"), loc.get("State")) or j.get("LocationName") or "")
        jid = j.get("JobId")
        jobs.append(Job(source="paylocity", board=board, job_id=str(jid), company=company or board,
                        title=html.unescape(j.get("JobTitle") or "").strip(),
                        url=f"https://recruiting.paylocity.com/recruiting/Jobs/Details/{jid}", location=place,
                        department=html.unescape(j.get("HiringDepartment") or ""), remote=bool(j.get("IsRemote")),
                        posted_at=j.get("PublishedDate") or ""))
    return jobs


PAYLOCITY_NAMES: dict[str, str] = {}


def fetch_paylocity(board, company="", session=None, existing=None):
    url = f"https://recruiting.paylocity.com/recruiting/jobs/All/{board}/"
    if not careers.allowed(session, url):
        raise BoardError("robots.txt doesn't allow this page")
    page = careers._get(session, url, "text/html").text
    return parse_paylocity(page, board, company or PAYLOCITY_NAMES.get(board, ""))


# ---------- Recruitee ----------

def parse_recruitee(data: dict, board: str, company: str) -> list[Job]:
    jobs = []
    for o in data.get("offers") or []:
        place = o.get("location") or _place(o.get("city"), o.get("state_name") or o.get("state_code"),
                                            o.get("country") or o.get("country_code"))
        remote = bool(o.get("remote"))
        desc = html_to_text((o.get("description") or "") + " " + (o.get("requirements") or ""))
        jobs.append(Job(source="recruitee", board=board, job_id=str(o.get("id") or o.get("slug")),
                        company=o.get("company_name") or company or board, title=(o.get("title") or "").strip(),
                        url=o.get("careers_url") or f"https://{board}.recruitee.com/o/{o.get('slug')}",
                        location=place + (" (Remote)" if remote and "remote" not in place.lower() else ""),
                        department=o.get("department") or "", remote=remote,
                        posted_at=o.get("published_at") or o.get("created_at") or "", description=desc))
    return jobs


def fetch_recruitee(board, company="", session=None, existing=None):
    return parse_recruitee(_json(session, f"https://{board}.recruitee.com/api/offers/"), board, company)


# ---------- Personio ----------

def parse_personio(root, board: str, company: str, host: str) -> list[Job]:
    jobs = []
    for p in root.iter("position"):
        f = {c.tag: (c.text or "").strip() for c in p}
        descs = " ".join(html_to_text(v.text or "") for v in p.iter("value"))
        pid = f.get("id", "")
        jobs.append(Job(source="personio", board=board, job_id=pid, company=company or board,
                        title=f.get("name", ""), url=f"https://{host}/job/{pid}", location=f.get("office", ""),
                        department=f.get("department", ""), posted_at=f.get("createdAt", ""), description=descs,
                        extra={"seniority": f.get("seniority", ""), "type": f.get("employmentType", "")}))
    return jobs


def fetch_personio(board, company="", session=None, existing=None):
    last = None
    for host in (f"{board}.jobs.personio.de", f"{board}.jobs.personio.com"):
        try:
            return parse_personio(_xml(session, f"https://{host}/xml"), board,
                                  company or careers._company_from_board("personio", board), host)
        except BoardError as exc:
            last = exc
    raise last or BoardError("no Personio feed")


# ---------- Breezy ----------

def parse_breezy(items: list, board: str, company: str) -> list[Job]:
    jobs = []
    for j in items or []:
        loc = j.get("location") or {}
        remote = bool(loc.get("is_remote"))
        place = loc.get("name") or _place(loc.get("city"), (loc.get("state") or {}).get("id"),
                                          (loc.get("country") or {}).get("id"))
        jobs.append(Job(source="breezy", board=board, job_id=str(j.get("id")), company=company or board,
                        title=(j.get("name") or "").strip(), url=j.get("url") or "",
                        location=place + (" (Remote)" if remote and "remote" not in place.lower() else ""),
                        department=j.get("department") or "", remote=remote,
                        posted_at=j.get("published_date") or "", salary=j.get("salary") or "",
                        extra={"type": (j.get("type") or {}).get("name", "")}))
    return jobs


def fetch_breezy(board, company="", session=None, existing=None):
    data = _json(session, f"https://{board}.breezy.hr/json")
    if not isinstance(data, list):
        raise BoardError("unexpected Breezy response")
    return parse_breezy(data, board, company or careers._company_from_board("breezy", board))


# ---------- Teamtailor ----------

_TT = "{https://teamtailor.com/locations}"


def parse_teamtailor(root, board: str, company: str) -> list[Job]:
    jobs = []
    for it in root.iter("item"):
        def t(tag):
            el = it.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        cities = []
        for el in it.iter():
            if el.tag.endswith("}city") or el.tag.endswith("}country"):
                if el.text and el.text.strip() and el.text.strip() not in cities:
                    cities.append(el.text.strip())
        dept = next((el.text for el in it.iter() if el.tag.endswith("}department") and el.text), "")
        remote = t("remoteStatus").lower() in ("fully", "remote", "fully_remote")
        link = t("link")
        jid = (re.search(r"/jobs/(\d+)", link) or [None, t("guid")])[1]
        jobs.append(Job(source="teamtailor", board=board, job_id=str(jid), company=company or board,
                        title=t("title"), url=link, location=", ".join(cities[:3]), department=dept or "",
                        remote=remote, posted_at=_rfc(t("pubDate")), description=html_to_text(t("description"))))
    return jobs


def fetch_teamtailor(board, company="", session=None, existing=None):
    host = board if "." in board else f"{board}.teamtailor.com"
    return parse_teamtailor(_xml(session, f"https://{host}/jobs.rss"), board,
                            company or careers._company_from_board("teamtailor", host))


# ---------- everything else: sitemap + job markup, then the job-list page ----------

_LINK = re.compile(r"href=[\"']([^\"'#]+)[\"']", re.I)
_JOB_LINK = re.compile(r"/(?:jobs?|careers?|positions?|openings?|vacanc(?:y|ies)|apply|requisitions?)/[^/?#]*\d", re.I)


def fetch_any(ats: str):
    """JazzHR, Jobvite, Rippling, ADP, UKG, Paycom, Dayforce and any site with job markup."""
    def fetch(board, company="", session=None, existing=None):
        try:
            jobs = careers.fetch_careersite(ats, board, company, session=session, existing=existing)
            if jobs:
                return jobs
        except BoardError as exc:
            if "robots" in str(exc) or "bot check" in str(exc):
                raise
        return listing_page(ats, board, company, session, existing)
    return fetch


def listing_page(ats, board, company, session, existing=None, budget=60) -> list[Job]:
    """No usable sitemap: read the site's job-list page, follow job links, keep pages with job markup."""
    base = board if board.startswith("http") else f"https://{board}"
    if not careers.allowed(session, base):
        raise BoardError("robots.txt doesn't allow this site")
    page = careers._get(session, base, "text/html").text
    links = []
    for href in _LINK.findall(page):
        u = urljoin(base + "/", html.unescape(href))
        if urlparse(u).netloc == urlparse(base).netloc and _JOB_LINK.search(urlparse(u).path) and u not in links:
            links.append(u)
    existing = existing or {}
    jobs = []
    for u in links[:300]:
        old = existing.get(u)
        if old is not None and old["has_desc"]:
            jobs.append(Job(source=ats, board=board, job_id=u.rstrip("/").rsplit("/", 1)[-1][:80],
                            company=company or careers._company_from_board(ats, board), title=old["title"], url=u,
                            location=old["location"], extra={"keep_desc": True}))
            continue
        if budget <= 0 or not careers.allowed(session, u) or not careers._take_page():
            continue
        budget -= 1
        try:
            d = careers.parse_job_page(careers._get(session, u, "text/html").text)
        except BoardError:
            continue
        if not d.get("title") or not d.get("description"):
            continue  # not a job page (or no markup): skip rather than guess
        jobs.append(Job(source=ats, board=board, job_id=u.rstrip("/").rsplit("/", 1)[-1][:80],
                        company=(d.get("company") or company or careers._company_from_board(ats, board))[:120],
                        title=d["title"][:200], url=u, location=d.get("location", ""), remote=d.get("remote", False),
                        posted_at=d.get("posted", ""), salary=d.get("salary", ""), description=d["description"]))
    if not jobs and not links:
        raise BoardError("no job listings found on this site")
    return jobs


SMB_ATS = ("bamboohr", "paylocity", "recruitee", "personio", "breezy", "teamtailor")
ANY_ATS = ("jazzhr", "jobvite", "rippling", "adp", "ukg", "paycom", "dayforce", "jsonld")
SMB_FETCHERS = {"bamboohr": fetch_bamboohr, "paylocity": fetch_paylocity, "recruitee": fetch_recruitee,
                "personio": fetch_personio, "breezy": fetch_breezy, "teamtailor": fetch_teamtailor,
                **{a: fetch_any(a) for a in ANY_ATS}}


def board_from_link(link: str) -> tuple[str, str] | None:
    u = urlparse(link.strip() if "://" in link else "https://" + link.strip())
    host, path = u.netloc.lower(), u.path
    for ats, rx in (("bamboohr", r"^([a-z0-9-]+)\.bamboohr\.com$"), ("recruitee", r"^([a-z0-9-]+)\.recruitee\.com$"),
                    ("personio", r"^([a-z0-9-]+)\.jobs\.personio\.(?:de|com)$"), ("breezy", r"^([a-z0-9-]+)\.breezy\.hr$")):
        m = re.match(rx, host)
        if m:
            return ats, m.group(1)
    m = re.search(r"/recruiting/jobs/(?:All|Details)/([0-9a-f-]{36})", path, re.I)
    if host == "recruiting.paylocity.com" and m:
        return "paylocity", m.group(1)
    if host.endswith(".teamtailor.com"):
        return "teamtailor", host
    if host.endswith(".applytojob.com"):
        return "jazzhr", f"https://{host}"
    if host == "jobs.jobvite.com":
        seg = [p for p in path.split("/") if p]
        return ("jobvite", f"https://jobs.jobvite.com/{seg[0]}") if seg else None
    if host == "ats.rippling.com":
        seg = [p for p in path.split("/") if p]
        return ("rippling", f"https://ats.rippling.com/{seg[0]}") if seg else None
    if "adp.com" in host and ("careercenter" in path or "mascsr" in path or "recruitment" in path):
        return "adp", link.split("#")[0]
    if host.endswith("ultipro.com") or host.endswith("ukg.net"):
        return "ukg", link.split("#")[0].split("?")[0]
    if "paycomonline" in host:
        return "paycom", link.split("#")[0]
    if "dayforcehcm.com" in host:
        return "dayforce", link.split("#")[0].split("?")[0]
    return None


def _register():
    from jobfeed import sources
    sources.FETCHERS.update(SMB_FETCHERS)


_register()
