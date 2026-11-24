"""Lead pipeline: import a list, find company emails on the company's own site, verify them, track stages.

Rules:
- Sources: the imported list, plus company websites. Each email records the page it came from.
- Websites are fetched only for live research, only pages that robots.txt allows, at most 4 pages per
  domain with a delay between requests. A site that refuses robots checks is skipped.
- Email check: syntax, then MX lookup. A lead can be contacted only with an MX-ok email.
- Stage moves follow the course pipeline: found > qualified > contacted > replied > booked > won | lost.
  found > qualified needs a 'qualification' approval (pamela or frederic). Every stage change is logged.
- ICP: 10+ employees when known, and a regulated sector when the sector is known.
"""

from __future__ import annotations

import csv
import re
import sqlite3
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

from .config import Config
from .gates import record_decision

USER_AGENT = "socialops-research/0.1"
PAGES = ("/", "/contact", "/contact-us", "/about")
MAX_PAGES_PER_DOMAIN = 4
ICP_SECTORS = ("health", "medical", "dental", "clinic", "physician", "real estate", "realty", "legal", "law", "attorney", "cpa", "accounting", "tax")
MIN_EMPLOYEES = 10
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
SYNTAX_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9](?:[A-Za-z0-9._%+-]*[A-Za-z0-9])?@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
JUNK_DOMAINS = ("sentry", "wixpress", "example", "domain.com", "yourdomain", "email.com", "godaddy", "squarespace")
STAGES = ("found", "qualified", "contacted", "replied", "booked", "won", "lost")
TRANSITIONS = {
    "found": {"qualified", "lost"},
    "qualified": {"contacted", "lost"},
    "contacted": {"replied", "lost"},
    "replied": {"booked", "lost"},
    "booked": {"won", "lost"},
    "won": set(),
    "lost": set(),
}
ALIASES = {
    "company": ("company", "companyname", "business", "businessname", "name", "organization", "organisation"),
    "website": ("website", "url", "domain", "site", "webaddress"),
    "sector": ("sector", "industry", "category", "type"),
    "city": ("city", "town"),
    "state": ("state", "province", "st"),
    "phone": ("phone", "telephone", "phonenumber"),
    "employees": ("employees", "employeecount", "numberofemployees", "size", "staff"),
}


class LeadError(ValueError):
    pass


def normalize_domain(value: str) -> str | None:
    value = (value or "").strip().lower()
    if not value:
        return None
    if "//" not in value:
        value = "https://" + value
    host = urlparse(value).hostname or ""
    host = host[4:] if host.startswith("www.") else host
    return host if "." in host else None


def _key(header: str) -> str:
    return re.sub(r"[^a-z]", "", header.lower())


def read_rows(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    if suffix == ".xlsx":
        try:
            import openpyxl
        except ImportError as exc:
            raise LeadError("XLSX needs openpyxl: pip install -e '.[leads]'") from exc
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(h or "") for h in rows[0]]
        return [dict(zip(headers, ["" if v is None else str(v) for v in r])) for r in rows[1:]]
    raise LeadError("file must be .csv or .xlsx")


def _pick(row: dict, field_name: str) -> str:
    wanted = ALIASES[field_name]
    for k, v in row.items():
        if _key(k or "") in wanted:
            return (v or "").strip()
    return ""


def is_icp(sector: str, employees: int | None) -> tuple[bool, str]:
    if employees is not None and employees < MIN_EMPLOYEES:
        return False, f"{employees} employees (under {MIN_EMPLOYEES})"
    if sector and not any(k in sector.lower() for k in ICP_SECTORS):
        return False, f"sector '{sector}' is outside the regulated-sector list"
    return True, ""


@dataclass
class ImportReport:
    imported: int = 0
    skipped: dict = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def import_leads(conn: sqlite3.Connection, brand_key: str, path: Path) -> ImportReport:
    report = ImportReport()
    rows = read_rows(path)
    for row in rows:
        company = _pick(row, "company")
        domain = normalize_domain(_pick(row, "website"))
        if not company:
            report.skip("no company name")
            continue
        if not domain:
            report.skip("no usable website")
            continue
        emp_raw = _pick(row, "employees").replace(",", "")
        employees = int(emp_raw) if emp_raw.isdigit() else None
        sector = _pick(row, "sector")
        ok, why = is_icp(sector, employees)
        if not ok:
            report.skip(f"not ICP: {why}")
            continue
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO leads (brand_key, company, domain, sector, city, state, phone, employees, "
                    "import_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (brand_key, company[:120], domain, sector[:80], _pick(row, "city")[:60],
                     _pick(row, "state")[:4], _pick(row, "phone")[:30], employees, path.name),
                )
                if cur.rowcount:
                    conn.execute("INSERT INTO lead_events (lead_id, event, detail) VALUES (?, 'imported', ?)",
                                 (cur.lastrowid, path.name))
                    report.imported += 1
                else:
                    report.skip("already in list (same website)")
        except sqlite3.Error as exc:
            report.skip(f"database error: {exc}")
    return report


