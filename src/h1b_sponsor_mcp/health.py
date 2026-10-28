"""Checks that a sponsors database is safe to publish (or still fit to serve).

Used in two places:
- `make refresh` runs `quality_gates(new, old)` before anything is released.
- the weekly GitHub workflow runs `release_health(db)` against the published release.

Each check returns (name, passed, detail) so failures read as a short report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb

from h1b_sponsor_mcp.queries import CERTIFIED, Store, months_back

SCHEMA_VERSION = 1
# DOL publishes ~6 weeks after quarter end, so data is at most ~5 months old when the
# maintainer is on schedule. Past this, a refresh has been missed.
STALE_AFTER_DAYS = 200
BELLWETHERS = ("Microsoft", "Amazon", "Google")
BANNED_COLUMN_PARTS = ("email", "phone", "poc", "attorney", "preparer", "address", "postal")


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def _store(path: Path) -> Store:
    return Store(duckdb.connect(str(path), read_only=True))


def structural_checks(store: Store, bellwethers: tuple[str, ...] = BELLWETHERS,
                      min_filings: int = 100) -> list[Check]:
    con = store.con
    checks = []
    version = store.meta.get("schema_version")
    checks.append(Check("schema version", version == SCHEMA_VERSION,
                        f"{version} (expected {SCHEMA_VERSION})"))
    columns = [r[0].lower() for r in con.execute(
        "SELECT column_name FROM information_schema.columns").fetchall()]
    leaked = sorted({c for c in columns if any(b in c for b in BANNED_COLUMN_PARTS)})
    checks.append(Check("no contact details", not leaked, ", ".join(leaked) or "none"))
    unresolved = con.execute(
        "SELECT count(*) FILTER (WHERE employer_id IS NULL) * 1.0 / count(*) FROM lca"
    ).fetchone()[0]
    checks.append(Check("filings resolved to employers", unresolved < 0.001,
                        f"{unresolved:.3%} unresolved"))
    start = months_back(store.anchor, 6)
    missing = []
    for name in bellwethers:
        ids = store.match_employers(name)
        n = con.execute(
            f"SELECT count(*) FROM lca l WHERE l.employer_id IN (SELECT unnest(?)) "
            f"AND {CERTIFIED} AND l.decision_date BETWEEN ? AND ?",
            [ids, start, store.anchor]).fetchone()[0] if ids else 0
        if n < min_filings:
            missing.append(f"{name}={n}")
    checks.append(Check("bellwether employers present", not missing,
                        ", ".join(missing) or ", ".join(bellwethers) + " all active"))
    try:
        store.company_sponsorship(bellwethers[0], role="data scientist")
        store.compare_companies(list(bellwethers))
        store.top_sponsors(state="GA", limit=5)
        store.salary_range("software engineer", state="WA")
        store.find_employer("Deloitte")
        store.data_info()
        checks.append(Check("every tool answers", True, "6 tools ran"))
    except Exception as exc:  # pragma: no cover - the point is to report, not raise
        checks.append(Check("every tool answers", False, repr(exc)))
    return checks


def quality_gates(new_path: Path, old_path: Path | None, **kw) -> list[Check]:
    """Gates for a freshly built database, compared with the one currently published."""
    new = _store(new_path)
    checks = structural_checks(new, **kw)
    if old_path is None or not old_path.exists():
        checks.append(Check("compared with previous release", True, "no previous release"))
        return checks
    old = _store(old_path)
    checks.append(Check("data moved forward", new.anchor > old.anchor,
                        f"{old.anchor} -> {new.anchor}"))
    # Filings in the period both builds cover should barely change. A big drop means a
    # file went missing or was misread, not that history changed.
    lo, hi = max(new.data_from, old.data_from), min(new.anchor, old.anchor)
    q = "SELECT count(*) FROM lca WHERE decision_date BETWEEN ? AND ?"
    n_new = new.con.execute(q, [lo, hi]).fetchone()[0]
    n_old = old.con.execute(q, [lo, hi]).fetchone()[0]
    ratio = n_new / n_old if n_old else 0.0
    checks.append(Check("overlapping filings stable", 0.98 <= ratio <= 1.05,
                        f"{n_old:,} -> {n_new:,} for {lo}..{hi} ({ratio - 1:+.1%})"))
    e_new, e_old = new.meta["counts"]["employers"], old.meta["counts"]["employers"]
    change = e_new / e_old - 1 if e_old else 0.0
    checks.append(Check("employer count plausible", -0.15 <= change <= 0.25,
                        f"{e_old:,} -> {e_new:,} ({change:+.1%})"))
    return checks


def release_health(db_path: Path, today: date | None = None, **kw) -> list[Check]:
    store = _store(db_path)
    today = today or date.today()
    checks = structural_checks(store, **kw)
    age = (today - store.anchor).days
    checks.append(Check("data is current", age <= STALE_AFTER_DAYS,
                        f"data through {store.anchor} ({age} days old; "
                        f"refresh due when > {STALE_AFTER_DAYS})"))
    return checks


def report(checks: list[Check]) -> str:
    lines = [f"{'PASS' if c.passed else 'FAIL'}  {c.name}: {c.detail}" for c in checks]
    return "\n".join(lines)


def summary(db_path: Path, bellwethers: tuple[str, ...] = BELLWETHERS) -> dict:
    """The numbers worth eyeballing in a before/after diff."""
    store = _store(db_path)
    w_start = months_back(store.anchor, 6)
    big = {}
    for name in bellwethers:
        ids = store.match_employers(name)
        big[name] = store.con.execute(
            f"SELECT count(*) FROM lca l WHERE l.employer_id IN (SELECT unnest(?)) "
            f"AND {CERTIFIED} AND l.decision_date BETWEEN ? AND ?",
            [ids, w_start, store.anchor]).fetchone()[0]
    return {
        "data through": str(store.anchor),
        "last-6-month window": f"{w_start:%b %Y}–{store.anchor:%b %Y}",
        "LCA filings": store.meta["counts"]["lca"],
        "PERM cases": store.meta["counts"]["perm"],
        "USCIS rows": store.meta["counts"]["uscis_rows"],
        "employers": store.meta["counts"]["employers"],
        "USCIS through": store.meta.get("uscis_through", ""),
        **{f"{k} LCAs, last 6 mo": v for k, v in big.items()},
    }


def main() -> None:
    """`python -m h1b_sponsor_mcp.health [db]`: health of a local file or the release."""
    import sys

    from h1b_sponsor_mcp.data import locate

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else locate(refresh=True)
    checks = release_health(path)
    print(report(checks))
    print(json.dumps(summary(path), indent=2))
    sys.exit(0 if all(c.passed for c in checks) else 1)


if __name__ == "__main__":
    main()

