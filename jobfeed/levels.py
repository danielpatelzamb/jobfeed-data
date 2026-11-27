"""Seniority from a job title, and where a location is. Plain rules: fast enough for a million titles."""
from __future__ import annotations

import re

LEVELS = ("intern", "entry", "mid", "senior")
LEVEL_LABELS = {"intern": "Internship", "entry": "Entry level / new grad", "mid": "Mid level (2-5 years)",
                "senior": "Senior and above"}


def _rx(words):
    return re.compile(r"(?<![a-z])(" + "|".join(words) + r")(?![a-z])")


_INTERN = _rx([r"intern", r"internship", r"co-?op", r"summer analyst", r"summer associate", r"apprentice",
               r"werkstudent", r"trainee", r"stagiaire", r"praktikum"])
_SENIOR = _rx([r"senior", r"sr\.?", r"staff", r"principal", r"lead", r"head", r"director", r"vp", r"vice president",
               r"chief", r"c[teofi]o", r"partner", r"managing", r"manager", r"architect", r"distinguished",
               r"fellow", r"expert", r"iii", r"iv", r"v", r"president", r"svp", r"evp", r"avp", r"executive"])
_ENTRY = _rx([r"junior", r"jr\.?", r"entry", r"entry[- ]level", r"new grad", r"graduate", r"early career",
              r"associate", r"analyst", r"assistant", r"coordinator", r"rotational", r"fellowship", r"i",
              r"campus", r"class of 20\d\d", r"20\d\d start", r"university grad"])
# "Associate" alone is entry in finance; "Associate Director" is not. Senior words win.


def level_of(title: str) -> str:
    t = (title or "").lower()
    if _INTERN.search(t) and "international" not in t.replace("intern ", ""):
        return "intern"
    if _SENIOR.search(t):
        return "senior"
    if _ENTRY.search(t):
        return "entry"
    return "mid"


_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
_US_WORDS = re.compile(r"(?<![a-z])(united states|usa|u\.s\.a?\.?|us|america|" + "|".join(_STATES) + r")(?![a-z])")
_US_ABBR = re.compile(r",\s*(" + "|".join(sorted(set(_STATES.values()))) + r")\b")
_US_CITIES = re.compile(r"(?<![a-z])(new york city|nyc|san francisco|sf bay area|bay area|los angeles|boston|"
                        r"seattle|chicago|austin|houston|dallas|denver|atlanta|miami|washington,? d\.?c\.?|"
                        r"philadelphia|san diego|san jose|palo alto|menlo park|mountain view|"
                        r"brooklyn|cambridge, ma|stamford|greenwich)(?![a-z])")
_NOT_US = re.compile(r"(?<![a-z])(canada|toronto|vancouver|london|united kingdom|uk|india|bangalore|bengaluru|"
                     r"germany|berlin|france|paris|ireland|dublin|singapore|australia|sydney|mexico|brazil|"
                     r"netherlands|amsterdam|spain|poland|japan|tokyo|israel|tel aviv|philippines|argentina|"
                     r"colombia|romania|portugal|lisbon|sweden|switzerland|zurich|hong kong|china|korea|"
                     r"south africa|nigeria|kenya|uae|dubai|emea|apac|latam|europe)(?![a-z])")


def is_us(location: str) -> bool | None:
    """True if the location is in the US, False if clearly elsewhere, None if it doesn't say."""
    raw = location or ""
    loc = raw.lower()
    if not loc.strip() or loc.strip() in ("remote", "anywhere", "worldwide", "global", "hybrid", "not specified"):
        return None
    us = bool(_US_WORDS.search(loc) or _US_ABBR.search(raw) or _US_CITIES.search(loc))
    other = bool(_NOT_US.search(loc))
    if us and not other:
        return True
    if other and not us:
        return False
    return True if us else None  # multi-country postings that include the US count as US
