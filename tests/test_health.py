from datetime import date

from h1b_sponsor_mcp import health
from tests.conftest import build_fixture_db, lca_row

FIXTURE_BIG = {"bellwethers": ("Acme Cloud", "Steady Corp"), "min_filings": 1}


def _names(checks, passed):
    return {c.name for c in checks if c.passed is passed}


def test_structural_checks_pass_on_fixture(db_path):
    store = health._store(db_path)
    checks = health.structural_checks(store, **FIXTURE_BIG)
    assert not _names(checks, False), health.report(checks)


def test_missing_bellwether_is_caught(db_path):
    checks = health.structural_checks(health._store(db_path), bellwethers=("Microsoft",))
    assert "bellwether employers present" in _names(checks, False)


def test_release_health_flags_stale_data(db_path):
    fresh = health.release_health(db_path, today=date(2026, 10, 28), **FIXTURE_BIG)
    stale = health.release_health(db_path, today=date(2027, 3, 1), **FIXTURE_BIG)
    assert "data is current" in _names(fresh, True)
    assert "data is current" in _names(stale, False)


def test_gates_require_new_data(db_path, tmp_path):
    same = build_fixture_db(tmp_path / "same.duckdb")
    checks = health.quality_gates(same, db_path, **FIXTURE_BIG)
    assert _names(checks, False) == {"data moved forward"}


def test_gates_pass_for_a_genuine_new_quarter(db_path, tmp_path):
    newer = build_fixture_db(tmp_path / "newer.duckdb", extra_lca=[
        lca_row(f"I-Q4-{i}", date(2026, 8, 1 + i), "Steady Corp", fein="34-5678901")
        for i in range(5)])
    checks = health.quality_gates(newer, db_path, **FIXTURE_BIG)
    assert all(c.passed for c in checks), health.report(checks)


def test_gates_catch_a_missing_file(db_path, tmp_path):
    # New quarter arrives, but a whole earlier file's worth of filings went missing.
    broken = build_fixture_db(tmp_path / "broken.duckdb", drop_case_prefix="I-ACME", extra_lca=[
        lca_row("I-Q4-1", date(2026, 8, 1), "Steady Corp", fein="34-5678901")])
    checks = health.quality_gates(broken, db_path, **FIXTURE_BIG)
    assert "overlapping filings stable" in _names(checks, False)
