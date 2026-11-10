"""New prospects from Fiber's B2B company database, asked through an Exa agent run: companies that match the ideal
customer in prospects.toml, each with the employee count and funding stage the database holds.

Found companies are kept in prospects_discovered.csv, which reads as a prospects file (name,domain first), so
`python -m prospects run --prospects prospects_discovered.csv` researches them and a second `discover` skips them.
"""

import csv
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from clients import exa_client
from pipeline import runs

logger = logging.getLogger("discover")

HISTORY = "prospects_discovered.csv"
FIELDS = ["name", "domain", "why", "employees", "funding_stage", "focus", "discovered_at"]
MAX_LIMIT = 50  # a Fiber search costs a credit per result: a bigger list is a bigger bill
MAX_EXCLUDED = 500  # known domains sent to the agent; every known domain is still filtered out here

SYSTEM_PROMPT = (
    "Only real operating companies with their own website domain: no agencies listing their clients, no "
    "directories, no job boards. One entry per company. Take the employee count and funding stage from the "
    "company database and use null where it has none. Never return a company from the exclusion list."
)


def _schema(limit):
    return {
        "type": "object",
        "required": ["companies"],
        "properties": {
            "companies": {
                "type": "array",
                "maxItems": limit,
                "items": {
                    "type": "object",
                    "required": ["name", "domain", "why"],
                    "properties": {
                        "name": {"type": "string"},
                        "domain": {"type": "string", "description": "Primary website domain, e.g. acme.com"},
                        "why": {"type": "string", "description": "One sentence: why this company matches"},
                        "employees": {"type": ["integer", "null"], "description": "Employee count in the database"},
                        "funding_stage": {"type": ["string", "null"], "description": "Funding stage in the database"},
                    },
                },
            }
        },
    }


def known_domains(*paths):
    """The domains already listed in name,domain CSV files (prospects.csv, the history), normalised. Missing files,
    comment lines and rows without a domain are passed over."""
    known = set()
    for path in map(Path, paths):
        if path.is_file():
            with open(path, newline="", encoding="utf-8-sig") as f:
                known |= {
                    runs.normalize_domain(row[1])
                    for row in csv.reader(f)
                    if len(row) > 1 and not row[0].strip().startswith("#") and row[1].strip()
                }
    return known


_DOMAIN_RE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+")  # a host and nothing else: no port, address, query or sentence


def _text(value, *, name=False):
    """`value` as one line of text that prints and saves anywhere: whitespace runs become a space, half a
    surrogate pair (text cut inside an emoji) becomes "?", and text a spreadsheet would run as a formula gets a
    leading apostrophe (it comes from the web, through the agent).

    name=True also drops leading "#"s and spaces: a prospects file's readers take a row that starts with "#" for
    a comment, so "#paid" is kept as "paid". Otherwise `run` would never research it and every discover would
    find it, and pay for it, again."""
    text = " ".join(str(value or "").split()).encode("utf-8", "replace").decode("utf-8")
    if name:
        text = text.lstrip("# ")
    return f"'{text}" if text[:1] in ("=", "+", "-", "@") else text


def find(config, *, limit, focus="", known=()):
    """Up to `limit` companies that match [icp].description, narrowed by `focus`, none of them in `known`.

    Returns [{"name", "domain", "why", "employees", "funding_stage"}] with normalised domains, each domain once.
    One Exa agent run with Fiber attached; what it costs depends on how many database calls the agent makes.
    """
    icp = config["icp"]
    focus = _text(focus)
    avoid = f" Leave out: {'; '.join(icp['disqualifiers'])}." if icp["disqualifiers"] else ""
    narrowed = f" Narrow it to: {focus}." if focus else ""
    query = (
        "I'm building a B2B sales prospecting list using a B2B company database. "
        f"Find up to {limit} companies that match this ideal customer: {icp['description']}{narrowed}{avoid} "
        "For each return its name, its primary website domain, its employee count, its funding stage and one "
        "sentence on why it matches."
    )
    known = set(known)
    content, _ = exa_client.agent_answer(
        query,
        _schema(limit),
        system_prompt=SYSTEM_PROMPT,
        data_sources=["fiber"],
        exclude=[{"domain": domain} for domain in sorted(known)[:MAX_EXCLUDED]],
    )
    found = {}
    for company in content.get("companies") or []:
        if not isinstance(company, dict):
            continue
        name = _text(company.get("name"), name=True)
        domain = runs.normalize_domain(_text(company.get("domain")))
        employees = company.get("employees")
        if name and _DOMAIN_RE.fullmatch(domain) and domain not in known and domain not in found:
            found[domain] = {
                "name": name,
                "domain": domain,
                "why": _text(company.get("why")),
                "employees": employees if type(employees) is int and employees > 0 else None,
                "funding_stage": _text(company.get("funding_stage")) or None,
            }
    return list(found.values())[:limit]


def check_writable(path):
    """Raises OSError when rows could not be appended to the history; leaves no file behind when there was none
    (also behind a link to a file that is not there yet: the link stays, the file goes again)."""
    existed = os.path.exists(path)
    with open(path, "a", encoding="utf-8"):
        pass
    if not existed:
        os.unlink(os.path.realpath(path))


def save(path, companies, focus):
    """Appends `companies` (as find returns them) to the history CSV, under a `# name,domain,...` first line that
    a prospects file reader skips."""
    path = Path(path)
    new_file = not path.exists() or path.stat().st_size == 0
    stamp = datetime.now().isoformat(timespec="seconds")
    mid_line = not new_file and path.read_bytes()[-1:] not in (b"\n", b"\r")  # hand-edited, no final newline
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["# " + FIELDS[0], *FIELDS[1:]])
        elif mid_line:
            f.write("\r\n")  # or the first new row would be glued to the last old one
        for company in companies:
            writer.writerow([*(company[field] for field in FIELDS[:5]), _text(focus), stamp])
