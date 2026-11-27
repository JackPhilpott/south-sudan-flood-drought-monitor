"""One alert flag per indicator, from a simple threshold. No combined score. No Earth Engine here.

Each flag is True, False, or blank (missing) when the indicator itself is missing, so "no data"
is never read as "no problem".
"""

import pandas as pd

from ssd_monitor.config import AlertThresholds
from ssd_monitor.flood_flags import percentile_flag


def _flag(values: pd.Series, condition: pd.Series) -> pd.Series:
    return condition.astype("boolean").mask(values.isna())


def add_flags(df: pd.DataFrame, t: AlertThresholds) -> pd.DataFrame:
    """Add flag_rain_low, flag_ndvi_low and flag_flood. Thresholds are strict (below / above).

    df must hold every month of each county: the flood flag compares a month with the same calendar
    month in the other years.
    """
    out = df.copy()
    out["flag_rain_low"] = _flag(out["rain_pct_normal"], out["rain_pct_normal"] < t.rain_pct_normal_below)
    out["flag_ndvi_low"] = _flag(out["ndvi_anomaly"], out["ndvi_anomaly"] < t.ndvi_anomaly_below)
    # R1 (flood_flags.py): compared with each county's own record for the same calendar month.
    r1 = percentile_flag(out, t.flood_new_percentile, t.flood_new_min_km2)
    out["flag_flood"] = r1.astype("boolean").mask(out["new_km2"].isna())
    return out


def describe(t: AlertThresholds) -> dict[str, str]:
    """Plain-language rule for each flag, for figure notes and the CLI."""
    return {
        "flag_rain_low": f"rainfall below {t.rain_pct_normal_below:g}% of normal",
        "flag_ndvi_low": f"NDVI anomaly below {t.ndvi_anomaly_below:g} standard deviations",
        "flag_flood": (
            f"new water above the county's own {t.flood_new_percentile:g}th percentile for the month "
            f"and at least {t.flood_new_min_km2:g} km²"
        ),
    }
