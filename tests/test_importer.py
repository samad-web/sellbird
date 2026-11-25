"""CSV import (build-docs/features/csv-import.md). Fictional data only."""

import pytest
import pytest_asyncio

from sellbird import importer
from sellbird.importer import ImportFileError, import_rows, read_rows
from sellbird.state import StateManager

HEADER = "business_name,Email,Website,First Name,Last Name,Title,City,niche,notes\n"


def _csv(tmp_path, body, header=HEADER, name="leads.csv"):
    path = tmp_path / name
    path.write_text(header + body, encoding="utf-8-sig")
    return path


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "t.db"))
    await sm.init_db()
    return sm


@pytest.fixture
def graded(monkeypatch):
    """The verifier's verdict, per address; records every lookup."""
    calls = []

    async def verify(email, env=None):
        calls.append(email)
        return "risky" if email.startswith("info@") else "verified"

    monkeypatch.setattr(importer, "verify_published_address", verify)
    return calls


@pytest.mark.asyncio
async def test_ac1_valid_rows_import_as_new_prospects(tmp_path, state, graded):
    path = _csv(tmp_path,
                "Acme Fertility,ana@acme.example.com,acme.example.com,Ana,Rao,Director,Chennai,Fertility clinic,Opened a new centre\n"
                "Beta Clinic,info@beta.example.com,https://beta.example.com,,,,Salem,Fertility clinic,\n")
    result = await import_rows(state, read_rows(path))
    assert result.total == 2 and not result.skipped
    ana = await state.get_prospect_by_email("ana@acme.example.com")
    assert ana.status == "new" and ana.title == "Director" and ana.source == importer.SOURCE
    assert ana.personalization_notes == "Opened a new centre" and ana.industry == "Fertility clinic"
    company = await state.get_company(ana.company_id)
    assert company.domain == "acme.example.com" and company.location == "Chennai"
    inbox = await state.get_prospect_by_email("info@beta.example.com")
    assert inbox.is_company_inbox and inbox.title == "General inbox"


@pytest.mark.asyncio
async def test_ac2_status_comes_from_the_verifier_not_the_file(tmp_path, state, graded):
    path = _csv(tmp_path, "Acme,info@acme.example.com,,,,,,,,verified\n",
                header=HEADER.rstrip("\n") + ",email_status\n")
    await import_rows(state, read_rows(path))
    assert (await state.get_prospect_by_email("info@acme.example.com")).email_status == "risky"
    assert graded == ["info@acme.example.com"]


@pytest.mark.asyncio
async def test_ac3_dedup_within_file_and_against_database(tmp_path, state, graded):
    body = ("Acme,ana@acme.example.com,,Ana,Rao,Director,,,\n"
            "Acme again,ANA@acme.example.com,,Ana,Rao,Director,,,\n")
    first = await import_rows(state, read_rows(_csv(tmp_path, body)))
    assert first.total == 1 and first.skipped == [(3, "same email earlier in the file")]
    again = await import_rows(state, read_rows(_csv(tmp_path, body, name="again.csv")))
    assert again.total == 0 and (2, "already on file") in again.skipped


@pytest.mark.asyncio
async def test_ac4_bad_rows_skipped_good_rows_still_import(tmp_path, state, graded):
    body = ("Acme,,,,,,,,\n"
            "Acme,not-an-email,,,,,,,\n"
            ",ana@acme.example.com,,,,,,,\n"
            "Acme,ana@acme.example.com,,Ana,Rao,,,,\n"
            "Acme,solo@acme.example.com,,Ana,,Director,,,\n"
            "Good Co,owner@good.example.com,,Sam,Iyer,Owner,,,\n")
    result = await import_rows(state, read_rows(_csv(tmp_path, body)))
    assert result.total == 1
    reasons = dict(result.skipped)
    assert reasons[2] == "no email" and "isn't an email" in reasons[3]
    assert reasons[4] == "no company" and reasons[5] == "a named person needs a first name, last name and title"
    assert reasons[6] == reasons[5]


@pytest.mark.asyncio
async def test_ac5_free_mail_is_never_the_company_domain(tmp_path, state, graded):
    body = ("Acme Clinic,acmeclinic@gmail.com,,,,,,,\n"
            "Beta Clinic,betaclinic@gmail.com,,,,,,,\n")
    await import_rows(state, read_rows(_csv(tmp_path, body)))
    a = await state.get_prospect_by_email("acmeclinic@gmail.com")
    b = await state.get_prospect_by_email("betaclinic@gmail.com")
    assert a.company_id != b.company_id, "two gmail clinics must stay two companies"
    assert (await state.get_company(a.company_id)).domain == ""


@pytest.mark.asyncio
async def test_ac6_dry_run_writes_and_verifies_nothing(tmp_path, state, graded):
    path = _csv(tmp_path, "Acme,ana@acme.example.com,,Ana,Rao,Director,,,\n")
    result = await import_rows(state, read_rows(path), dry_run=True)
    assert result.total == 1 and graded == []
    assert await state.get_prospect_by_email("ana@acme.example.com") is None


def test_ac7_missing_required_columns_is_an_error(tmp_path):
    with pytest.raises(ImportFileError, match="email"):
        read_rows(_csv(tmp_path, "Acme,acme.example.com\n", header="company,website\n"))
    with pytest.raises(ImportFileError, match="doesn't exist"):
        read_rows(tmp_path / "missing.csv")
    with pytest.raises(ImportFileError, match="no rows"):
        read_rows(_csv(tmp_path, "", header="company,email\n", name="empty.csv"))


def test_too_many_rows_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "MAX_ROWS", 2)
    body = "".join(f"Co{i},a{i}@x.example.com,,,,,,,\n" for i in range(3))
    with pytest.raises(ImportFileError, match="more than 2 rows"):
        read_rows(_csv(tmp_path, body))
