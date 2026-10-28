"""Read the raw government files into slim, standardised frames.

Only the columns the server needs are kept. Contact names, emails, phone numbers,
attorneys and preparers are dropped here and never reach the published database.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import pandas as pd

from h1b_sponsor_mcp.names import clean_fein, normalise_employer

log = logging.getLogger(__name__)

ANNUAL_FACTOR = {"YEAR": 1, "HOUR": 2080, "WEEK": 52, "BI-WEEKLY": 26, "MONTH": 12}

LCA_COLUMNS = {
    "CASE_NUMBER": "case_number",
    "CASE_STATUS": "status",
    "RECEIVED_DATE": "received_date",
    "DECISION_DATE": "decision_date",
    "VISA_CLASS": "visa_class",
    "JOB_TITLE": "job_title",
    "SOC_CODE": "soc_code",
    "SOC_TITLE": "soc_title",
    "FULL_TIME_POSITION": "full_time",
    "TOTAL_WORKER_POSITIONS": "workers",
    "NEW_EMPLOYMENT": "new_employment",
    "CONTINUED_EMPLOYMENT": "continued_employment",
    "CHANGE_EMPLOYER": "change_employer",
    "EMPLOYER_NAME": "employer_name",
    "EMPLOYER_FEIN": "fein",
    "EMPLOYER_STATE": "employer_state",
    "WORKSITE_CITY": "worksite_city",
    "WORKSITE_STATE": "worksite_state",
    "WAGE_RATE_OF_PAY_FROM": "wage_from",
    "WAGE_UNIT_OF_PAY": "wage_unit",
    "PW_WAGE_LEVEL": "pw_level",
}

PERM_COLUMNS = {
    "CASE_NUMBER": "case_number",
    "CASE_STATUS": "status",
    "RECEIVED_DATE": "received_date",
    "DECISION_DATE": "decision_date",
    "EMP_BUSINESS_NAME": "employer_name",
    "EMP_FEIN": "fein",
    "EMP_STATE": "employer_state",
    "JOB_TITLE": "job_title",
    "PWD_SOC_CODE": "soc_code",
    "PWD_SOC_TITLE": "soc_title",
    "PRIMARY_WORKSITE_CITY": "worksite_city",
    "PRIMARY_WORKSITE_STATE": "worksite_state",
    "JOB_OPP_WAGE_FROM": "wage_from",
    "JOB_OPP_WAGE_PER": "wage_unit",
}

USCIS_COUNTS = {
    "New Employment Approval": "new_employment_approved",
    "New Employment Denial": "new_employment_denied",
    "Continuation Approval": "continuation_approved",
    "Continuation Denial": "continuation_denied",
    "Change with Same Employer Approval": "same_employer_change_approved",
    "Change with Same Employer Denial": "same_employer_change_denied",
    "New Concurrent Approval": "concurrent_approved",
    "New Concurrent Denial": "concurrent_denied",
    "Change of Employer Approval": "change_of_employer_approved",
    "Change of Employer Denial": "change_of_employer_denied",
    "Amended Approval": "amended_approved",
    "Amended Denial": "amended_denied",
}

US_STATES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA",
    "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA",
    "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "PUERTO RICO": "PR", "GUAM": "GU", "VIRGIN ISLANDS": "VI",
}


def read_excel_cached(path: Path, cache_dir: Path) -> pd.DataFrame:
    """Excel parsing is the slow step; cache each file as Parquet keyed by its content hash."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    cached = cache_dir / f"{path.stem}.{digest.hexdigest()[:12]}.parquet"
    if cached.exists():
        return pd.read_parquet(cached)
    log.info("parsing %s", path.name)
    df = pd.read_excel(path, engine="calamine", dtype=str)
    cache_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cached, index=False)
    return df


def _text(s: pd.Series) -> pd.Series:
    s = s.astype("string").str.strip().str.replace(r"\s+", " ", regex=True)
    return s.mask(s == "")


