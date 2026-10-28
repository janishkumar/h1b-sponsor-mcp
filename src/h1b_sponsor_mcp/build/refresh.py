"""`make refresh`: new government files in ~/Downloads -> gated, published release.

The one step that can't be automated is downloading the files: DOL and USCIS refuse
scripted downloads and DOL's data API doesn't carry this data. Everything after that is
here:

1. collect   move recognised files from Downloads into data/raw (archiving any file
             they replace) and retire fiscal years older than the retention window
2. build     rebuild the database next to the published one
3. gate      run quality gates against the currently published database
4. diff      print before/after numbers
5. publish   only if every gate passed and you confirm: gzip + GitHub release
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb

from h1b_sponsor_mcp.build.assemble import build, discover
from h1b_sponsor_mcp.health import quality_gates, report, summary

log = logging.getLogger(__name__)

DOL_FILE = re.compile(r"^(LCA|PERM)_Dis\w*?_Data_FY(\d{4})(?:_Q(\d))?\.xlsx$", re.I)
USCIS_FILE = re.compile(r"^Employer Information.*\.csv$", re.I)
KEEP_FISCAL_YEARS = 3  # current + two prior, so "vs a year earlier" always has data


def _canonical_dol_name(name: str) -> str:
    m = DOL_FILE.match(name)
    program, fy, q = m.group(1).upper(), m.group(2), m.group(3)
    return f"{program}_Disclosure_Data_FY{fy}" + (f"_Q{q}" if q else "") + ".xlsx"


def collect(downloads: Path, raw: Path, archive: Path) -> list[str]:
    """Move recognised files into data/raw. Returns what moved."""
    moved = []
    for f in sorted(downloads.iterdir()):
        if f.is_dir() or f.name.startswith("."):
            continue
        if DOL_FILE.match(f.name):
            dest = raw / "dol" / _canonical_dol_name(f.name)
        elif USCIS_FILE.match(f.name):
            dest = raw / "uscis" / "employer_information.csv"
        else:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), archive / dest.name)
        shutil.move(str(f), dest)
        moved.append(dest.name)
    return moved


def apply_retention(raw: Path, archive: Path, keep: int = KEEP_FISCAL_YEARS) -> list[str]:
    files = [(f, int(DOL_FILE.match(f.name).group(2)))
             for f in (raw / "dol").glob("*.xlsx") if DOL_FILE.match(f.name)]
    if not files:
        return []
    oldest_kept = max(fy for _, fy in files) - keep + 1
    retired = []
    for f, fy in files:
        if fy < oldest_kept:
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), archive / f.name)
            retired.append(f.name)
    return retired


def _meta(db: Path, key: str):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return json.loads(con.execute("SELECT value FROM meta WHERE key = ?", [key])
                          .fetchone()[0])
    finally:
        con.close()


def _fmt(x) -> str:
    return f"{x:,}" if isinstance(x, int) else str(x)


def diff_table(old: Path | None, new: Path, **kw) -> str:
    after = summary(new, **kw)
    before = summary(old, **kw) if old and old.exists() else {}
    width = max(len(k) for k in after)
    lines = [f"{'':<{width}}  {'published':>22}  {'new build':>22}"]
    for k, v in after.items():
        lines.append(f"{k:<{width}}  {_fmt(before.get(k, '—')):>22}  {_fmt(v):>22}")
    return "\n".join(lines)


def publish(db: Path, repo: str | None = None) -> str:
    through = _meta(db, "lca_through")
    uscis = _meta(db, "uscis_through")
    tag = f"data-{through}"
    gz = db.with_suffix(".duckdb.gz")
    with db.open("rb") as src, gzip.open(gz, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst, length=1 << 20)
    cmd = ["gh", "release", "create", tag, str(gz), "--title", f"Data through {through}",
           "--notes", f"DOL LCA and PERM decisions through {through}; USCIS Employer "
           f"Data Hub through {uscis}. Built {date.today()}."]
    if repo:
        cmd += ["--repo", repo]
    subprocess.run(cmd, check=True)
    return tag


def refresh(downloads: Path, raw: Path, dist: Path, cache: Path, uscis_through: str | None,
            dry_run: bool, assume_yes: bool, rebuild: bool = False,
            gate_kwargs: dict | None = None) -> int:
    gate_kwargs = gate_kwargs or {}
    archive = raw / "archive" / date.today().isoformat()
    published = dist / "sponsors.duckdb"
    candidate = dist / "sponsors.next.duckdb"

    moved = collect(downloads, raw, archive)
    print("Collected:", ", ".join(moved) if moved else "nothing new in " + str(downloads))
    if not moved and not rebuild:
        print("Download the new files first (see README), or pass --rebuild.")
        return 1
    retired = apply_retention(raw, archive)
    if retired:
        print("Retired (older than retention window):", ", ".join(retired))

    if uscis_through is None:
        new_uscis = any(n == "employer_information.csv" for n in moved)
        previous = _meta(published, "uscis_through") if published.exists() else ""
        if new_uscis or not previous:
            if assume_yes:
                print("A new USCIS file needs a label: pass --uscis-through 'FY2026 Q4'.")
                return 1
            uscis_through = input("USCIS data hub covers through (e.g. FY2026 Q4): ").strip()
        else:
            uscis_through = previous

    inputs = discover(raw)
    inputs.uscis_through = uscis_through
    build(inputs, candidate, cache)

    print("\n" + diff_table(published, candidate,
                             **{k: v for k, v in gate_kwargs.items() if k == "bellwethers"})
          + "\n")
    checks = quality_gates(candidate, published, **gate_kwargs)
    print(report(checks) + "\n")
    if not all(c.passed for c in checks):
        print("Gates failed: nothing published. The candidate is at", candidate)
        return 1
    if dry_run:
        print("Dry run: gates passed, nothing published.")
        return 0
    if not assume_yes and input("Publish this release? [y/N] ").strip().lower() != "y":
        print("Not published. The candidate is at", candidate)
        return 1
    candidate.replace(published)
    print("Published", publish(published))
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="h1b-sponsor-refresh")
    sub = p.add_subparsers(dest="cmd")
    p.add_argument("--downloads", type=Path, default=Path.home() / "Downloads")
    p.add_argument("--raw", type=Path, default=Path("data/raw"))
    p.add_argument("--dist", type=Path, default=Path("dist"))
    p.add_argument("--cache", type=Path, default=Path("data/interim"))
    p.add_argument("--uscis-through", default=None)
    p.add_argument("--dry-run", action="store_true", help="gate and diff, don't publish")
    p.add_argument("--yes", action="store_true", help="publish without asking")
    p.add_argument("--rebuild", action="store_true", help="rebuild even with no new files")
    sub.add_parser("publish", help="publish dist/sponsors.duckdb as-is")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "publish":
        print("Published", publish(args.dist / "sponsors.duckdb"))
        return
    sys.exit(refresh(args.downloads, args.raw, args.dist, args.cache, args.uscis_through,
                     args.dry_run, args.yes, args.rebuild))


if __name__ == "__main__":
    main()
