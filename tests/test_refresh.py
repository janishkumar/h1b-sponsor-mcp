import gzip
import subprocess
from datetime import date

import pytest

from h1b_sponsor_mcp.build import refresh
from tests.conftest import build_fixture_db, lca_row

FIXTURE_BIG = {"bellwethers": ("Acme Cloud", "Steady Corp"), "min_filings": 1}


def test_collect_recognises_dol_and_uscis_files(tmp_path):
    dl, raw, arc = tmp_path / "Downloads", tmp_path / "raw", tmp_path / "archive"
    dl.mkdir()
    for name in ["LCA_Dislclosure_Data_FY2026_Q3.xlsx",  # DOL's own typo
                 "PERM_Disclosure_Data_FY2025.xlsx", "Employer Information (1).csv",
                 "resume.pdf", "LCA_Appendix_A_FY2025_Q4.xlsx"]:
        (dl / name).write_text("new")
    (raw / "uscis").mkdir(parents=True)
    (raw / "uscis" / "employer_information.csv").write_text("old")

    moved = refresh.collect(dl, raw, arc)
    assert moved == ["employer_information.csv", "LCA_Disclosure_Data_FY2026_Q3.xlsx",
                     "PERM_Disclosure_Data_FY2025.xlsx"]
    assert (raw / "uscis" / "employer_information.csv").read_text() == "new"
    assert (arc / "employer_information.csv").read_text() == "old"  # replaced, not lost
    assert (dl / "resume.pdf").exists() and (dl / "LCA_Appendix_A_FY2025_Q4.xlsx").exists()


def test_retention_keeps_three_fiscal_years(tmp_path):
    raw, arc = tmp_path / "raw", tmp_path / "archive"
    (raw / "dol").mkdir(parents=True)
    for fy in (2024, 2025, 2026, 2027):
        (raw / "dol" / f"LCA_Disclosure_Data_FY{fy}_Q1.xlsx").write_text("x")
    assert refresh.apply_retention(raw, arc) == ["LCA_Disclosure_Data_FY2024_Q1.xlsx"]
    assert len(list((raw / "dol").iterdir())) == 3


def _setup(tmp_path, monkeypatch, newer: bool):
    dl, raw, dist = tmp_path / "Downloads", tmp_path / "raw", tmp_path / "dist"
    dl.mkdir()
    dist.mkdir()
    (dl / "LCA_Disclosure_Data_FY2026_Q4.xlsx").write_text("x")
    build_fixture_db(dist / "sponsors.duckdb")
    extra = [lca_row("I-Q4-1", date(2026, 9, 30), "Steady Corp", fein="34-5678901")] \
        if newer else None
    monkeypatch.setattr(refresh, "build",
                        lambda inputs, out, cache: build_fixture_db(out, extra_lca=extra))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, check: calls.append(cmd))
    return dl, raw, dist, calls


def test_refresh_refuses_when_gates_fail(tmp_path, monkeypatch):
    dl, raw, dist, calls = _setup(tmp_path, monkeypatch, newer=False)
    code = refresh.refresh(dl, raw, dist, tmp_path, "FY2026 Q3", dry_run=False,
                           assume_yes=True, gate_kwargs=FIXTURE_BIG)
    assert code == 1 and calls == []
    assert (dist / "sponsors.next.duckdb").exists()  # kept for inspection


def test_refresh_publishes_when_gates_pass(tmp_path, monkeypatch):
    dl, raw, dist, calls = _setup(tmp_path, monkeypatch, newer=True)
    code = refresh.refresh(dl, raw, dist, tmp_path, "FY2026 Q4", dry_run=False,
                           assume_yes=True, gate_kwargs=FIXTURE_BIG)
    assert code == 0
    assert calls and calls[0][:4] == ["gh", "release", "create", "data-2026-09-30"]
    with gzip.open(dist / "sponsors.duckdb.gz") as fh:
        assert fh.read(4)  # a real gzip of the new database


def test_refresh_dry_run_never_publishes(tmp_path, monkeypatch):
    dl, raw, dist, calls = _setup(tmp_path, monkeypatch, newer=True)
    assert refresh.refresh(dl, raw, dist, tmp_path, "FY2026 Q4", dry_run=True,
                           assume_yes=True, gate_kwargs=FIXTURE_BIG) == 0
    assert calls == []


def test_new_uscis_file_needs_a_label_when_unattended(tmp_path, monkeypatch):
    dl, raw, dist, calls = _setup(tmp_path, monkeypatch, newer=True)
    (dl / "Employer Information.csv").write_text("x")
    assert refresh.refresh(dl, raw, dist, tmp_path, None, dry_run=False,
                           assume_yes=True, gate_kwargs=FIXTURE_BIG) == 1
    assert calls == []


@pytest.mark.parametrize("name,canonical", [
    ("LCA_Dislclosure_Data_FY2026_Q3.xlsx", "LCA_Disclosure_Data_FY2026_Q3.xlsx"),
    ("perm_disclosure_data_fy2025_q4.xlsx", "PERM_Disclosure_Data_FY2025_Q4.xlsx"),
])
def test_canonical_names(name, canonical):
    assert refresh._canonical_dol_name(name) == canonical
