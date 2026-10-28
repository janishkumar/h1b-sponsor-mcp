"""Resolve employer names into employers.

Normalised names that share a federal tax ID (FEIN) in DOL filings are the same legal
employer, whatever the spelling ("AMAZON.COM SERVICES LLC" / "Amazon.com Services, Inc.").
USCIS rows only carry the last four digits of the tax ID, so they join by normalised name.

A FEIN that links dozens of unrelated names is almost always a payroll provider or a
keying error, so such FEINs are ignored rather than allowed to merge everything.
"""

from __future__ import annotations

import hashlib

import pandas as pd

MAX_NAMES_PER_FEIN = 12


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def resolve(dol_names: pd.DataFrame, uscis_names: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (name_map, employers).

    dol_names:   columns employer_norm, employer_name, fein (one row per filing)
    uscis_names: employer_norm values from USCIS
    name_map:    employer_norm -> employer_id
    employers:   employer_id, display_name, n_names, n_feins
    """
    filings = (
        dol_names.dropna(subset=["fein"])
        .loc[lambda d: d["employer_norm"] != ""]
        .groupby(["fein", "employer_norm"]).size().rename("n").reset_index()
    )
    names_per_fein = filings.groupby("fein")["employer_norm"].nunique()
    usable = names_per_fein[names_per_fein <= MAX_NAMES_PER_FEIN].index
    filings = filings[filings["fein"].isin(usable)]
    # A name joins a FEIN's cluster only if its first word matches the name that files
    # most under that FEIN. "AMAZON WEB SERVICES" joins "AMAZON COM SERVICES"; a small
    # firm that keyed Amazon's FEIN by mistake ("TECHCORP SOLUTIONS") does not.
    dominant = (filings.sort_values(["fein", "n", "employer_norm"],
                                    ascending=[True, False, True])
                .drop_duplicates("fein").set_index("fein")["employer_norm"])
    first_word = filings["employer_norm"].str.split(" ").str[0]
    dominant_first = filings["fein"].map(dominant).str.split(" ").str[0]
    pairs = filings.loc[first_word == dominant_first, ["employer_norm", "fein"]]

    uf = _UnionFind()
    all_norms = set(dol_names["employer_norm"].dropna()) | set(uscis_names.dropna())
    all_norms.discard("")
    for n in all_norms:
        uf.find("N:" + n)
    for n, f in pairs.itertuples(index=False):
        uf.union("N:" + n, "F:" + f)

    name_map = pd.DataFrame({"employer_norm": sorted(all_norms)})
    name_map["root"] = name_map["employer_norm"].map(lambda n: uf.find("N:" + n))

    # Display name: the raw spelling filed most often across the employer's names.
    counts = (
        dol_names.dropna(subset=["employer_norm"])
        .groupby(["employer_norm", "employer_name"]).size().rename("n").reset_index()
        .merge(name_map, on="employer_norm")
    )
    display = (
        counts.sort_values(["root", "n", "employer_name"], ascending=[True, False, True])
        .drop_duplicates("root").set_index("root")["employer_name"]
    )
    name_map["display_name"] = name_map["root"].map(display)
    name_map["display_name"] = name_map["display_name"].fillna(name_map["employer_norm"])
    name_map["employer_id"] = name_map["root"].map(
        lambda r: "E" + hashlib.sha1(r.encode()).hexdigest()[:10]
    )
    fein_counts = pairs.assign(root=pairs["employer_norm"].map(lambda n: uf.find("N:" + n)))
    n_feins = fein_counts.groupby("root")["fein"].nunique()
    employers = (
        name_map.groupby(["employer_id", "root", "display_name"], as_index=False)
        .agg(n_names=("employer_norm", "nunique"))
    )
    employers["n_feins"] = employers["root"].map(n_feins).fillna(0).astype(int)
    return (
        name_map[["employer_norm", "employer_id"]],
        employers.drop(columns="root"),
    )
