"""MCP server: H-1B and green-card sponsorship by employer, from public DOL and USCIS data."""

from __future__ import annotations

import logging
from functools import lru_cache

import duckdb
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from h1b_sponsor_mcp.data import DataUnavailable, locate
from h1b_sponsor_mcp.queries import Store

mcp = MCPServer(
    "h1b-sponsor",
    instructions=(
        "Answers 'is this company still sponsoring H-1B visas / green cards, and how much?' "
        "from public US Department of Labor and USCIS filing data. Tools return markdown "
        "tables; show them to the user as-is. Every table states its date window. This is "
        "public filing data, not legal advice."
    ),
)


READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


@lru_cache(maxsize=1)
def store() -> Store:
    return Store(duckdb.connect(str(locate()), read_only=True))


def _run(fn, *args, **kwargs) -> str:
    try:
        return fn(store(), *args, **kwargs)
    except DataUnavailable as exc:
        return f"Data unavailable: {exc}"


@mcp.tool(annotations=READ_ONLY)
def company_sponsorship(company: str, role: str | None = None, state: str | None = None,
                        city: str | None = None) -> str:
    """Is a company still sponsoring H-1Bs, and how much? Returns trend tables.

    Shows certified H-1B LCA filings for the last 6 months, the fiscal year so far and the
    last full fiscal year (each vs the same period a year earlier), split into new
    employment and transfers; green-card (PERM) certifications; USCIS petition
    approvals/denials by fiscal year; and a denial-rate flag (Normal / Elevated / High)
    relative to the national rate. Point out a High or Elevated flag to the user.

    Args:
        company: Employer name as people say it, e.g. "Microsoft", "Amazon", "JPMorgan".
        role: Optional job filter, e.g. "data scientist", "machine learning",
            "software engineer". Matches job title and occupation title.
        state: Optional two-letter worksite state, e.g. "GA".
        city: Optional worksite city, e.g. "Atlanta".
    """
    return _run(Store.company_sponsorship, company, role, state, city)


@mcp.tool(annotations=READ_ONLY)
def compare_companies(companies: list[str], role: str | None = None,
                      state: str | None = None, city: str | None = None) -> str:
    """Compare employers side by side: filings, trend, green cards, denial-rate flag, verdict.

    Args:
        companies: Two or more employer names, e.g. ["Stripe", "Databricks", "Snowflake"].
        role: Optional job filter, e.g. "data scientist".
        state: Optional two-letter worksite state.
        city: Optional worksite city.
    """
    return _run(Store.compare_companies, companies[:25], role, state, city)


@mcp.tool(annotations=READ_ONLY)
def top_sponsors(role: str | None = None, state: str | None = None, city: str | None = None,
                 months: int = 12, limit: int = 25) -> str:
    """Rank employers by certified H-1B filings (with denial-rate flags), for a role and place.

    Args:
        role: Optional job filter, e.g. "data scientist", "data engineer".
        state: Optional two-letter worksite state, e.g. "GA".
        city: Optional worksite city, e.g. "Atlanta".
        months: Look-back window in whole months ending at the latest data (1-21).
        limit: Number of employers to return (max 100).
    """
    return _run(Store.top_sponsors, role, state, city, months, limit)


@mcp.tool(annotations=READ_ONLY)
def salary_range(role: str, state: str | None = None, city: str | None = None,
                 company: str | None = None) -> str:
    """Offered H-1B wages for a role (percentiles and by experience level), last 12 months.

    Args:
        role: Job, e.g. "data scientist", "software engineer".
        state: Optional two-letter worksite state.
        city: Optional worksite city.
        company: Optional employer to restrict to.
    """
    return _run(Store.salary_range, role, state, city, company)


@mcp.tool(annotations=READ_ONLY)
def find_employer(query: str) -> str:
    """List the legal employer entities a name matches, so you can pick an exact one.

    Args:
        query: Start of the employer name, e.g. "Deloitte" or "Amazon Web Services".
    """
    return _run(Store.find_employer, query)


@mcp.tool(annotations=READ_ONLY)
def data_info() -> str:
    """Which data the answers come from, the dates covered, and the caveats."""
    return _run(Store.data_info)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
