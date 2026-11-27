"""Import the user's own LinkedIn connections from LinkedIn's data export (Connections.csv).

LinkedIn: Settings > Data privacy > Get a copy of your data > Connections. Only first-degree
connections are included; no LinkedIn pages are ever requested by this app.
"""
from __future__ import annotations

import csv
import io
import re

REQUIRED = {"first name", "last name", "company"}
_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|sa|ag|holdings|group|technologies|technology|labs)\b\.?")
_NON = re.compile(r"[^a-z0-9 ]+")


class ImportError_(Exception):
    pass


def normalize_company(name: str | None) -> str:
    n = (name or "").lower().replace("&", " and ")
    n = _NON.sub(" ", n)
    n = _SUFFIXES.sub(" ", n)
    return " ".join(n.split())


def parse_connections_csv(data: bytes | str) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lower().startswith("first name")), None)
    if start is None:
        raise ImportError_("That doesn't look like LinkedIn's Connections.csv (no 'First Name' header found)")
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    fields = {(f or "").strip().lower() for f in reader.fieldnames or []}
    if not REQUIRED <= fields:
        raise ImportError_("The file is missing First Name, Last Name or Company columns")
    out = []
    for row in reader:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if not (r.get("first name") or r.get("last name")):
            continue
        out.append({
            "first_name": r.get("first name", ""),
            "last_name": r.get("last name", ""),
            "url": r.get("url", ""),
            "email": r.get("email address", ""),
            "company": r.get("company", ""),
            "company_norm": normalize_company(r.get("company", "")),
            "position": r.get("position", ""),
            "connected_on": r.get("connected on", ""),
        })
    return out


def company_matches(job_company: str, connection_company_norm: str) -> bool:
    a = normalize_company(job_company)
    b = connection_company_norm
    if not a or not b:
        return False
    if a == b:
        return True
    # "palantir" vs "palantir technologies" handled by suffix stripping; allow word-prefix matches too
    return b.startswith(a + " ") or a.startswith(b + " ")
