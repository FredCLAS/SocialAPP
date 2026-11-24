import csv
from pathlib import Path

import pytest

from socialops.config import load_config
from socialops.db import connect, migrate_up
from socialops.gates import GateError
from socialops.leads import (
    export_csv, import_leads, is_icp, normalize_domain, pick_email, qualify, research_lead,
    robots_allows, set_stage, EXPORT_COLUMNS,
)


@pytest.fixture
def conn(tmp_db):
    c = connect(tmp_db)
    migrate_up(c)
    c.execute("INSERT INTO brands (key, display_name, website) VALUES ('clas_by_fred', 'CLAS by Fred', 'https://classbyfred.com')")
    c.commit()
    yield c
    c.close()


def write_csv(tmp_path, rows, name="list.csv"):
    p = tmp_path / name
    keys = sorted({k for r in rows for k in r})
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    return p


def lead_id(conn, domain):
    return conn.execute("SELECT id FROM leads WHERE domain=?", (domain,)).fetchone()[0]


def test_domain_normalization():
    assert normalize_domain("https://www.Example-Clinic.com/about") == "example-clinic.com"
    assert normalize_domain("clinic.org") == "clinic.org"
    assert normalize_domain("") is None
    assert normalize_domain("localhost") is None


def test_icp_rules():
    assert is_icp("Healthcare", 25) == (True, "")
    assert not is_icp("Healthcare", 4)[0]
    assert not is_icp("Retail", 50)[0]
    assert is_icp("", None)[0]  # unknown sector and size are kept for review


def test_import_filters_dedupes_and_reports(conn, tmp_path):
    f = write_csv(tmp_path, [
        {"Company": "Bay Dental", "Website": "https://www.baydental.com", "Sector": "Dental", "Employees": "14", "City": "Jacksonville", "State": "FL"},
        {"Company": "Tiny Clinic", "Website": "tinyclinic.com", "Sector": "Healthcare", "Employees": "3"},
        {"Company": "Shop", "Website": "shop.com", "Sector": "Retail", "Employees": "40"},
        {"Company": "No Site", "Website": "", "Sector": "Legal"},
        {"Company": "Bay Dental again", "Website": "baydental.com", "Sector": "Dental", "Employees": "14"},
        {"Company": "Smith CPA", "Website": "smithcpa.com", "Sector": "CPA", "Employees": "12"},
    ])
    rep = import_leads(conn, "clas_by_fred", f)
    assert rep.imported == 2
    assert rep.skipped["not ICP: 3 employees (under 10)"] == 1
    assert rep.skipped["no usable website"] == 1
    assert rep.skipped["already in list (same website)"] == 1
    assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2


