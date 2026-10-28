# h1b-sponsor-mcp — design

Date: 2026-10-28. Author: Janish Kumar.

## Intent

A job seeker who will need visa sponsorship looks at a role and asks: *is this company still sponsoring, and how much?* The answer exists in public government data, but it's spread across three agencies, published as gigabyte Excel files, and full of inconsistent employer names. Existing tools show one source, one period, or lifetime totals, and none answer "still?".

This MCP server lets Claude answer that question with readable tables, from a local database that refreshes each quarter.

## Users and questions

- International students and H-1B holders job hunting: "Is Microsoft still sponsoring?", "Compare Stripe, Databricks and Snowflake", "Who sponsors data scientists in Georgia?", "What do H-1B data scientists earn in Atlanta?"

## Goals

1. Per-company sponsorship summary across three signals:
   - DOL LCA filings (freshest; the 6-month view),
   - USCIS H-1B petition approvals and denials (confirmed),
   - DOL PERM green-card certifications (long-term sponsorship).
2. Trend windows anchored on the data's end date: last 6 months vs. the same months a year earlier, fiscal-year-to-date vs. the same span last year, last full fiscal year.
3. Optional role and location filters on every tool.
4. Employer names resolved so "Amazon" finds all of Amazon's legal entities, with the entities listed so the user can see what was counted.
5. Output as markdown tables, every one stating its date window and what the source does and doesn't mean.
6. Install with one command; data downloaded once and cached; works offline.

## Non-goals

- Legal or immigration advice. USCIS case status (needs personal data). Visa bulletin and processing times (later).
- Hosting a server. It runs locally inside the user's MCP client.

## Anti-goals

- Presenting LCAs as hires. An LCA is a prerequisite filing; it covers new hires, renewals and transfers, and some never become petitions. Every table says so.
- Shipping personal data. Source files include employer contacts' names, emails and attorneys. The published database drops all of it.
- Undated numbers. Every table names its window ("Jan–Jun 2026"), never just "last 6 months".

## Data

| Source | Grain | Coverage used | Notes |
|---|---|---|---|
| DOL LCA disclosure | one row per LCA | decisions Oct 2024 – Jun 2026 | FY2025 published per quarter; current FY published cumulatively. De-duplicated by case number. |
| DOL PERM disclosure | one row per case | decisions Oct 2024 – Jun 2026 | |
| USCIS H-1B Employer Data Hub | employer × fiscal year | FY2023 – FY2026 Q3 | Tableau crosstab export (UTF-16, tab-separated). Approvals/denials split by petition type: new employment, continuation, change of employer, etc. |

DOL and USCIS block scripted downloads, so the raw files are fetched in a browser by the maintainer (about five minutes a quarter). Users never touch them: they download the pre-built database from the repo's GitHub release.

## Decisions

- **Windows use decision date.** It's what the files are partitioned by, so a window is never half-covered. LCAs are decided within days of filing, so it tracks filing date closely.
- **"Certified LCAs"** = status Certified or Certified-Withdrawn, visa class H-1B. New-employment and change-of-employer counts come from the LCA's own fields.
- **Employer resolution:** normalise names (case, punctuation, legal suffixes), then union names that share a federal tax ID (FEIN) in DOL data. USCIS rows (last four digits of tax ID only) attach by normalised name. Queries match on whole leading words ("amazon" → "AMAZON.COM SERVICES", "AMAZON WEB SERVICES"), and the matched entities are shown.
- **"Still sponsoring?" verdict** from the last-6-month vs prior-year LCA counts: active / occasional / slowing sharply / no filings in the last 6 months / no recent sponsorship. Rules are in code and in the README.
- **Wages** annualised (hour×2080, week×52, bi-weekly×26, month×12) from the LCA's offered wage.
- **Stack:** Python, official MCP SDK (FastMCP), DuckDB. The runtime installs only `mcp`, `duckdb`, `platformdirs`; pandas/calamine are a `build` extra for the maintainer.
- **Distribution:** `uvx --from git+https://github.com/janishkumar/h1b-sponsor-mcp h1b-sponsor-mcp`. Database is a GitHub release asset; the server checks for a newer release at most once a day. `H1B_SPONSOR_DB` points at a local file instead.

## Tools

| Tool | Answers |
|---|---|
| `company_sponsorship(company, role?, state?, city?)` | Summary tables for one employer |
| `compare_companies(companies, role?, state?, city?)` | Side-by-side table with verdicts |
| `top_sponsors(role?, state?, city?, months=12, limit=25)` | Ranked employers |
| `salary_range(role, state?, city?, company?)` | Wage percentiles from certified LCAs |
| `find_employer(query)` | Which legal entities a name matches |
| `data_info()` | Sources, coverage dates, caveats |

## Left open

Exact role-alias list, table column order, test fixtures.
