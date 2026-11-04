"""Monthly rainfall from CHIRPS pentads (Earth Engine).

CHIRPS pentads split each month into six periods (days 1-5, 6-10, 11-15, 16-20, 21-25 and 26 to the
end of the month), so the sum of the pentads that start in a month is that month's total.
The normal and the % of normal are worked out later, from the county totals (stats.py).
"""

import ee

from ssd_monitor.config import RainfallConfig
from ssd_monitor.dates import month_window
from ssd_monitor.earthengine import county_stats, with_fallback


def monthly_rainfall(cfg: RainfallConfig, month: str) -> ee.Image:
    """Total rainfall in mm for one month ("YYYY-MM"), one band: rain_mm."""
    start, end = month_window(month)
    pentads = ee.ImageCollection(cfg.collection).filterDate(start, end).select("precipitation")
    return with_fallback(pentads, ["precipitation"]).sum().rename("rain_mm")


def rainfall_table(
    cfg: RainfallConfig, months: list[str], counties: ee.FeatureCollection
) -> ee.FeatureCollection:
    """County mean of the monthly total, for each month: properties county_code, month, rain_mm."""
    images = [monthly_rainfall(cfg, m) for m in months]
    reducer = ee.Reducer.mean().setOutputs(["rain_mm"])
    return county_stats(images, months, counties, reducer, cfg.scale_m, tile_scale=1)