def test_import_xlsx(conn, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Company", "Website", "Industry", "Employees"])
    ws.append(["Legal Group", "legalgroup.com", "Legal", 30])
    path = tmp_path / "list.xlsx"
    wb.save(path)
    assert import_leads(conn, "clas_by_fred", path).imported == 1


def test_bad_file_type(conn, tmp_path):
    from socialops.leads import LeadError
    p = tmp_path / "list.txt"
    p.write_text("x")
    with pytest.raises(LeadError):
        import_leads(conn, "clas_by_fred", p)


def test_pick_email_only_company_domain():
    html = "<a href='mailto:info@clinic.com'>x</a> ads@google.com john@sentry.io Contact: Office@Clinic.com"
    assert pick_email(html, "clinic.com") == "info@clinic.com"
    assert pick_email("admin@other.com", "clinic.com") is None
    assert pick_email("logo@2x.png and hi@clinic.com", "clinic.com") == "hi@clinic.com"


def test_robots_rules():
    rules = "User-agent: *\nDisallow: /private\n"
    assert robots_allows("", "https://x.com/contact")
    assert not robots_allows(rules, "https://x.com/private/page")
    assert robots_allows(rules, "https://x.com/contact")


def fake_site(pages, robots_status=404):
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return robots_status, pages.get("robots", "")
        path = url.split(".com", 1)[1] or "/"
        if path in pages:
            return 200, pages[path]
        return 404, ""

    return fetch, calls


def test_research_finds_email_and_records_source(conn, tmp_path):
    f = write_csv(tmp_path, [{"Company": "Bay Dental", "Website": "baydental.com", "Sector": "Dental", "Employees": "14"}])
    import_leads(conn, "clas_by_fred", f)
    lid = lead_id(conn, "baydental.com")
    fetch, _ = fake_site({"/contact": "Email us at front@baydental.com"})
    out = research_lead(conn, lid, fetch, lambda d: True, sleep=lambda s: None)
    assert out == "email mx_ok"
    row = conn.execute("SELECT email, email_source, email_check FROM leads WHERE id=?", (lid,)).fetchone()
    assert tuple(row) == ("front@baydental.com", "https://baydental.com/contact", "mx_ok")


def test_research_respects_robots_disallow(conn, tmp_path):
    f = write_csv(tmp_path, [{"Company": "Bay Dental", "Website": "baydental.com", "Sector": "Dental", "Employees": "14"}])
    import_leads(conn, "clas_by_fred", f)
    lid = lead_id(conn, "baydental.com")
    fetch, calls = fake_site({"/contact": "front@baydental.com", "robots": "User-agent: *\nDisallow: /contact\n"}, robots_status=200)
    assert research_lead(conn, lid, fetch, lambda d: True, sleep=lambda s: None) == "no company-domain email on the site"
    assert not any(c.endswith("/contact") for c in calls)


def test_research_skips_site_when_robots_unreachable(conn, tmp_path):
    f = write_csv(tmp_path, [{"Company": "Bay Dental", "Website": "baydental.com", "Sector": "Dental", "Employees": "14"}])
    import_leads(conn, "clas_by_fred", f)
    lid = lead_id(conn, "baydental.com")
    fetch, calls = fake_site({"/contact": "front@baydental.com"}, robots_status=503)
    assert "robots.txt unavailable" in research_lead(conn, lid, fetch, lambda d: True, sleep=lambda s: None)
    assert len(calls) == 1


def test_research_caps_pages_and_delays(conn, tmp_path):
    f = write_csv(tmp_path, [{"Company": "Bay Dental", "Website": "baydental.com", "Sector": "Dental", "Employees": "14"}])
    import_leads(conn, "clas_by_fred", f)
    lid = lead_id(conn, "baydental.com")
    fetch, calls = fake_site({})
    sleeps = []
    research_lead(conn, lid, fetch, lambda d: True, sleep=sleeps.append)
    assert len([c for c in calls if not c.endswith("robots.txt")]) == 4
    assert len(sleeps) == 3 and all(s == 2.0 for s in sleeps)


def test_syntax_bad_and_mx_unknown_states(conn, tmp_path):
    f = write_csv(tmp_path, [{"Company": "Bay Dental", "Website": "baydental.com", "Sector": "Dental", "Employees": "14"}])
    import_leads(conn, "clas_by_fred", f)
    lid = lead_id(conn, "baydental.com")
    fetch, _ = fake_site({"/": "reach me at a..b@baydental.com"})
    research_lead(conn, lid, fetch, lambda d: None, sleep=lambda s: None)
    assert conn.execute("SELECT email_check FROM leads WHERE id=?", (lid,)).fetchone()[0] == "syntax_bad"


def make_lead(conn, tmp_path, domain="baydental.com", email=True, mx=True):
    f = write_csv(tmp_path, [{"Company": "Bay Dental", "Website": domain, "Sector": "Dental", "Employees": "14"}],
                  name=f"{domain}.csv")
    import_leads(conn, "clas_by_fred", f)
    lid = lead_id(conn, domain)
    if email:
        conn.execute("UPDATE leads SET email='front@baydental.com', email_check=? WHERE id=?",
                     ("mx_ok" if mx else "no_mx", lid))
        conn.commit()
    return lid


def test_qualify_needs_approver_and_moves_stage(conn, tmp_path):
    lid = make_lead(conn, tmp_path, email=False)
    with pytest.raises(GateError):
        qualify(conn, lid, "intern")
    assert qualify(conn, lid, "frederic") == "moved to qualified"
    assert conn.execute("SELECT stage FROM leads WHERE id=?", (lid,)).fetchone()[0] == "qualified"
    events = [r[0] for r in conn.execute("SELECT event FROM lead_events WHERE lead_id=? ORDER BY id", (lid,))]
    assert events == ["imported", "stage:qualified"]


def test_contacting_needs_qualified_and_mx_ok(conn, tmp_path):
    lid = make_lead(conn, tmp_path, mx=False)
    assert set_stage(conn, lid, "contacted", "pamela").startswith("not moved")  # found -> contacted not allowed
    qualify(conn, lid, "frederic")
    assert "MX check" in set_stage(conn, lid, "contacted", "pamela")
    conn.execute("UPDATE leads SET email_check='mx_ok' WHERE id=?", (lid,))
    conn.commit()
    assert set_stage(conn, lid, "contacted", "pamela") == "moved to contacted"


def test_stage_rules_and_terminal_states(conn, tmp_path):
    lid = make_lead(conn, tmp_path)
    qualify(conn, lid, "frederic")
    set_stage(conn, lid, "contacted", "pamela")
    set_stage(conn, lid, "replied", "pamela")
    set_stage(conn, lid, "booked", "pamela")
    set_stage(conn, lid, "won", "pamela")
    assert set_stage(conn, lid, "lost", "pamela").startswith("not moved")  # won is terminal
    assert set_stage(conn, lid, "bogus", "pamela").startswith("not moved")


def test_export_has_sheet_columns(conn, tmp_path):
    make_lead(conn, tmp_path)
    out = tmp_path / "crm.csv"
    assert export_csv(conn, "clas_by_fred", out) == 1
    with out.open() as fh:
        header = next(csv.reader(fh))
    assert header == EXPORT_COLUMNS
