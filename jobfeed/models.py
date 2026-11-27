from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def html_to_text(raw: str | None) -> str:
    """Strip tags and unescape entities. Greenhouse double-escapes its HTML, so unescape twice."""
    if not raw:
        return ""
    text = html.unescape(html.unescape(raw))
    text = _TAG.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


@dataclass
class Job:
    source: str          # greenhouse | lever | ashby
    board: str           # board token / site name
    job_id: str
    company: str
    title: str
    url: str
    location: str = ""
    department: str = ""
    remote: bool = False
    posted_at: str = ""  # ISO timestamp as published by the board
    salary: str = ""
    description: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.board}:{self.job_id}"
