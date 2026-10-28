"""Answers to the questions the tools expose, as markdown.

All windows are anchored on the newest decision date in the data, not on today, so a
"last 6 months" figure is always fully covered by the published files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta

import duckdb

from h1b_sponsor_mcp.names import query_key
from h1b_sponsor_mcp.render import fmt_int, fmt_money, fmt_pct, table

CERTIFIED = "l.status LIKE 'Certified%' AND l.visa_class = 'H-1B'"
PERM_CERTIFIED = "p.status LIKE 'Certified%'"

ROLE_PATTERNS = {
    "data scientist": ["%data scien%"],
    "data science": ["%data scien%"],
    "machine learning": ["%machine learning%", "%ml engineer%", "%mlops%"],
    "ml engineer": ["%machine learning%", "%ml engineer%", "%mlops%"],
    "ai engineer": ["%ai engineer%", "%artificial intelligence%", "%genai%", "%ai/ml%"],
    "data engineer": ["%data engineer%"],
    "data analyst": ["%data analyst%"],
    "analytics": ["%analytics%", "%data analyst%"],
    "software engineer": ["%software engineer%", "%software developer%", "%software development%"],
    "business analyst": ["%business analyst%"],
    "product manager": ["%product manager%"],
}

USCIS_APPROVED = (
    "new_employment_approved + continuation_approved + same_employer_change_approved"
    " + concurrent_approved + change_of_employer_approved + amended_approved"
)
USCIS_DENIED = USCIS_APPROVED.replace("_approved", "_denied")

# Denial-rate flags, relative to the national rate over the same fiscal years.
# Calibrated on FY2025-26: national 2.5%, employer median 1.6% (20+ decisions). These
# rules flag 5.5% of those employers High and a further 3.1% Elevated.
# Signals (kanban): every verdict and flag leads with one of these so a table reads
# at a glance. The word after it carries the same meaning for anyone without emoji.
GREEN, AMBER, RED, GREY = "🟢", "🟡", "🔴", "⚪"
DENIAL_HIGH = f"{RED} High"
DENIAL_ELEVATED = f"{AMBER} Elevated"
DENIAL_NORMAL = f"{GREEN} Normal"
DENIAL_FEW = f"{GREY} Too few to judge"
SIGNAL_LEGEND = (f"Signals: {GREEN} good · {AMBER} caution · {RED} warning · "
                 f"{GREY} not enough data.")

HIGH_MULTIPLE = 3.0
ELEVATED_MULTIPLE = 2.0
MIN_DECISIONS = 20
MIN_DENIALS = 3

LCA_NOTE = (
    "LCA = Labor Condition Application, the DOL filing an employer makes before an H-1B "
    "petition. It covers new hires, renewals and transfers, and some never become "
    "petitions, so treat counts as an upper bound on sponsorship activity."
)


# --- dates -------------------------------------------------------------------------------

def _shift_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    last = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30,
            31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def fiscal_year(d: date) -> int:
    return d.year + 1 if d.month >= 10 else d.year


def _label(start: date, end: date) -> str:
    if start.year == end.year:
        return f"{start:%b}–{end:%b %Y}"
    return f"{start:%b %Y}–{end:%b %Y}"


@dataclass(frozen=True)
class Window:
    name: str
    start: date
    end: date
    prior_start: date | None
    prior_end: date | None

    @property
    def label(self) -> str:
        return _label(self.start, self.end)


def months_back(anchor: date, n: int) -> date:
    """First day of the calendar month n-1 months before the anchor's month.

    Windows are whole calendar months ending on the anchor, so "last 6 months" from
    Jun 30 is Jan 1–Jun 30, never Dec 31–Jun 30.
    """
    return _shift_months(date(anchor.year, anchor.month, 1), -(n - 1))


def windows(anchor: date, data_from: date) -> list[Window]:
    six_start = months_back(anchor, 6)
    fy = fiscal_year(anchor)
    fy_start = date(fy - 1, 10, 1)
    last_fy_start, last_fy_end = date(fy - 2, 10, 1), date(fy - 1, 9, 30)

    def prior(s: date, e: date) -> tuple[date | None, date | None]:
        ps = _shift_months(s, -12)
        pe = _shift_months(date(e.year, e.month, 1), -11) - timedelta(days=1)
        if e != _shift_months(date(e.year, e.month, 1), 1) - timedelta(days=1):
            pe = _shift_months(e, -12)  # anchor mid-month: same day a year earlier
        return (ps, pe) if ps >= data_from else (None, None)

    out = [Window("Last 6 months", six_start, anchor, *prior(six_start, anchor)),
           Window(f"FY{fy} so far", fy_start, anchor, *prior(fy_start, anchor))]
    if last_fy_start >= data_from:
        out.append(Window(f"FY{fy - 1}", last_fy_start, last_fy_end,
                          *prior(last_fy_start, last_fy_end)))
    return out


# --- the store ---------------------------------------------------------------------------

class Store:
    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        self.meta = {k: json.loads(v) for k, v in meta.items()}
        self.anchor = date.fromisoformat(self.meta["lca_through"])
        self.data_from = date.fromisoformat(self.meta["lca_from"])
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        self.has_perm = "perm" in tables
        self.has_uscis = "uscis" in tables
        if self.has_uscis:
            # Denial rates use the last full fiscal year plus the current one so far:
            # recent enough to matter, enough volume to mean something.
            self.denial_fys = sorted(self.meta.get("uscis_fiscal_years") or [])[-2:]
            self.national = self._denial_counts(None)

    # USCIS denial rates ----------------------------------------------------------------
    def _denial_counts(self, ids: list[str] | None) -> dict:
        emp_sql = "AND employer_id IN (SELECT unnest(?))" if ids is not None else ""
        params = [self.denial_fys] + ([ids] if ids is not None else [])
        a, d, na, nd = self.con.execute(
            f"""SELECT sum({USCIS_APPROVED}), sum({USCIS_DENIED}),
                       sum(new_employment_approved), sum(new_employment_denied)
                FROM uscis WHERE fiscal_year IN (SELECT unnest(?)) {emp_sql}""",
            params).fetchone()
        a, d, na, nd = (int(x or 0) for x in (a, d, na, nd))
        return {"decisions": a + d, "denials": d, "rate": d / (a + d) if a + d else None,
                "new_decisions": na + nd, "new_denials": nd,
                "new_rate": nd / (na + nd) if na + nd else None}

    @staticmethod
    def _level(rate: float | None, decisions: int, denials: int, national: float | None,
               min_decisions: int = MIN_DECISIONS) -> str:
        if rate is None or decisions < min_decisions or not national:
            return DENIAL_FEW
        if denials >= MIN_DENIALS and rate >= HIGH_MULTIPLE * national:
            return DENIAL_HIGH
        if denials >= MIN_DENIALS and rate >= ELEVATED_MULTIPLE * national:
            return DENIAL_ELEVATED
        return DENIAL_NORMAL

    def denial(self, ids: list[str]) -> dict | None:
        if not self.has_uscis or not self.denial_fys:
            return None
        c = self._denial_counts(ids)
        n = self.national
        c["level"] = self._level(c["rate"], c["decisions"], c["denials"], n["rate"])
        c["new_level"] = self._level(c["new_rate"], c["new_decisions"], c["new_denials"],
                                     n["new_rate"], min_decisions=MIN_DECISIONS // 2)
        return c

    def denial_label(self) -> str:
        fys = self.denial_fys
        through = self.meta.get("uscis_through", "")
        last = f"FY{fys[-1]}" + (f" to {through.split()[-1]}" if through else "")
        return f"FY{fys[0]}–{last}" if len(fys) > 1 else last

    def denial_cell(self, ids: list[str]) -> str:
        d = self.denial(ids)
        if not d or d["rate"] is None:
            return "—"
        level = d["level"]
        if level == DENIAL_FEW:
            return f"{GREY} {d['rate']:.1%} (few cases)"
        icon, word = level.split(" ", 1)
        return f"{icon} {d['rate']:.1%}" + ("" if level == DENIAL_NORMAL else f" {word}")

    def denial_line(self, ids: list[str]) -> str | None:
        d = self.denial(ids)
        if not d or not d["decisions"]:
            return None
        n = self.national
        parts = [f"**USCIS denial rate, {self.denial_label()}:** "
                 f"{fmt_pct(d['rate'], signed=False)} of {fmt_int(d['decisions'])} decisions "
                 f"(national {fmt_pct(n['rate'], signed=False)}): **{d['level']}**"]
        if d["new_decisions"]:
            parts.append(f"new-employment petitions (e.g. from F-1/OPT): "
                         f"{fmt_pct(d['new_rate'], signed=False)} of "
                         f"{fmt_int(d['new_decisions'])} (national "
                         f"{fmt_pct(n['new_rate'], signed=False)}): **{d['new_level']}**")
        return " · ".join(parts)

    # filters -------------------------------------------------------------------------
    @staticmethod
    def _role_sql(role: str | None, alias: str = "l") -> tuple[str, list]:
        if not role:
            return "", []
        pats = ROLE_PATTERNS.get(role.strip().lower(), [f"%{role.strip()}%"])
        ors = " OR ".join(
            f"{alias}.job_title ILIKE ? OR {alias}.soc_title ILIKE ?" for _ in pats
        )
        return f" AND ({ors})", [p for p in pats for _ in (0, 1)]

    @staticmethod
    def _place_sql(state: str | None, city: str | None, alias: str = "l") -> tuple[str, list]:
        sql, params = "", []
        if state:
            sql += f" AND {alias}.worksite_state = ?"
            params.append(state.strip().upper())
        if city:
            sql += f" AND {alias}.worksite_city = ?"
            params.append(city.strip().upper())
        return sql, params

    def _filters(self, role, state, city, alias="l") -> tuple[str, list]:
        r_sql, r_p = self._role_sql(role, alias)
        p_sql, p_p = self._place_sql(state, city, alias)
        return r_sql + p_sql, r_p + p_p

    @staticmethod
    def _describe(role, state, city) -> str:
        bits = [f"role ~ '{role}'" if role else None,
                f"city {city.upper()}" if city else None,
                f"state {state.upper()}" if state else None]
        bits = [b for b in bits if b]
        return f" ({', '.join(bits)})" if bits else ""

    # employer matching ---------------------------------------------------------------
    def match_employers(self, company: str) -> list[str]:
        key = query_key(company)
        if not key:
            return []
        rows = self.con.execute(
            "SELECT DISTINCT employer_id FROM employer_names "
            "WHERE employer_norm = ? OR employer_norm LIKE ? || ' %'", [key, key]
        ).fetchall()
        return [r[0] for r in rows]

    def suggestions(self, company: str, limit: int = 5) -> list[str]:
        key = query_key(company)
        rows = self.con.execute(
            f"""SELECT e.display_name, count(*) n FROM employer_names n
                JOIN employers e USING (employer_id)
                JOIN lca l ON l.employer_norm = n.employer_norm
                WHERE n.employer_norm LIKE '%' || ? || '%'
                GROUP BY 1 ORDER BY n DESC LIMIT {int(limit)}""", [key]).fetchall()
        return [r[0] for r in rows]

    def entities(self, ids: list[str], limit: int = 8) -> list[tuple]:
        since = months_back(self.anchor, 12)
        return self.con.execute(
            f"""SELECT e.display_name, count(l.case_number) AS lcas
                FROM employers e LEFT JOIN lca l ON l.employer_id = e.employer_id
                  AND {CERTIFIED} AND l.decision_date BETWEEN ? AND ?
                WHERE e.employer_id IN (SELECT unnest(?))
                GROUP BY 1 ORDER BY lcas DESC, 1 LIMIT {int(limit)}""",
            [since, self.anchor, ids]).fetchall()

    # counts --------------------------------------------------------------------------
    def _lca_counts(self, ids, start, end, role=None, state=None, city=None) -> dict:
        f_sql, f_p = self._filters(role, state, city)
        row = self.con.execute(
            f"""SELECT count(*), count(*) FILTER (WHERE l.new_employment > 0),
                       count(*) FILTER (WHERE l.change_employer > 0)
                FROM lca l WHERE l.employer_id IN (SELECT unnest(?)) AND {CERTIFIED}
                  AND l.decision_date BETWEEN ? AND ? {f_sql}""",
            [ids, start, end, *f_p]).fetchone()
        return {"lcas": row[0], "new": row[1], "transfer": row[2]}

    def _perm_count(self, ids, start, end, role=None, state=None, city=None) -> int | None:
        if not self.has_perm:
            return None
        f_sql, f_p = self._filters(role, state, city, alias="p")
        return self.con.execute(
            f"""SELECT count(*) FROM perm p WHERE p.employer_id IN (SELECT unnest(?))
                AND {PERM_CERTIFIED} AND p.decision_date BETWEEN ? AND ? {f_sql}""",
            [ids, start, end, *f_p]).fetchone()[0]

    def verdict(self, ids, role=None, state=None, city=None) -> str:
        w = windows(self.anchor, self.data_from)[0]
        now = self._lca_counts(ids, w.start, w.end, role, state, city)["lcas"]
        before = (self._lca_counts(ids, w.prior_start, w.prior_end, role, state, city)["lcas"]
                  if w.prior_start else None)
        if now == 0:
            any_12 = self._lca_counts(ids, months_back(self.anchor, 12), self.anchor,
                                      role, state, city)["lcas"]
            return (f"{RED} No filings in last 6 months" if any_12
                    else f"{RED} No recent sponsorship")
        if before and before >= 10 and now <= 0.5 * before:
            return f"{AMBER} Slowing sharply ({(now - before) / before:+.0%})"
        if now < 5:
            return f"{AMBER} Occasional"
        return f"{GREEN} Yes, active"

    # tools ---------------------------------------------------------------------------
    def company_sponsorship(self, company: str, role=None, state=None, city=None) -> str:
        ids = self.match_employers(company)
        if not ids:
            sugg = self.suggestions(company)
            hint = f" Did you mean: {', '.join(sugg)}?" if sugg else ""
            return f"No employer matching **{company}** in the data.{hint}"

        ents = self.entities(ids)
        title = ents[0][0] if ents else company
        scope = self._describe(role, state, city)
        out = [f"### H-1B sponsorship: {title}{scope}",
               f"**Still sponsoring?** {self.verdict(ids, role, state, city)}"]
        denial = self.denial_line(ids)
        if denial:
            out.append(denial)
        out.append("")

        rows = []
        for w in windows(self.anchor, self.data_from):
            c = self._lca_counts(ids, w.start, w.end, role, state, city)
            p = (self._lca_counts(ids, w.prior_start, w.prior_end, role, state, city)
                 if w.prior_start else None)
            change = (c["lcas"] - p["lcas"]) / p["lcas"] if p and p["lcas"] else None
            perm = self._perm_count(ids, w.start, w.end, role, state, city)
            rows.append([f"{w.name} ({w.label})", fmt_int(c["lcas"]), fmt_int(c["new"]),
                         fmt_int(c["transfer"]), fmt_pct(change), fmt_int(perm)])
        out.append(table(["Window", "Certified H-1B LCAs", "New employment", "Transfers in",
                          "vs same period a year earlier", "Green card (PERM) certified"],
                         rows))

        if self.has_uscis:
            out += ["", "**USCIS decisions on H-1B petitions (company-wide)**", ""]
            urows = self.con.execute(
                """SELECT fiscal_year,
                          sum(new_employment_approved), sum(new_employment_denied),
                          sum(change_of_employer_approved), sum(change_of_employer_denied),
                          sum(continuation_approved),
                          sum(new_employment_approved + continuation_approved
                              + same_employer_change_approved + concurrent_approved
                              + change_of_employer_approved + amended_approved),
                          sum(new_employment_denied + continuation_denied
                              + same_employer_change_denied + concurrent_denied
                              + change_of_employer_denied + amended_denied)
                   FROM uscis WHERE employer_id IN (SELECT unnest(?))
                   GROUP BY 1 ORDER BY 1 DESC""", [ids]).fetchall()
            through = self.meta.get("uscis_through", "")
            latest_fy = max(self.meta.get("uscis_fiscal_years") or [0])
            body = []
            for fy, na, nd, ca, cd, cont, appr, den in urows:
                label = f"FY{fy}" + (f" (to {through.split()[-1]})"
                                     if fy == latest_fy and through else "")
                rate = den / (appr + den) if (appr + den) else None
                body.append([label, fmt_int(na), fmt_int(nd), fmt_int(ca), fmt_int(cd),
                             fmt_int(cont), fmt_pct(rate, signed=False)])
            out.append(table(["Fiscal year", "New employment approved", "denied",
                              "Change of employer approved", "denied", "Continuations approved",
                              "Denial rate (all types)"], body) if body
                       else "_No USCIS petitions on record under this name._")

        if len(ents) > 1:
            out += ["", "**Employer entities counted** (certified LCAs, all roles, last 12 months)",
                    "",
                    table(["Entity", "LCAs"], [[n, fmt_int(c)] for n, c in ents])]
        out += ["", SIGNAL_LEGEND, self.footnote(role_filtered=bool(role or state or city))]
        return "\n".join(out)

    def compare_companies(self, companies: list[str], role=None, state=None, city=None) -> str:
        w = windows(self.anchor, self.data_from)[0]
        year_start = months_back(self.anchor, 12)
        last_fy = fiscal_year(self.anchor) - 1
        rows = []
        for name in companies:
            ids = self.match_employers(name)
            if not ids:
                rows.append([name, "not found", "", "", "", "", "", ""])
                continue
            now = self._lca_counts(ids, w.start, w.end, role, state, city)
            before = (self._lca_counts(ids, w.prior_start, w.prior_end, role, state, city)
                      if w.prior_start else None)
            change = ((now["lcas"] - before["lcas"]) / before["lcas"]
                      if before and before["lcas"] else None)
            perm = self._perm_count(ids, year_start, self.anchor, role, state, city)
            new_appr = None
            if self.has_uscis:
                new_appr = self.con.execute(
                    "SELECT sum(new_employment_approved) FROM uscis WHERE employer_id IN "
                    "(SELECT unnest(?)) AND fiscal_year = ?", [ids, last_fy]).fetchone()[0]
            display = self.entities(ids, 1)[0][0]
            rows.append([display, fmt_int(now["lcas"]), fmt_int(now["new"]), fmt_pct(change),
                         fmt_int(perm), fmt_int(new_appr), self.denial_cell(ids),
                         self.verdict(ids, role, state, city)])
        scope = self._describe(role, state, city)
        return "\n".join([
            f"### Sponsorship comparison{scope}", "",
            table(["Company", f"LCAs {w.label}", "of which new employment",
                   "vs a year earlier", "PERM certified, last 12 mo",
                   f"USCIS new-employment approvals FY{last_fy}",
                   f"USCIS denial rate {self.denial_label()}" if self.has_uscis
                   else "USCIS denial rate", "Still sponsoring?"],
                  rows, ["l", "r", "r", "r", "r", "r", "r", "l"]),
            "", self.denial_note(), self.footnote(role_filtered=bool(role or state or city))])

    def top_sponsors(self, role=None, state=None, city=None, months: int = 12,
                     limit: int = 25) -> str:
        months = max(1, min(int(months), 21))
        limit = max(1, min(int(limit), 100))
        start = months_back(self.anchor, months)
        f_sql, f_p = self._filters(role, state, city)
        rows = self.con.execute(
            f"""SELECT e.employer_id, e.display_name, count(*) lcas,
                       count(*) FILTER (WHERE l.new_employment > 0) new_emp,
                       median(l.wage_annual) wage
                FROM lca l JOIN employers e USING (employer_id)
                WHERE {CERTIFIED} AND l.decision_date BETWEEN ? AND ? {f_sql}
                GROUP BY 1, 2 ORDER BY lcas DESC, 2 LIMIT {limit}""",
            [start, self.anchor, *f_p]).fetchall()
        scope = self._describe(role, state, city)
        body = [[i + 1, n, fmt_int(c), fmt_int(ne), fmt_money(w), self.denial_cell([eid])]
                for i, (eid, n, c, ne, w) in enumerate(rows)]
        return "\n".join([
            f"### Top H-1B sponsors{scope}, {_label(start, self.anchor)}", "",
            table(["#", "Employer", "Certified LCAs", "New employment", "Median offered wage",
                   "USCIS denial rate"],
                  body, ["r", "l", "r", "r", "r", "r"]) if body else "_No matching filings._",
            "", self.denial_note(), self.footnote(role_filtered=bool(role or state or city))])

    def salary_range(self, role: str, state=None, city=None, company=None) -> str:
        start = months_back(self.anchor, 12)
        f_sql, f_p = self._filters(role, state, city)
        emp_sql, emp_p = "", []
        if company:
            ids = self.match_employers(company)
            if not ids:
                return f"No employer matching **{company}** in the data."
            emp_sql, emp_p = " AND l.employer_id IN (SELECT unnest(?))", [ids]
        base = (f"FROM lca l WHERE {CERTIFIED} AND l.wage_annual IS NOT NULL "
                f"AND l.decision_date BETWEEN ? AND ? {f_sql}{emp_sql}")
        params = [start, self.anchor, *f_p, *emp_p]
        overall = self.con.execute(
            f"""SELECT count(*), quantile_cont(l.wage_annual, [0.1, 0.25, 0.5, 0.75, 0.9])
                {base}""", params).fetchone()
        if not overall[0]:
            return "No certified H-1B filings match those filters in the last 12 months."
        by_level = self.con.execute(
            f"""SELECT coalesce(l.pw_level, 'Not stated') lvl, count(*),
                       quantile_cont(l.wage_annual, 0.5) {base}
                GROUP BY 1 ORDER BY 1""", params).fetchall()
        q = overall[1]
        scope = self._describe(role, state, city) + (f" at {company}" if company else "")
        return "\n".join([
            f"### Offered H-1B wages{scope}, {_label(start, self.anchor)}", "",
            table(["Filings", "10th pct", "25th pct", "Median", "75th pct", "90th pct"],
                  [[fmt_int(overall[0]), *[fmt_money(v) for v in q]]], ["r"] * 6),
            "", "**By prevailing-wage level** (I = entry, IV = most senior)", "",
            table(["Level", "Filings", "Median"],
                  [[lv, fmt_int(n), fmt_money(m)] for lv, n, m in by_level]),
            "", "Wages are the lower bound of the offered range on each LCA, annualised. "
            + self.footnote(role_filtered=True, include_lca_note=False)])

    def find_employer(self, query: str, limit: int = 15) -> str:
        ids = self.match_employers(query)
        if not ids:
            sugg = self.suggestions(query, 10)
            return (f"No employer name starts with **{query}**."
                    + (f" Closest: {', '.join(sugg)}." if sugg else ""))
        rows = self.con.execute(
            f"""SELECT e.display_name, e.n_names, e.n_feins,
                       (SELECT count(*) FROM lca l WHERE l.employer_id = e.employer_id
                          AND {CERTIFIED}) lcas,
                       (SELECT string_agg(DISTINCT worksite_state, ', ') FROM
                          (SELECT worksite_state FROM lca WHERE employer_id = e.employer_id
                           AND worksite_state IS NOT NULL LIMIT 200)) states
                FROM employers e WHERE e.employer_id IN (SELECT unnest(?))
                ORDER BY lcas DESC LIMIT {int(limit)}""", [ids]).fetchall()
        return "\n".join([
            f"### Employers matching '{query}'", "",
            table(["Employer", "Name spellings", "Tax IDs", f"Certified LCAs since "
                   f"{self.data_from:%b %Y}", "Worksite states (sample)"],
                  [[n, s, f, fmt_int(c), st or "—"] for n, s, f, c, st in rows],
                  ["l", "r", "r", "r", "l"]),
            "", "Pass one of these exact names to the other tools to count only that entity."])

    def data_info(self) -> str:
        m = self.meta
        c = m["counts"]
        rows = [
            ["DOL LCA disclosures", f"{m['lca_from']} → {m['lca_through']}",
             fmt_int(c["lca"]), ", ".join(m["lca_files"])],
            ["DOL PERM disclosures", f"{m['perm_from']} → {m['perm_through']}",
             fmt_int(c["perm"]), ", ".join(m["perm_files"])],
            ["USCIS H-1B Employer Data Hub",
             f"FY{min(m['uscis_fiscal_years'])} → {m['uscis_through']}"
             if m["uscis_fiscal_years"] else "—", fmt_int(c["uscis_rows"]), "Crosstab export"],
        ]
        return "\n".join([
            "### Data coverage", "",
            table(["Source", "Decisions covered", "Rows", "Files"], rows, ["l", "l", "r", "l"]),
            "", f"{fmt_int(c['employers'])} employers resolved from "
            f"{fmt_int(c['employer_names'])} name spellings. Built {m['built_at']}.",
            "", LCA_NOTE,
            "", "This is public filing data, not legal advice. Employer policies change; "
            "confirm with the employer."])

    def denial_note(self) -> str:
        if not self.has_uscis or not self.national["rate"]:
            return ""
        n = self.national["rate"]
        return (f"{SIGNAL_LEGEND} Denial flags compare each employer's USCIS H-1B denial "
                f"rate ({self.denial_label()}, all petition types) with the national "
                f"{n:.1%}: **{DENIAL_HIGH}** = {HIGH_MULTIPLE:.0f}x national or more "
                f"({HIGH_MULTIPLE * n:.1%}+), **{DENIAL_ELEVATED}** = "
                f"{ELEVATED_MULTIPLE:.0f}x ({ELEVATED_MULTIPLE * n:.1%}+); both need "
                f"{MIN_DECISIONS}+ decisions and {MIN_DENIALS}+ denials. Denials are "
                "company-wide, not role-specific.")

    def footnote(self, role_filtered: bool = False, include_lca_note: bool = True) -> str:
        parts = [f"_Data through {self.anchor:%b %d, %Y} (DOL decision dates)."]
        if role_filtered:
            parts.append("Role and location filters apply to DOL filings; USCIS data has "
                         "no job title or worksite.")
        if include_lca_note:
            parts.append(LCA_NOTE)
        return " ".join(parts) + "_"