def _number(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype("string").str.replace(r"[$,\s]", "", regex=True),
                         errors="coerce")


def _state(s: pd.Series) -> pd.Series:
    s = _text(s).str.upper()
    return s.map(lambda v: US_STATES.get(v, v) if isinstance(v, str) else v)


def annualise(amount: pd.Series, unit: pd.Series) -> pd.Series:
    factor = _text(unit).str.upper().map(ANNUAL_FACTOR)
    wage = amount * factor
    # Drop obvious keying errors (e.g. an hourly rate entered as annual).
    return wage.where(wage.between(15_000, 2_000_000))


def _standardise(df: pd.DataFrame, columns: dict[str, str], source: str) -> pd.DataFrame:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"{source}: missing columns {missing}")
    out = df[list(columns)].rename(columns=columns)
    for c in out.columns:
        out[c] = _text(out[c])
    out["status"] = out["status"].str.title()
    out["decision_date"] = pd.to_datetime(out["decision_date"], errors="coerce")
    out["received_date"] = pd.to_datetime(out["received_date"], errors="coerce")
    out["fein"] = out["fein"].map(clean_fein)
    out["employer_norm"] = out["employer_name"].map(normalise_employer)
    out["worksite_city"] = out["worksite_city"].str.upper()
    out["worksite_state"] = _state(out["worksite_state"])
    out["employer_state"] = _state(out["employer_state"])
    out["soc_code"] = out["soc_code"].str.replace(r"\.\d+$", "", regex=True)
    out["wage_annual"] = annualise(_number(out.pop("wage_from")), out.pop("wage_unit"))
    return out


def standardise_lca(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate LCA files, keep the latest decision per case number."""
    raw = pd.concat(frames, ignore_index=True)
    # Two FY2025 files spell the dependency flag differently; we don't keep it anyway.
    df = _standardise(raw, LCA_COLUMNS, "LCA")
    for c in ["workers", "new_employment", "continued_employment", "change_employer"]:
        df[c] = _number(df[c]).fillna(0).astype("int32")
    df["full_time"] = df["full_time"].str.upper().eq("Y")
    df = df.dropna(subset=["case_number", "decision_date", "employer_name"])
    df = df.sort_values("decision_date").drop_duplicates("case_number", keep="last")
    return df.reset_index(drop=True)


def standardise_perm(frames: list[pd.DataFrame]) -> pd.DataFrame:
    df = _standardise(pd.concat(frames, ignore_index=True), PERM_COLUMNS, "PERM")
    df = df.dropna(subset=["case_number", "decision_date", "employer_name"])
    df = df.sort_values("decision_date").drop_duplicates("case_number", keep="last")
    return df.reset_index(drop=True)


def read_uscis(path: Path) -> pd.DataFrame:
    """USCIS Employer Data Hub crosstab export: UTF-16, tab-separated."""
    raw_bytes = path.read_bytes()[:4]
    encoding = "utf-16" if raw_bytes[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
    sep = "\t" if encoding == "utf-16" else ","
    df = pd.read_csv(path, encoding=encoding, sep=sep, dtype=str)
    df.columns = [re.sub(r"\s+", " ", c).strip() for c in df.columns]
    out = pd.DataFrame({
        "fiscal_year": _number(df["Fiscal Year"]).astype("Int64"),
        "employer_name": _text(df["Employer (Petitioner) Name"]),
        "tax_id_last4": _text(df["Tax ID"]),
        "naics": _text(df["Industry (NAICS) Code"]),
        "city": _text(df["Petitioner City"]).str.upper(),
        "state": _state(df["Petitioner State"]),
    })
    for src, dst in USCIS_COUNTS.items():
        out[dst] = _number(df[src]).fillna(0).astype("int32")
    out = out.dropna(subset=["employer_name", "fiscal_year"])
    out = out[out["employer_name"].str.upper() != "NULL"]
    out["employer_norm"] = out["employer_name"].map(normalise_employer)
    return out.reset_index(drop=True)
