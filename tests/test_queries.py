from datetime import date

import pytest

from h1b_sponsor_mcp.queries import months_back, windows
from h1b_sponsor_mcp.render import table


def test_windows_are_whole_calendar_months():
    w6, fy, last_fy = windows(date(2026, 6, 30), date(2024, 10, 1))
    assert (w6.start, w6.end) == (date(2026, 1, 1), date(2026, 6, 30))
    assert (w6.prior_start, w6.prior_end) == (date(2025, 1, 1), date(2025, 6, 30))
    assert w6.label == "Jan–Jun 2026"
    assert (fy.start, fy.prior_start, fy.prior_end) == (
        date(2025, 10, 1), date(2024, 10, 1), date(2025, 6, 30))
    assert last_fy.name == "FY2025" and last_fy.prior_start is None  # FY2024 not loaded


def test_windows_mid_month_anchor_uses_same_day_last_year():
    w6 = windows(date(2026, 6, 15), date(2024, 10, 1))[0]
    assert w6.prior_end == date(2025, 6, 15)


def test_months_back():
    assert months_back(date(2026, 6, 30), 12) == date(2025, 7, 1)
    assert months_back(date(2026, 2, 28), 1) == date(2026, 2, 1)


def test_store_reads_metadata(store):
    assert store.anchor == date(2026, 6, 30)
    assert store.has_perm and store.has_uscis


def test_matching_merges_spellings_and_lists_prefix_neighbours(store):
    ids = store.match_employers("acme")
    names = [n for n, _ in store.entities(ids)]
    assert names[0].upper().startswith("ACME CLOUD")
    assert any("Tree" in n for n in names)  # shown, so the user can see it was counted
    assert len(store.match_employers("Acme Cloud Services")) == 1


@pytest.mark.parametrize("company,expected", [
    ("Acme Cloud", "🟡 Slowing sharply (-60%)"),
    ("Steady Corp", "🟢 Yes, active"),
    ("Gone LLC", "🔴 No recent sponsorship"),
])
def test_verdicts(store, company, expected):
    assert store.verdict(store.match_employers(company)) == expected


def test_company_sponsorship_tables(store):
    md = store.company_sponsorship("Acme Cloud")
    assert "Last 6 months (Jan–Jun 2026)" in md
    assert "| Last 6 months (Jan–Jun 2026) | 8 | 8 | 0 | -60% | 1 |" in md
    assert "FY2026 (to Q3)" in md and "| FY2025 | 30 | 2 | 5 | 1 |" in md
    assert "Data through Jun 30, 2026" in md
    # All three Acme Cloud spellings share a tax ID, so they're one entity, not three.
    assert "Employer entities counted" not in md


def test_role_filter_and_denied_or_e3_not_counted(store):
    md = store.company_sponsorship("Acme Cloud", role="data scientist")
    assert "| Last 6 months (Jan–Jun 2026) | 8 |" in md
    assert "| FY2025 (Oct 2024–Sep 2025) | 0 |" in md
    steady = store._lca_counts(store.match_employers("Steady Corp"),
                               date(2026, 1, 1), date(2026, 6, 30))
    assert steady == {"lcas": 10, "new": 0, "transfer": 10}


def test_denial_flags_relative_to_national(store):
    # Fixture national rate: 11 denials of 295 decisions = 3.7%.
    assert store.national["rate"] == pytest.approx(11 / 295)
    gone = store.denial(store.match_employers("Gone LLC"))
    assert gone["level"] == "🔴 High" and gone["rate"] == pytest.approx(6 / 26)
    assert store.denial(store.match_employers("Steady Corp"))["level"] == "🟢 Normal"
    assert store.denial(store.match_employers("Acme Cloud"))["level"] == "🟢 Normal"


@pytest.mark.parametrize("rate,decisions,denials,expected", [
    (0.50, 10, 5, "⚪ Too few to judge"),   # under 20 decisions
    (0.10, 20, 2, "🟢 Normal"),                       # high rate but only 2 denials
    (0.08, 100, 8, "🟡 Elevated"),                    # 2x national of 0.035
    (0.11, 100, 11, "🔴 High"),                     # 3x national
])
def test_denial_level_thresholds(rate, decisions, denials, expected):
    from h1b_sponsor_mcp.queries import Store
    assert Store._level(rate, decisions, denials, 0.035) == expected


def test_denial_flag_shows_in_every_table(store):
    assert "**USCIS denial rate, FY2025–FY2026 to Q3:**" in store.company_sponsorship("Gone LLC")
    assert "🔴 23.1% High" in store.compare_companies(["Gone LLC", "Steady Corp"])
    top = store.top_sponsors(state="TX")
    assert "| 1 | Steady Corp |" in top and "Denial flags compare" in top


def test_every_answer_carries_the_signal_legend(store):
    for md in (store.company_sponsorship("Acme Cloud"), store.compare_companies(["Acme Cloud"]),
               store.top_sponsors()):
        assert "🟢 good · 🟡 caution · 🔴 warning" in md


def test_unknown_company_is_handled(store):
    assert "No employer matching" in store.company_sponsorship("Zzqx Imaginary")


def test_compare_companies(store):
    md = store.compare_companies(["Acme Cloud", "Steady Corp", "Nobody Here"])
    assert "| Steady Corp | 10 | 0 | +0% |" in md
    assert "🟢 1.0% | 🟢 Yes, active |" in md
    assert "| Nobody Here | not found |" in md


def test_top_sponsors_respects_place_filter(store):
    md = store.top_sponsors(state="TX", months=12)
    assert "| 1 | Steady Corp |" in md and "Acme" not in md


def test_salary_range_annualises_hourly(store):
    md = store.salary_range("data scientist", city="Atlanta")
    assert "| 8 | $124,800 |" in md  # $60/hour x 2080


def test_find_employer_and_data_info(store):
    assert "Acme Tree Care LLC" in store.find_employer("Acme")
    info = store.data_info()
    assert "2024-10-01 → 2026-06-30" in info and "not legal advice" in info


def test_table_escapes_pipes():
    assert "A/B" in table(["x"], [["A|B"]])
