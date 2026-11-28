"""Roles from big employers' own careers sites, where the site's rules allow reading them.

Google: its terms allow automated access that follows its robots.txt, and robots.txt only rules out paging through
search results (`page=`). So we run a fixed set of first-page searches (role x level, United States), one request a
second, and read the job links they show. robots.txt is checked before every request.

Not read, on purpose (see CLAUDE.md): Meta (automated collection needs its written permission), Amazon (its
conditions of use bar data-gathering tools), Apple and Microsoft (their sites don't let crawlers read robots.txt).
Students add those roles with Save."""
from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote_plus

import requests

from jobfeed.models import Job, html_to_text
from jobfeed.sources import BoardError

log = logging.getLogger("jobfeed.bigtech")
GOOGLE_BASE = "https://www.google.com/about/careers/applications/jobs/results/"
GOOGLE_ROLES = ("product manager", "associate product manager", "program manager", "software engineer",
                "data scientist", "data analyst", "business analyst", "user experience", "solutions engineer",
                "hardware engineer", "research", "marketing", "finance", "sales", "operations", "consultant",
                "security engineer", "machine learning")
GOOGLE_LEVELS = ("INTERN_AND_APPRENTICE", "EARLY", "MID")
MAX_QUERIES = 60

_LINK = re.compile(r'href="(?:https://www\.google\.com)?/?(?:about/careers/applications/)?jobs/results/'
                   r'(\d{6,})-([a-z0-9-]+)[^"]*"', re.I)
_HEAD = re.compile(r"<h[23][^>]*>(.*?)</h[23]>", re.S | re.I)
_PLACE = re.compile(r"([A-Z][A-Za-z .'-]+, [A-Z]{2}, USA(?:; [A-Z][A-Za-z .'-]+, [A-Z]{2}, USA)*)")
_PAY = re.compile(r"US: \$[\d,]+ ?- ?\$[\d,]+")


def google_queries() -> list[str]:
    out = []
    for lv in GOOGLE_LEVELS:
        for role in GOOGLE_ROLES:
            if lv == "MID" and role not in ("product manager", "program manager", "software engineer",
                                            "data scientist"):
                continue  # students and new grads first; a few mid searches catch "plain" titles
            out.append(f"{GOOGLE_BASE}?q={quote_plus(role)}&target_level={lv}&location=United+States")
    return out[:MAX_QUERIES]


def _unslug(slug: str) -> str:
    small = {"and", "of", "for", "the", "in", "to", "a"}
    words = slug.split("-")
    return " ".join(w.upper() if w in ("phd", "ux", "ui", "ai", "ml", "tpm", "apm", "it") else
                    (w if (w in small and i) else w.capitalize()) for i, w in enumerate(words))


def parse_google_results(page: str) -> list[dict]:
    """Each job card on a results page: id, title, place, pay (title from the card's heading, else the link)."""
    jobs, seen = [], set()
    matches = list(_LINK.finditer(page))
    for i, m in enumerate(matches):
        jid = m.group(1)
        if jid in seen:
            continue
        seen.add(jid)
        start = matches[i - 1].end() if i else max(0, m.start() - 4000)
        card = page[start:m.end() + 3000]
        heads = [html_to_text(h).strip() for h in _HEAD.findall(page[start:m.start() + 600])]
        slug_title = _unslug(m.group(2))
        title = next((h for h in reversed(heads) if h and h.lower().replace(",", "").split()[:2]
                      == slug_title.lower().replace(",", "").split()[:2]), slug_title)
        text = html.unescape(re.sub(r"<[^>]+>", " ", card))
        place = _PLACE.search(text)
        pay = _PAY.search(text)
        jobs.append({"id": jid, "title": title, "slug": m.group(2), "location": place.group(1) if place else "United States",
                     "salary": pay.group(0).replace("US: ", "") if pay else ""})
    return jobs


def fetch_google(board: str = "google", company: str = "", session=None, existing=None, queries=None) -> list[Job]:
    from jobfeed.careers import _get, allowed
    s = session or requests.Session()
    found: dict[str, dict] = {}
    errors = 0
    for url in queries or google_queries():
        if not allowed(s, url):
            continue
        try:
            page = _get(s, url, "text/html").text
            got = parse_google_results(page)
            if not got:  # say what came back, so the audit shows whether it's a markup change or a block
                log.warning("Google careers: 0 roles parsed (%s bytes, %s job links, consent page: %s) for %s",
                            len(page), page.count("jobs/results/"), "consent.google" in page, url[-80:])
            for j in got:
                found.setdefault(j["id"], j)
        except BoardError as exc:
            errors += 1
            if "bot check" in str(exc) or errors >= 5:
                break  # never pushed past a challenge; what we have is kept
    if not found and errors:
        raise BoardError("Google careers didn't answer; trying again next run")
    return [Job(source="google", board="google", job_id=j["id"], company="Google", title=j["title"][:200],
                url=f"{GOOGLE_BASE}{j['id']}-{j['slug']}", location=j["location"][:200],
                remote="remote" in j["location"].lower(), salary=j["salary"]) for j in found.values()]


