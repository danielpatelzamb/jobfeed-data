"""Fetchers and parsers for public job-board APIs.

Each board type has a pure `parse_*` function (dict -> list[Job]) and a thin `fetch_*`
wrapper that does the HTTP call. Only documented, unauthenticated GET endpoints are used.
"""
from __future__ import annotations

import time
from typing import Callable

import requests

from jobfeed.models import Job, html_to_text

USER_AGENT = "jobfeed/0.1 (personal job-search tool)"
TIMEOUT = 20

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{board}?mode=json"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"

_last_lever_call = 0.0


class BoardError(Exception):
    """A board could not be fetched or parsed."""


def _get_json(url: str, session: requests.Session | None = None):
    """One polite retry for a busy server (429/5xx, honoring Retry-After up to 30s) or a dropped connection."""
    import time as _time
    s = session or requests
    resp = None
    for attempt in (1, 2):
        try:
            resp = s.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            if attempt == 2:
                raise BoardError(f"request failed: {type(exc).__name__}") from exc
            _time.sleep(3)
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt == 1:
            try:
                wait = min(30.0, float(resp.headers.get("Retry-After", "5")))
            except ValueError:
                wait = 5.0
            _time.sleep(wait)
            continue
        break
    if resp.status_code == 404:
        raise BoardError("board not found (404) - check the board name")
    if resp.status_code != 200:
        raise BoardError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise BoardError("response was not JSON") from exc


# ---------- Greenhouse ----------

def parse_greenhouse(data: dict, board: str, company: str) -> list[Job]:
    jobs = []
    for item in data.get("jobs", []):
        location = (item.get("location") or {}).get("name", "") or ""
        departments = item.get("departments") or []
        jobs.append(Job(
            source="greenhouse",
            board=board,
            job_id=str(item["id"]),
            company=company or item.get("company_name") or board,
            title=(item.get("title") or "").strip(),
            url=item.get("absolute_url", ""),
            location=location,
            department=departments[0].get("name", "") if departments else "",
            remote="remote" in location.lower(),
            posted_at=item.get("first_published") or item.get("updated_at") or "",
            description=html_to_text(item.get("content")),
        ))
    return jobs


def fetch_greenhouse(board: str, company: str = "", session=None) -> list[Job]:
    return parse_greenhouse(_get_json(GREENHOUSE_URL.format(board=board), session), board, company)


# ---------- Lever ----------

_INTERVALS = {"per-year-salary": "", "per-month-salary": "/mo", "per-week-salary": "/wk",
              "per-day-wage": "/day", "per-hour-wage": "/hr", "one-time": " one-time"}


def _money(n: float, cur: str) -> str:
    sign = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "C$", "AUD": "A$", "SGD": "S$"}.get(cur, "")
    amount = f"{n / 1000:.0f}K" if n >= 10000 else f"{n:,.0f}"
    return f"{sign}{amount}" if sign else f"{amount} {cur}".strip()


def _lever_salary(rng: dict | None) -> str:
    if not rng or rng.get("min") is None:
        return ""
    cur = rng.get("currency", "")
    lo, hi = rng["min"], rng.get("max") or rng["min"]
    suffix = _INTERVALS.get(rng.get("interval", ""), "")
    span = _money(lo, cur) if lo == hi else f"{_money(lo, cur)} – {_money(hi, cur)}"
    return span + suffix


def parse_lever(data: list, board: str, company: str) -> list[Job]:
    jobs = []
    for item in data:
        cats = item.get("categories") or {}
        location = cats.get("location", "") or ""
        workplace = (item.get("workplaceType") or "").lower()
        created = item.get("createdAt")
        posted = ""
        if isinstance(created, (int, float)):
            posted = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created / 1000))
        jobs.append(Job(
            source="lever",
            board=board,
            job_id=str(item["id"]),
            company=company or board,
            title=(item.get("text") or "").strip(),
            url=item.get("hostedUrl", ""),
            location=location,
            department=cats.get("team", "") or cats.get("department", "") or "",
            remote=workplace == "remote" or "remote" in location.lower(),
            posted_at=posted,
            salary=_lever_salary(item.get("salaryRange")),
            description=item.get("descriptionPlain") or html_to_text(item.get("description")),
            extra={"commitment": cats.get("commitment", ""), "country": item.get("country")},
        ))
    return jobs


