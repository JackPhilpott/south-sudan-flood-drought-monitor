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


def year_chunks(first_year: int, last_year: int, size: int) -> list[tuple[int, int]]:
    """Split a range of years into consecutive chunks of at most `size` years, both ends included."""
    if size < 1:
        raise ValueError("size must be at least 1")
    return [(y, min(y + size - 1, last_year)) for y in range(first_year, last_year + 1, size)]
