"""How many years of experience a posting asks for, read from its description.

Built against real phrasings from Greenhouse, Lever and Ashby postings (Nov 2026), e.g.
"10+ years of experience", "Minimum 1 year of experience in mobile development",
"5–7+ years of experience selling SaaS", "At least three years of previous administrative ... experience",
"2+ years of hands-on work experience".
"""
from __future__ import annotations

import re

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "a": 1, "an": 1}
_N = r"(\d{1,2}|" + "|".join(k for k in _NUM_WORDS if len(k) > 2) + r")"

# "<n>(+)( - <m>)(+) years" with an optional lead-in, followed within a short window by an experience word.
_YEARS = re.compile(
    r"(?P<lead>minimum(?: of)?|at least|min\.?|over|more than|\b)?\s*"
    r"(?P<n>" + _N + r")\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(?P<m>" + _N + r")\s*\+?)?\s*"
    r"(?:\(\s*\d+\s*\)\s*)?(?:full[- ]time\s+)?years?['’]?",
    re.I)
_EXP_AFTER = re.compile(r"^[^.;\n]{0,70}?\b(experience|work(?:ing)?|of (?:\w+ ){0,3}(?:in|as|at|with)\b|"
                        r"professional|industry|relevant|post[- ]?(?:grad|graduate|mba)|track record)", re.I)
_IN_AFTER = re.compile(r"^\s*(?:of\s+)?(?:[\w-]+\s+){0,2}?(?:in|as an?|at an?)\s+(?!(?:a|the)?\s*row\b)", re.I)
_NOT_EXP = re.compile(r"^\s*(?:old|of age|degree|program|college|university|ago|warranty|contract|term|commitment|"
                      r"vesting|cliff|in business|history)", re.I)
_SOFT = re.compile(r"preferred|nice to have|nice-to-have|bonus|a plus|ideally|desired|is helpful|an advantage", re.I)
_SOFT_HEAD = re.compile(r"^(?!.*\d)\W*(preferred|nice[- ]to[- ]have|bonus|pluses|extra credit)[^.]{0,40}:?\s*$", re.I)
_HARD_HEAD = re.compile(r"^(?!.*\d)\W*(required|requirements|minimum|basic|must[- ]have|what you('|’)ll need|"
                        r"qualifications|you have|about you|who you are)[^.]{0,40}:?\s*$", re.I)
_SPLIT = re.compile(r"(?<=[.;!?])\s+|\n+|\s+[•·▪●◦]\s*|\s{2,}")


def _num(s: str) -> int | None:
    if s is None:
        return None
    s = s.lower()
    return int(s) if s.isdigit() else _NUM_WORDS.get(s)


def years_required(text: str) -> tuple[int | None, int | None]:
    """(required, preferred): the most years any must-have line asks for, and the same for nice-to-haves.
    None when the posting never says."""
    if not text:
        return None, None
    req: list[int] = []
    pref: list[int] = []
    soft_section = False
    for line in _SPLIT.split(text):
        if _SOFT_HEAD.match(line):
            soft_section = True
            continue
        if _HARD_HEAD.match(line):
            soft_section = False
            continue
        if "year" not in line.lower():
            continue
        for m in _YEARS.finditer(line):
            n = _num(m.group("n"))
            if n is None or n > 25:
                continue
            after = line[m.end():]
            if _NOT_EXP.match(after) or not (_EXP_AFTER.match(after) or _IN_AFTER.match(after)):
                continue
            if m.group("n").lower() in ("a", "an") and not m.group("lead"):
                continue
            (pref if soft_section or _SOFT.search(line) else req).append(n)
    return (max(req) if req else None), (max(pref) if pref else None)


def experience_note(required: int | None, have: int, stretch: int) -> str:
    if required is None:
        return ""
    if required <= have:
        return f"Asks for {required}+ years: you qualify"
    if required <= have + stretch:
        return f"Asks for {required}+ years: a stretch"
    return f"Asks for {required}+ years"