def fetch_lever(board: str, company: str = "", session=None) -> list[Job]:
    global _last_lever_call
    wait = 1.0 - (time.monotonic() - _last_lever_call)
    if wait > 0:
        time.sleep(wait)  # Lever's robots.txt asks for a 1 s crawl delay
    _last_lever_call = time.monotonic()
    data = _get_json(LEVER_URL.format(board=board), session)
    if not isinstance(data, list):
        raise BoardError("unexpected Lever response")
    return parse_lever(data, board, company)


# ---------- Ashby ----------

def parse_ashby(data: dict, board: str, company: str) -> list[Job]:
    jobs = []
    for item in data.get("jobs", []):
        if item.get("isListed") is False:
            continue
        comp = item.get("compensation") or {}
        job_id = item.get("id") or item.get("jobUrl", "").rstrip("/").rsplit("/", 1)[-1]
        jobs.append(Job(
            source="ashby",
            board=board,
            job_id=str(job_id),
            company=company or board,
            title=(item.get("title") or "").strip(),
            url=item.get("jobUrl", ""),
            location=item.get("location", "") or "",
            department=item.get("department", "") or "",
            remote=bool(item.get("isRemote")) or (item.get("workplaceType") == "Remote"),
            posted_at=item.get("publishedAt", "") or "",
            salary=comp.get("scrapeableCompensationSalarySummary", "") or "",
            description=item.get("descriptionPlain") or html_to_text(item.get("descriptionHtml")),
            extra={"employment_type": item.get("employmentType", "")},
        ))
    return jobs


def fetch_ashby(board: str, company: str = "", session=None) -> list[Job]:
    return parse_ashby(_get_json(ASHBY_URL.format(board=board), session), board, company)


# SmartRecruiters' Posting API: documented, no key needed for public postings (only internal ones need a token).
SMARTRECRUITERS_URL = "https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100&offset={offset}"
SMARTRECRUITERS_MAX_PAGES = 30


def parse_smartrecruiters(data: dict, board: str, company: str) -> list[Job]:
    jobs = []
    for p in data.get("content") or []:
        loc = p.get("location") or {}
        place = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                                                    (loc.get("country") or "").upper()) if x)
        remote = bool(loc.get("remote"))
        if remote and "remote" not in place.lower():
            place = f"{place} (Remote)" if place else "Remote"
        kind = ((p.get("typeOfEmployment") or {}).get("label") or "")
        jobs.append(Job(
            source="smartrecruiters", board=board, job_id=str(p.get("id") or p.get("uuid") or ""),
            company=company or ((p.get("company") or {}).get("name") or board),
            title=(p.get("name") or "").strip(),
            url=f"https://jobs.smartrecruiters.com/{board}/{p.get('id')}",
            location=place, department=((p.get("department") or {}).get("label") or
                                        (p.get("function") or {}).get("label") or ""),
            remote=remote, posted_at=p.get("releasedDate") or "",
            extra={"employment": kind} if kind else {}))
    return [j for j in jobs if j.job_id and j.title]


def fetch_smartrecruiters(board: str, company: str = "", session=None) -> list[Job]:
    import time as _t
    out: list[Job] = []
    for page in range(SMARTRECRUITERS_MAX_PAGES):
        data = _get_json(SMARTRECRUITERS_URL.format(board=board, offset=page * 100), session)
        if not isinstance(data, dict):
            raise BoardError("response was not JSON")
        batch = parse_smartrecruiters(data, board, company)
        out += batch
        if len(data.get("content") or []) < 100 or len(out) >= (data.get("totalFound") or 0):
            break
        _t.sleep(0.5)  # gentle between pages of one company
    if not out and not (data.get("totalFound") or 0) and page == 0 and data.get("content") is None:
        raise BoardError("board not found (404)")
    return out


FETCHERS: dict[str, Callable[..., list[Job]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
}


# Careers-site readers (Workday, iCIMS, SuccessFactors, Taleo, Oracle) register themselves into FETCHERS.
import jobfeed.careers  # noqa: E402,F401
import jobfeed.smb  # noqa: F401,E402  (registers the smaller hiring systems)
