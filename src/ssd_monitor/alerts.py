"""One alert flag per indicator, from a simple threshold. No combined score. No Earth Engine here.

Each flag is True, False, or blank (missing) when the indicator itself is missing, so "no data"
is never read as "no problem".
"""

import pandas as pd

from ssd_monitor.config import AlertThresholds


def _flag(values: pd.Series, condition: pd.Series) -> pd.Series:
    return condition.astype("boolean").mask(values.isna())


def add_flags(df: pd.DataFrame, t: AlertThresholds) -> pd.DataFrame:
    """Add flag_rain_low, flag_ndvi_low and flag_flood. Each threshold is strict (below / above)."""
    out = df.copy()
    out["flag_rain_low"] = _flag(out["rain_pct_normal"], out["rain_pct_normal"] < t.rain_pct_normal_below)
    out["flag_ndvi_low"] = _flag(out["ndvi_anomaly"], out["ndvi_anomaly"] < t.ndvi_anomaly_below)
    out["flag_flood"] = _flag(out["flooded_pct"], out["flooded_pct"] > t.flooded_pct_above)
    return out


def describe(t: AlertThresholds) -> dict[str, str]:
    """Plain-language rule for each flag, for figure notes and the CLI."""
    return {
        "flag_rain_low": f"rainfall below {t.rain_pct_normal_below:g}% of normal",
        "flag_ndvi_low": f"NDVI anomaly below {t.ndvi_anomaly_below:g} standard deviations",
        "flag_flood": f"more than {t.flooded_pct_above:g}% of the county flooded",
    }
