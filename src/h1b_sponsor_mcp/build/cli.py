"""`h1b-sponsor-build`: raw government files -> dist/sponsors.duckdb."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from h1b_sponsor_mcp.build.assemble import build, discover


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="h1b-sponsor-build")
    p.add_argument("--raw", type=Path, default=Path("data/raw"))
    p.add_argument("--cache", type=Path, default=Path("data/interim"))
    p.add_argument("--out", type=Path, default=Path("dist/sponsors.duckdb"))
    p.add_argument("--uscis-through", default="",
                   help='label for the USCIS export, e.g. "FY2026 Q3"')
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    inputs = discover(args.raw)
    inputs.uscis_through = args.uscis_through
    meta = build(inputs, args.out, args.cache)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
