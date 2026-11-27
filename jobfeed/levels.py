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
               r"fellow", r"expert", r"iii", r"iv", r"v", r"president", r"svp", r"evp", r"avp", r"executive",
               r"supervisor", r"superintendent"])
_ENTRY = _rx([r"junior", r"jr\.?", r"entry", r"entry[- ]level", r"new grad", r"graduate", r"early career",
              r"associate", r"analyst", r"assistant", r"coordinator", r"rotational", r"fellowship", r"i",
              r"campus", r"class of 20\d\d", r"20\d\d start", r"university grad"])
# "Associate" alone is entry in finance; "Associate Director" is not. Senior words win.

# "Manager" is a seniority word only for people managers. Product, program, project, account, marketing (and similar)
# managers are individual roles hired at every level, including "Associate Product Manager" for new grads.
_IC_MANAGER = re.compile(r"(?<![a-z])(?:technical |associate |assistant |junior |jr\.? )?(?:product|program|project|"
                         r"account|marketing|product marketing|brand|community|content|campaign|growth|social media|"
                         r"category|partner(?:ship)?s?|customer success|success|client|case|office|lifecycle|channel|"
                         r"merchandising|events?|implementation|operations program|release|ecommerce|e-commerce|"
                         r"crm|seo|digital marketing|influencer|retention|pricing) managers?(?![a-z])")
_PEOPLE_MANAGER = re.compile(r"(?<![a-z])(group|general|engineering|people|regional|district|area|senior|sr\.?) "
                             r"(product |program )?managers?(?![a-z])|managers? of(?![a-z])")


# Most titles carry no seniority word ("Software Engineer", "Data Analyst"); level_of calls those "mid", and weak
# words like "Analyst" or "Associate" make a title "entry". Neither is a real statement about the role, so a search
# for a level also takes the neighbouring tier and lets the years the posting asks for rule roles out.
_REACH = {"intern": {"intern"}, "entry": {"entry", "mid"}, "mid": {"mid", "entry"}, "senior": {"senior", "mid"}}


_MID_WORD = _rx([r"ii", r"mid[- ]level", r"mid", r"intermediate", r"level 2", r"l2"])


def too_senior_for(levels: list[str] | None, title: str) -> bool:
    """A title that names a mid tier ("Engineer II", "Mid-level") isn't for someone who only wants entry roles."""
    return bool(levels) and set(levels) <= {"intern", "entry"} and bool(_MID_WORD.search((title or "").lower()))


def search_levels(levels: list[str] | None) -> list[str]:
    """The stored title levels a search for these levels should include."""
    out: set[str] = set()
    for lv in levels or []:
        out |= _REACH.get(lv, {lv})
    return sorted(out)


def level_of(title: str) -> str:
    t = (title or "").lower()
    if _INTERN.search(t) and "international" not in t.replace("intern ", ""):
        return "intern"
    if _PEOPLE_MANAGER.search(t):
        return "senior"
    if _SENIOR.search(_IC_MANAGER.sub(" ", t)):
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
_NOT_US = re.compile(r"(?<![a-z])(canada|toronto|vancouver|montreal|ottawa|calgary|edmonton|ontario|quebec|"
                     r"british columbia|alberta|london|united kingdom|uk|england|scotland|wales|manchester|edinburgh|"
                     r"india|bangalore|bengaluru|hyderabad|pune|chennai|mumbai|delhi|new delhi|gurgaon|gurugram|noida|"
                     r"kolkata|ahmedabad|telangana|karnataka|maharashtra|tamil nadu|haryana|uttar pradesh|"
                     r"pakistan|karachi|lahore|islamabad|bangladesh|dhaka|sri lanka|colombo|nepal|"
                     r"germany|berlin|munich|hamburg|frankfurt|france|paris|ireland|dublin|singapore|australia|sydney|"
                     r"melbourne|new zealand|auckland|mexico|guadalajara|monterrey|brazil|sao paulo|são paulo|"
                     r"rio de janeiro|netherlands|amsterdam|rotterdam|spain|madrid|barcelona|poland|warsaw|krakow|"
                     r"kraków|wroclaw|japan|tokyo|osaka|israel|tel aviv|philippines|manila|cebu|argentina|buenos aires|"
                     r"colombia|bogota|bogotá|medellin|chile|santiago|peru|lima|romania|bucharest|portugal|lisbon|porto|"
                     r"sweden|stockholm|switzerland|zurich|geneva|austria|vienna|italy|milan|rome|belgium|brussels|"
                     r"denmark|copenhagen|norway|oslo|finland|helsinki|estonia|tallinn|lithuania|vilnius|latvia|riga|"
                     r"czech|czechia|prague|hungary|budapest|ukraine|kyiv|serbia|belgrade|bulgaria|sofia|croatia|"
                     r"greece|athens|turkey|türkiye|istanbul|hong kong|china|shanghai|beijing|shenzhen|taiwan|taipei|"
                     r"korea|seoul|vietnam|ho chi minh|hanoi|thailand|bangkok|indonesia|jakarta|malaysia|kuala lumpur|"
                     r"south africa|cape town|johannesburg|nigeria|lagos|kenya|nairobi|ghana|accra|egypt|cairo|morocco|"
                     r"uae|united arab emirates|dubai|abu dhabi|saudi arabia|riyadh|qatar|doha|bahrain|kuwait|oman|"
                     r"jordan|amman|lebanon|dominican republic|costa rica|guatemala|el salvador|honduras|panama|"
                     r"uruguay|montevideo|kazakhstan|armenia|yerevan|emea|apac|latam|europe|asia)(?![a-z])")
# a location that is only a country code, and isn't a US state code
_ISO_ONLY = {"pk", "do", "it", "tr", "ae", "br", "mx", "gb", "uk", "fr", "es", "pl", "nl", "be", "se", "no", "dk",
             "fi", "ch", "at", "ie", "pt", "ro", "bg", "hr", "rs", "ua", "cz", "hu", "gr", "il", "eg", "ng", "ke",
             "za", "au", "nz", "jp", "kr", "cn", "hk", "tw", "sg", "my", "th", "vn", "ph", "id", "bd", "lk", "np",
             "ar", "cl", "co", "pe", "uy", "cr", "gt", "sv", "hn", "pa", "sa", "qa", "kw", "om", "bh", "jo", "lb"}


def is_us(location: str) -> bool | None:
    """True if the location is in the US, False if clearly elsewhere, None if it doesn't say."""
    raw = location or ""
    loc = raw.lower()
    if not loc.strip() or loc.strip() in ("remote", "anywhere", "worldwide", "global", "hybrid", "not specified"):
        return None
    if loc.strip(" .") in _ISO_ONLY:
        return False
    words = bool(_US_WORDS.search(loc))
    strong = words or bool(_US_ABBR.search(raw))
    us = strong or bool(_US_CITIES.search(loc))
    other = bool(_NOT_US.search(loc))
    if other and not words:  # "Bengaluru, IN", "Amsterdam, NH", "Cambridge, UK": a foreign place beats a bare code
        return False
    if us and not other:
        return True
    if other and not us:
        return False
    return True if us else None  # multi-country postings that include the US count as US
