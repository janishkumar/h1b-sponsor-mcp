import duckdb
import pandas as pd
import pytest

from h1b_sponsor_mcp.build.employers import MAX_NAMES_PER_FEIN, resolve
from h1b_sponsor_mcp.build.sources import annualise, read_uscis, standardise_lca
from h1b_sponsor_mcp.names import clean_fein, normalise_employer, query_key
from tests.conftest import make_lca, write_uscis


@pytest.mark.parametrize("raw,expected", [
    ("Amazon.com Services, LLC", "AMAZON COM SERVICES"),
    ("AMAZON.COM SERVICES LLC", "AMAZON COM SERVICES"),
    ("The Home Depot, Inc.", "HOME DEPOT"),
    ("Ernst & Young U.S. LLP", "ERNST AND YOUNG U S"),
    ("Acme Holdings LLC d/b/a Acme Labs", "ACME HOLDINGS"),
    (None, ""),
])
def test_normalise_employer(raw, expected):
    assert normalise_employer(raw) == expected


def test_query_key_applies_brand_aliases():
    assert query_key("Facebook") == "META PLATFORMS"
    assert query_key("aws") == "AMAZON WEB SERVICES"
    assert query_key("Stripe") == "STRIPE"


@pytest.mark.parametrize("raw,expected", [
    ("91-1144442", "911144442"), ("000000000", None), ("12-345", None), (None, None),
])
def test_clean_fein(raw, expected):
    assert clean_fein(raw) == expected


def test_annualise_units_and_outliers():
    amount = pd.Series([50.0, 2000.0, 120000.0, 5.0, 120000.0])
    unit = pd.Series(["Hour", "Week", "Year", "Hour", "Fortnight"])
    out = annualise(amount, unit)
    assert out.iloc[0] == 104_000 and out.iloc[1] == 104_000 and out.iloc[2] == 120_000
    assert pd.isna(out.iloc[3])  # $10,400/yr: a keying error, dropped
    assert pd.isna(out.iloc[4])  # unknown unit


def test_lca_dedupes_case_numbers_keeping_latest_decision():
    lca = standardise_lca(make_lca())
    assert lca["case_number"].is_unique
    row = lca.set_index("case_number").loc["I-ACME-0"]
    assert row["decision_date"] == pd.Timestamp("2025-02-20")
    assert row["worksite_state"] == "GA"


def test_uscis_reader_handles_tableau_utf16(tmp_path):
    path = tmp_path / "x.csv"
    write_uscis(path)
    df = read_uscis(path)
    assert len(df) == 4  # the blank-employer row is dropped
    assert df["new_employment_approved"].sum() == 162
    assert set(df["fiscal_year"]) == {2025, 2026}


def test_fein_merges_spellings_but_not_shared_payroll_feins():
    names = [f"CLIENT {i}" for i in range(MAX_NAMES_PER_FEIN + 1)]
    dol = pd.DataFrame({
        "employer_norm": ["ACME CLOUD", "ACME CLOUD SERVICES", *names],
        "employer_name": ["Acme Cloud", "Acme Cloud Services", *names],
        "fein": ["111111111", "111111111", *(["999999998"] * len(names))],
    })
    name_map, employers = resolve(dol, pd.Series(["ACME CLOUD", "USCIS ONLY"]))
    ids = name_map.set_index("employer_norm")["employer_id"]
    assert ids["ACME CLOUD"] == ids["ACME CLOUD SERVICES"]
    assert ids["CLIENT 0"] != ids["CLIENT 1"]  # payroll-provider FEIN ignored
    assert "USCIS ONLY" in ids.index
    assert employers["employer_id"].is_unique


def test_published_database_has_no_contact_details(db_path):
    con = duckdb.connect(str(db_path), read_only=True)
    columns = {r[0].lower() for r in con.execute(
        "SELECT column_name FROM information_schema.columns").fetchall()}
    for banned in ("email", "phone", "poc", "attorney", "preparer"):
        assert not any(banned in c for c in columns), banned


def test_fein_typo_by_another_firm_does_not_merge():
    dol = pd.DataFrame({
        "employer_norm": ["AMAZON COM SERVICES"] * 50 + ["AMAZON WEB SERVICES"] * 10
        + ["TECHCORP SOLUTIONS"] * 2,
        "employer_name": ["x"] * 62,
        "fein": ["912646425"] * 62,
    })
    ids = resolve(dol, pd.Series(dtype=str))[0].set_index("employer_norm")["employer_id"]
    assert ids["AMAZON COM SERVICES"] == ids["AMAZON WEB SERVICES"]
    assert ids["TECHCORP SOLUTIONS"] != ids["AMAZON COM SERVICES"]
