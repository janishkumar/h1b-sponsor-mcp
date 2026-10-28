"""Turn the raw files into the single DuckDB file the server ships with."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pandas as pd

from h1b_sponsor_mcp.build.employers import resolve
from h1b_sponsor_mcp.build.sources import (
    read_excel_cached,
    read_uscis,
    standardise_lca,
    standardise_perm,
)

log = logging.getLogger(__name__)
SCHEMA_VERSION = 1


@dataclass
class BuildInputs:
    lca_files: list[Path]
    perm_files: list[Path]
    uscis_file: Path | None
    uscis_through: str = ""


def discover(raw_dir: Path) -> BuildInputs:
    dol = raw_dir / "dol"
    uscis = sorted((raw_dir / "uscis").glob("*.csv"))
    return BuildInputs(
        lca_files=sorted(p for p in dol.glob("*.xlsx") if p.name.upper().startswith("LCA_")),
        perm_files=sorted(p for p in dol.glob("*.xlsx") if p.name.upper().startswith("PERM_")),
        uscis_file=uscis[-1] if uscis else None,
    )


def build(inputs: BuildInputs, out_path: Path, cache_dir: Path) -> dict:
    if not inputs.lca_files:
        raise SystemExit("no LCA files found")
    lca = standardise_lca([read_excel_cached(p, cache_dir) for p in inputs.lca_files])
    perm = standardise_perm([read_excel_cached(p, cache_dir) for p in inputs.perm_files]) \
        if inputs.perm_files else None
    uscis = read_uscis(inputs.uscis_file) if inputs.uscis_file else None
    log.info("lca %d, perm %d, uscis %d rows", len(lca), 0 if perm is None else len(perm),
             0 if uscis is None else len(uscis))
    return write_database(lca, perm, uscis, out_path, inputs)


def write_database(lca: pd.DataFrame, perm: pd.DataFrame | None, uscis: pd.DataFrame | None,
                   out_path: Path, inputs: BuildInputs) -> dict:
    """Resolve employers across sources and write the published DuckDB file."""
    dol_names = pd.concat(
        [f[["employer_norm", "employer_name", "fein"]] for f in (lca, perm) if f is not None],
        ignore_index=True,
    )
    name_map, employers = resolve(
        dol_names, uscis["employer_norm"] if uscis is not None else pd.Series(dtype=str)
    )
    ids = name_map.set_index("employer_norm")["employer_id"]
    lca["employer_id"] = lca["employer_norm"].map(ids)
    if perm is not None:
        perm["employer_id"] = perm["employer_norm"].map(ids)
    if uscis is not None:
        uscis["employer_id"] = uscis["employer_norm"].map(ids)

    meta = {
        "schema_version": SCHEMA_VERSION,
        "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lca_from": str(lca["decision_date"].min().date()),
        "lca_through": str(lca["decision_date"].max().date()),
        "lca_files": [p.name for p in inputs.lca_files],
        "perm_from": "" if perm is None else str(perm["decision_date"].min().date()),
        "perm_through": "" if perm is None else str(perm["decision_date"].max().date()),
        "perm_files": [p.name for p in inputs.perm_files],
        "uscis_fiscal_years": [] if uscis is None
        else sorted(int(y) for y in uscis["fiscal_year"].unique()),
        "uscis_through": inputs.uscis_through,
        "counts": {
            "lca": len(lca),
            "perm": 0 if perm is None else len(perm),
            "uscis_rows": 0 if uscis is None else len(uscis),
            "employers": len(employers),
            "employer_names": len(name_map),
        },
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    con = duckdb.connect(str(out_path))
    tables = {"lca": lca, "employers": employers, "employer_names": name_map}
    if perm is not None:
        tables["perm"] = perm
    if uscis is not None:
        tables["uscis"] = uscis
    for name, df in tables.items():
        con.register("tmp", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM tmp")
        con.unregister("tmp")
    con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
    con.executemany("INSERT INTO meta VALUES (?, ?)",
                    [(k, json.dumps(v)) for k, v in meta.items()])
    # Indexes for the lookups the server makes on every call.
    con.execute("CREATE INDEX lca_emp ON lca(employer_id)")
    con.execute("CREATE INDEX names_norm ON employer_names(employer_norm)")
    con.execute("CHECKPOINT")
    con.close()
    return meta