# Big employers' own job pages that members can share with everyone by saving them (public pages, no sign-in).
# host -> (company, path pattern a single job's page has)
SHAREABLE = {
    "amazon.jobs": ("Amazon", r"/jobs/\d+"),
    "metacareers.com": ("Meta", r"/(jobs|profile/job_details)/\d+"),
    "jobs.apple.com": ("Apple", r"/details/[\w-]+"),
    "careers.microsoft.com": ("Microsoft", r"/(job|jobs)/\d+"),
    "apply.careers.microsoft.com": ("Microsoft", r"/job/\d+"),
    "jobs.careers.microsoft.com": ("Microsoft", r"/job/\d+"),
    "google.com": ("Google", r"/about/careers/applications/jobs/results/\d+"),
    "lifeattiktok.com": ("TikTok", r"/search/\d+"),
    "jobs.bytedance.com": ("ByteDance", r"/position/\d+"),
    "tesla.com": ("Tesla", r"/careers/search/job/[\w-]+"),
    "explore.jobs.netflix.net": ("Netflix", r"/careers/job/\d+"),
    "jobs.netflix.com": ("Netflix", r"/jobs/\d+"),
}


def shareable(url: str) -> tuple[str, str, str] | None:
    """(clean url, host, company) when a link is a single public job page on a big employer's own site."""
    from urllib.parse import urlparse
    u = urlparse((url or "").strip())
    if u.scheme not in ("http", "https"):
        return None
    host = u.netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    rule = SHAREABLE.get(host)
    if not rule or not re.search(rule[1], u.path):
        return None
    return f"https://{u.netloc.lower()}{u.path}".rstrip("/"), host, rule[0]  # query/fragment dropped: may be personal


def shared_job(url: str, title: str, location: str = "", description: str = "", posted: str = "") -> Job | None:
    import hashlib
    hit = shareable(url)
    title = re.sub(r"\s+", " ", title or "").strip()
    if not hit or not title:
        return None
    clean, host, company = hit
    title = re.sub(r"\s*[|–—-]\s*(Careers at |Jobs at )?(%s)\b.*$" % re.escape(company), "", title, flags=re.I).strip()
    return Job(source="shared", board=host, job_id=hashlib.sha1(clean.encode()).hexdigest()[:16], company=company,
               title=title[:200], url=clean, location=(location or "").strip()[:200],
               remote="remote" in (location or "").lower(), posted_at=(posted or "")[:40],
               description=(description or "")[:20000])


# Search links into big employers' own careers sites, built from a member's titles and level. jobfeed never fetches
# these pages: the member clicks through and searches there. What they save comes back as a shared posting.
SEARCH_LINKS = {
    "Google": "https://www.google.com/about/careers/applications/jobs/results/?q={q}&location=United%20States{google_level}",
    "Amazon": "https://www.amazon.jobs/en/search?base_query={q}&loc_query=United%20States",
    "Meta": "https://www.metacareers.com/jobs?q={q}",
    "Apple": "https://jobs.apple.com/en-us/search?search={q}&location=united-states-USA{apple_level}",
    "Microsoft": "https://apply.careers.microsoft.com/careers?query={q}&location=United%20States",
    "Netflix": "https://explore.jobs.netflix.net/careers?query={q}",
    "TikTok": "https://lifeattiktok.com/search?keyword={q}",
    "Tesla": "https://www.tesla.com/careers/search/?query={q}&country=US",
}


def search_links(titles: list[str], levels: list[str] | None = None, limit_titles: int = 2) -> list[dict]:
    """[{title, links: [{company, url}]}] for a member's first titles. Interns get internship searches."""
    from urllib.parse import quote
    levels = set(levels or [])
    intern_only = bool(levels) and levels <= {"intern"}
    google_level = ("&target_level=INTERN_AND_APPRENTICE" if intern_only else
                    "&target_level=EARLY" if levels and levels <= {"intern", "entry"} else "")
    apple_level = "&team=internships-STDNT-INTRN" if intern_only else ""
    out = []
    for title in [t.strip() for t in titles if t and t.strip()][:limit_titles]:
        words = re.sub(r"\s+", " ", title)
        q = words + (" intern" if intern_only and "intern" not in words.lower() else "")
        links = []
        for company, tpl in SEARCH_LINKS.items():
            # Google and Apple filter by level themselves; elsewhere the word "intern" does it
            qq = words if company in ("Google",) or (company == "Apple" and intern_only) else q
            links.append({"company": company, "url": tpl.format(q=quote(qq), google_level=google_level,
                                                                 apple_level=apple_level)})
        out.append({"title": title, "links": links})
    return out


def _register():
    from jobfeed import sources
    sources.FETCHERS["google"] = fetch_google


_register()