# --- email discovery and verification -------------------------------------------------

def syntax_ok(email: str) -> bool:
    return bool(SYNTAX_RE.match(email))


def mx_lookup_default(domain: str) -> bool | None:
    try:
        import dns.resolver
    except ImportError:
        return None  # unknown: MX check not available on this Mac
    try:
        return bool(dns.resolver.resolve(domain, "MX", lifetime=5))
    except Exception:
        return False


def pick_email(html: str, domain: str) -> str | None:
    """First email on the page that belongs to the company's own domain. Anything else is ignored."""
    for m in EMAIL_RE.findall(html):
        e = m.lower().rstrip(".")
        dom = e.split("@", 1)[1]
        if any(j in dom for j in JUNK_DOMAINS) or dom.endswith((".png", ".jpg", ".gif", ".webp")):
            continue
        if dom == domain or dom.endswith("." + domain):
            return e
    return None


def robots_allows(robots_text: str, url: str) -> bool:
    """True when there are no rules, or the rules allow this user agent to fetch the URL."""
    if not robots_text.strip():
        return True
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(robots_text.splitlines())
    return rp.can_fetch(USER_AGENT, url)


def research_lead(conn: sqlite3.Connection, lead_id: int, fetch: Callable[[str], tuple[int, str]],
                  mx_check: Callable[[str], bool | None], sleep: Callable[[float], None] = lambda s: None,
                  delay: float = 2.0) -> str:
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        return "no such lead"
    if lead["email"]:
        return "already has an email"
    domain = lead["domain"]
    status, robots = fetch(f"https://{domain}/robots.txt")
    if status == 0 or status >= 500:
        return "skipped: robots.txt unavailable, site not checked"
    rules = robots if status == 200 else ""  # 404 or other 4xx: no rules published
    found, source = None, None
    fetched = 0
    for path in PAGES:
        if fetched >= MAX_PAGES_PER_DOMAIN:
            break
        url = urljoin(f"https://{domain}", path)
        if not robots_allows(rules, url):
            continue
        if fetched:
            sleep(delay)
        fetched += 1
        status, html = fetch(url)
        if status != 200:
            continue
        found = pick_email(html, domain)
        if found:
            source = url
            break
    if not found:
        with conn:
            conn.execute("INSERT INTO lead_events (lead_id, event, detail) VALUES (?, 'email_not_found', '')", (lead_id,))
        return "no company-domain email on the site"
    if not syntax_ok(found):
        check = "syntax_bad"
    else:
        result = mx_check(domain)
        check = "unverified" if result is None else ("mx_ok" if result else "no_mx")
    with conn:
        conn.execute("UPDATE leads SET email=?, email_source=?, email_check=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                     "WHERE id=?", (found, source, check, lead_id))
        conn.execute("INSERT INTO lead_events (lead_id, event, detail) VALUES (?, 'email_found', ?)",
                     (lead_id, f"{check} from {source}"))
    return f"email {check}"


# --- stages -----------------------------------------------------------------------

def qualify(conn: sqlite3.Connection, lead_id: int, actor: str) -> str:
    lead = conn.execute("SELECT stage FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None or lead["stage"] != "found":
        return "not qualified: lead must be in 'found'"
    record_decision(conn, "qualification", lead_id, actor, "approved")
    return set_stage(conn, lead_id, "qualified", actor, "qualified by " + actor)


def set_stage(conn: sqlite3.Connection, lead_id: int, to: str, actor: str | None, detail: str = "") -> str:
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if lead is None:
        return "no such lead"
    current = lead["stage"]
    if to not in TRANSITIONS.get(current, set()):
        return f"not moved: {current} to {to} is not allowed"
    if to == "contacted" and (not lead["email"] or lead["email_check"] != "mx_ok"):
        return "not moved: contacting needs an email that passed the MX check"
    with conn:
        conn.execute("UPDATE leads SET stage=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (to, lead_id))
        conn.execute("INSERT INTO lead_events (lead_id, event, detail, actor) VALUES (?, ?, ?, ?)",
                     (lead_id, f"stage:{to}", detail or f"from {current}", actor))
    return f"moved to {to}"


def http_fetch(url: str, timeout: float = 10.0) -> tuple[int, str]:
    """GET a public page. Returns (status, text); status 0 on network failure. Generic user agent only."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"user-agent": USER_AGENT, "accept": "text/html,text/plain"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(1_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, ""


EXPORT_COLUMNS = ["company", "domain", "sector", "city", "state", "stage", "email", "email_source",
                  "email_check", "import_source", "updated_at"]


def export_csv(conn: sqlite3.Connection, brand_key: str, out: Path) -> int:
    rows = conn.execute("SELECT * FROM leads WHERE brand_key = ? ORDER BY stage, company", (brand_key,)).fetchall()
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(EXPORT_COLUMNS)
        for r in rows:
            w.writerow([r[c] if r[c] is not None else "" for c in EXPORT_COLUMNS])
    return len(rows)
