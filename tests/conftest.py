"""A small synthetic dataset with one planted example of every case the tools handle.

Decisions run Oct 2024 – Jun 2026, like the real files, so window maths matches production.
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

from h1b_sponsor_mcp.build.assemble import BuildInputs, write_database
from h1b_sponsor_mcp.build.sources import (
    LCA_COLUMNS,
    PERM_COLUMNS,
    read_uscis,
    standardise_lca,
    standardise_perm,
)
from h1b_sponsor_mcp.queries import Store

ACME_FEIN = "12-3456789"


def lca_row(case: str, decided: date, employer: str, fein: str = ACME_FEIN,
            title: str = "Software Engineer", wage: str = "120000", unit: str = "Year",
            city: str = "Atlanta", state: str = "GA", new: str = "1", change: str = "0",
            status: str = "Certified", visa: str = "H-1B", level: str = "II") -> dict:
    row = {k: None for k in LCA_COLUMNS}
    row.update({
        "CASE_NUMBER": case, "CASE_STATUS": status, "RECEIVED_DATE": str(decided),
        "DECISION_DATE": str(decided), "VISA_CLASS": visa, "JOB_TITLE": title,
        "SOC_CODE": "15-1252.00", "SOC_TITLE": "Software Developers", "FULL_TIME_POSITION": "Y",
        "TOTAL_WORKER_POSITIONS": "1", "NEW_EMPLOYMENT": new, "CONTINUED_EMPLOYMENT": "0",
        "CHANGE_EMPLOYER": change, "EMPLOYER_NAME": employer, "EMPLOYER_FEIN": fein,
        "EMPLOYER_STATE": "GA", "WORKSITE_CITY": city, "WORKSITE_STATE": state,
        "WAGE_RATE_OF_PAY_FROM": wage, "WAGE_UNIT_OF_PAY": unit, "PW_WAGE_LEVEL": level,
    })
    # A contact-detail column that exists in the real files and must never ship.
    row["EMPLOYER_POC_EMAIL"] = "hr@example.com"
    return row


def _monthly(start: date, n: int, step_days: int) -> list[date]:
    return [start + timedelta(days=i * step_days) for i in range(n)]


def make_lca() -> list[pd.DataFrame]:
    rows = []
    # ACME: 20 filings Jan–Jun 2025, 8 in Jan–Jun 2026 -> "slowing sharply".
    for i, d in enumerate(_monthly(date(2025, 1, 5), 20, 8)):
        name = ["ACME CLOUD INC", "Acme Cloud, Inc.", "ACME CLOUD SERVICES LLC"][i % 3]
        rows.append(lca_row(f"I-ACME-{i}", d, name))
    for i, d in enumerate(_monthly(date(2026, 1, 10), 8, 20)):
        rows.append(lca_row(f"I-ACME-N{i}", d, "ACME CLOUD INC", title="Data Scientist",
                            wage="60", unit="Hour"))
    # A different company that also starts with "Acme": separate FEIN, separate entity.
    rows.append(lca_row("I-TREE-1", date(2026, 2, 1), "Acme Tree Care LLC", fein="23-4567890"))
    # STEADY: 10 filings in each of the two Jan–Jun windows -> "Yes, active".
    for yr in (2025, 2026):
        for i, d in enumerate(_monthly(date(yr, 1, 3), 10, 15)):
            rows.append(lca_row(f"I-ST-{yr}-{i}", d, "Steady Corp", fein="34-5678901",
                                state="TX", city="Austin", change="1", new="0"))
    # GONE: filings only in early FY2025 -> "No filings in last 6 months" / no recent.
    for i, d in enumerate(_monthly(date(2024, 10, 1), 5, 10)):
        rows.append(lca_row(f"I-GONE-{i}", d, "Gone LLC", fein="45-6789012"))
    # Pins the data's end date to Jun 30, 2026, like the real FY2026 Q3 file.
    rows.append(lca_row("I-OTHER-1", date(2026, 6, 30), "Other Inc", fein="56-7890123"))
    # Not counted: denied, and E-3 visa class.
    rows.append(lca_row("I-DEN-1", date(2026, 3, 1), "Steady Corp", fein="34-5678901",
                        status="Denied"))
    rows.append(lca_row("I-E3-1", date(2026, 3, 1), "Steady Corp", fein="34-5678901",
                        visa="E-3 Australian"))
    df = pd.DataFrame(rows)
    # Same case number in two quarterly files: the later decision wins.
    dup = df[df["CASE_NUMBER"] == "I-ACME-0"].copy()
    dup["DECISION_DATE"] = "2025-02-20"
    return [df, dup]


def make_perm() -> list[pd.DataFrame]:
    rows = []
    for i, d in enumerate(_monthly(date(2025, 11, 1), 4, 30)):
        row = {k: None for k in PERM_COLUMNS}
        row.update({"CASE_NUMBER": f"G-{i}", "CASE_STATUS": "Certified",
                    "RECEIVED_DATE": "2024-06-01", "DECISION_DATE": str(d),
                    "EMP_BUSINESS_NAME": "Acme Cloud Inc", "EMP_FEIN": ACME_FEIN,
                    "EMP_STATE": "GA", "JOB_TITLE": "Software Engineer",
                    "PWD_SOC_CODE": "15-1252", "PWD_SOC_TITLE": "Software Developers",
                    "PRIMARY_WORKSITE_CITY": "Atlanta", "PRIMARY_WORKSITE_STATE": "GEORGIA",
                    "JOB_OPP_WAGE_FROM": "130,000.00", "JOB_OPP_WAGE_PER": "Year"})
        rows.append(row)
    return [pd.DataFrame(rows)]


USCIS_HEADER = [
    "Line by line", "Fiscal Year   ", "Employer (Petitioner) Name", "Tax ID",
    "Industry (NAICS) Code", "Petitioner City", "Petitioner State", "Petitioner Zip Code",
    "New Employment Approval", "New Employment Denial", "Continuation Approval",
    "Continuation Denial", "Change with Same Employer Approval",
    "Change with Same Employer Denial", "New Concurrent Approval", "New Concurrent Denial",
    "Change of Employer Approval", "Change of Employer Denial", "Amended Approval",
    "Amended Denial",
]


def write_uscis(path) -> None:
    rows = [
        ["1", "2026", "", "", "", "DALLAS", "TX", "75275"] + ["0"] * 12,  # USCIS "Null" row
        ["2", "2025", "ACME CLOUD INC", "1111", "54 - Professional", "ATLANTA", "GA", "30301",
         "30", "2", "10", "0", "0", "0", "0", "0", "5", "1", "0", "0"],
        ["3", "2026", "ACME CLOUD INC", "1111", "54 - Professional", "ATLANTA", "GA", "30301",
         "12", "0", "4", "0", "0", "0", "0", "0", "3", "0", "0", "0"],
        # Low denials: 2 of 202.
        ["4", "2025", "STEADY CORP", "3333", "54 - Professional", "AUSTIN", "TX", "78701",
         "100", "1", "100", "1", "0", "0", "0", "0", "0", "0", "0", "0"],
        # High denials: 6 of 26, far above the fixture's national rate.
        ["5", "2025", "GONE LLC", "4444", "54 - Professional", "ATLANTA", "GA", "30301",
         "20", "6", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0"],
    ]
    text = "\n".join("\t".join(r) for r in [USCIS_HEADER, *rows]) + "\n"
    path.write_bytes(text.encode("utf-16"))


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("db")
    uscis_file = tmp / "employer_information.csv"
    write_uscis(uscis_file)
    out = tmp / "sponsors.duckdb"
    write_database(
        standardise_lca(make_lca()), standardise_perm(make_perm()), read_uscis(uscis_file),
        out, BuildInputs(lca_files=[], perm_files=[], uscis_file=uscis_file,
                         uscis_through="FY2026 Q3"),
    )
    return out


@pytest.fixture
def store(db_path) -> Store:
    return Store(duckdb.connect(str(db_path), read_only=True))
