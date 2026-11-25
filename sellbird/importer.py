"""Import a lead list from CSV (``sellbird import``).

Rows become companies and prospects with status ``new``, so the Writer drafts
for them and every draft waits in the Outbox like any other. Nothing in the
file is trusted for deliverability: each address is graded by the same check
Scout uses for a published address, so without a verifier it stays ``guess``
and is never written for or sent.
"""

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from sellbird.collectors.discover import normalize_domain
from sellbird.integrations.email_finder import verify_published_address
from sellbird.models.company import Company
from sellbird.models.prospect import COMPANY_INBOX_SOURCE, Prospect
from sellbird.state import FREE_MAIL_DOMAINS

MAX_ROWS = 2000           # every row costs a verification lookup
MAX_CELL = 300
MAX_NOTES = 1000
SOURCE = "import"

# Loose header names → the field they fill. First match wins.
ALIASES = {
    "company": ("company", "company_name", "business_name", "business"),
    "email": ("email", "email_address", "e_mail"),
    "website": ("website", "domain", "url", "site"),
    "first_name": ("first_name", "firstname", "first"),
    "last_name": ("last_name", "lastname", "last", "surname"),
    "title": ("title", "job_title", "role"),
    "phone": ("phone", "phone_number", "mobile"),
    "location": ("location", "city", "address"),
    "industry": ("industry", "niche", "category", "sub_niche"),
    "notes": ("notes", "personalization_notes", "opening_fact", "hook"),
    "source_url": ("source_url", "source"),
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")


class ImportFileError(ValueError):
    """The file can't be imported at all; nothing was written."""


@dataclass
class ImportResult:
    imported: dict[str, int] = field(default_factory=dict)   # email status → count
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (row number, reason)

    @property
    def total(self) -> int:
        return sum(self.imported.values())


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (header or "").strip().lower()).strip("_")


def read_rows(path: Path) -> list[dict]:
    """The file's rows, each mapped onto Sellbird's field names."""
    path = Path(path)
    if not path.is_file():
        raise ImportFileError(f"{path} doesn't exist")
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = {_key(h): h for h in reader.fieldnames or [] if h}
        column = {}
        for name, options in ALIASES.items():
            found = next((headers[o] for o in options if o in headers), None)
            if found and found not in column.values():
                column[name] = found
        missing = [n for n in ("company", "email") if n not in column]
        if missing:
            raise ImportFileError(
                f"no {' or '.join(missing)} column (found: {', '.join(headers.values()) or 'none'})")
        rows = []
        for raw in reader:
            if len(rows) >= MAX_ROWS:
                raise ImportFileError(f"more than {MAX_ROWS} rows; split the file")
            rows.append({n: " ".join(str(raw.get(c) or "").split()) for n, c in column.items()})
    if not rows:
        raise ImportFileError(f"{path.name} has no rows")
    return rows


def _problem(row: dict) -> str:
    email = row.get("email", "").lower()
    if not email:
        return "no email"
    if not EMAIL_RE.match(email):
        return f"'{email[:60]}' isn't an email address"
    if not row.get("company"):
        return "no company"
    if (row.get("first_name") or row.get("last_name")) and not (
            row.get("first_name") and row.get("last_name") and row.get("title")):
        return "a named person needs a first name, last name and title"
    return ""


def _company(row: dict) -> Company:
    website = row.get("website", "")
    domain = normalize_domain(website)
    email_domain = row["email"].lower().partition("@")[2]
    if not domain and email_domain not in FREE_MAIL_DOMAINS:
        domain = email_domain
    if website and "://" not in website:
        website = "https://" + website
    return Company(
        name=row["company"][:MAX_CELL], domain=domain, website=website[:MAX_CELL],
        location=row.get("location", "")[:MAX_CELL], phone=row.get("phone", "")[:MAX_CELL],
        industry=row.get("industry", "")[:MAX_CELL], source=SOURCE,
        source_url=website[:MAX_CELL],
        # A business with no domain still needs one identity across re-imports.
        external_id="" if domain else f"import:{row['company'].lower()[:MAX_CELL]}",
    )


async def import_rows(state, rows: list[dict], env=None, dry_run: bool = False) -> ImportResult:
    """Add each good row as a company and a ``new`` prospect."""
    result = ImportResult()
    seen = set()
    for number, row in enumerate(rows, start=2):      # row 1 is the header
        problem = _problem(row)
        email = row.get("email", "").lower()
        if not problem and email in seen:
            problem = "same email earlier in the file"
        if not problem and await state.prospect_exists(email=email):
            problem = "already on file"
        if problem:
            result.skipped.append((number, problem))
            continue
        seen.add(email)
        if dry_run:
            result.imported["not checked (dry run)"] = result.imported.get("not checked (dry run)", 0) + 1
            continue
        status = await verify_published_address(email, env=env)
        company = _company(row)
        company_id = await state.add_company(company)
        named = bool(row.get("first_name") or row.get("last_name"))
        await state.add_prospect(Prospect(
            company_id=company_id, company=company.name,
            first_name=row.get("first_name", "")[:MAX_CELL],
            last_name=row.get("last_name", "")[:MAX_CELL],
            title=row.get("title", "")[:MAX_CELL] if named else "General inbox",
            email=email, email_status=status, email_verified=(status == "verified"),
            phone=row.get("phone", "")[:MAX_CELL], industry=company.industry,
            source=SOURCE if named else COMPANY_INBOX_SOURCE,
            source_url=(row.get("source_url") or company.website)[:MAX_CELL],
            personalization_notes=row.get("notes", "")[:MAX_NOTES],
            status="new",
        ))
        result.imported[status] = result.imported.get(status, 0) + 1
    return result
