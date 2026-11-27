"""Month and year windows. Months are written "YYYY-MM"; date windows are [start, end) in "YYYY-MM-DD"."""

import pandas as pd


def months_between(first: str, last: str) -> list[str]:
    """Every month from first to last, both included, as "YYYY-MM"."""
    if first > last:
        raise ValueError(f"{first} is after {last}")
    return pd.period_range(first, last, freq="M").strftime("%Y-%m").tolist()


def months_in_years(first_year: int, last_year: int) -> list[str]:
    """Every month of the given years, both included."""
    return months_between(f"{first_year}-01", f"{last_year}-12")


def month_window(month: str) -> tuple[str, str]:
    """The first day of the month and the first day of the next month (exclusive end)."""
    period = pd.Period(month, freq="M")
    return period.start_time.strftime("%Y-%m-%d"), (period + 1).start_time.strftime("%Y-%m-%d")


def add_months(month: str, n: int) -> str:
    """The month n months after `month` (n may be negative)."""
    return (pd.Period(month, freq="M") + n).strftime("%Y-%m")


def burn_window(month: str, recent_months: int) -> list[str]:
    """The months whose burns count for `month` in the recent-burn test (a1): it and the months before."""
    return months_between(add_months(month, -recent_months), month)


def season_burn_window(month: str, season_start: int, in_season: bool) -> list[str]:
    """The months whose burns count for `month` in the burn-season mask (a2), which the project uses.

    In the burn season, every month from the most recent `season_start` (1 November) to `month`;
    outside it, none. So a burn can only remove radar water later in the same dry season, never in a
    wet-season flood.
    """
    if not in_season:
        return []
    period = pd.Period(month, freq="M")
    start_year = period.year if period.month >= season_start else period.year - 1
    return months_between(f"{start_year}-{season_start:02d}", month)


def year_chunks(first_year: int, last_year: int, size: int) -> list[tuple[int, int]]:
    """Split a range of years into consecutive chunks of at most `size` years, both ends included."""
    if size < 1:
        raise ValueError("size must be at least 1")
    return [(y, min(y + size - 1, last_year)) for y in range(first_year, last_year + 1, size)]
