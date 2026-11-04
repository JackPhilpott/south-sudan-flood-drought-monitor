"""Monthly NDVI from MODIS MOD13Q1 (Earth Engine).

MOD13Q1 is a 16-day composite at 250 m. A month's NDVI is the mean of the composites whose 16-day
period starts in that month, using only pixels rated good or marginal. Water, built-up and bare
land are masked with ESA WorldCover, because their NDVI says nothing about vegetation condition.
The baseline and the standardised anomaly are worked out later, from the county means (stats.py).
"""

import ee

from ssd_monitor.config import VegetationConfig
from ssd_monitor.dates import month_window
from ssd_monitor.earthengine import county_stats, with_fallback

NDVI_SCALE_FACTOR = 0.0001  # MOD13Q1 stores NDVI as integers


def vegetated(cfg: VegetationConfig) -> ee.Image:
    """1 where the WorldCover class is kept, 0 where it is in exclude_worldcover."""
    landcover = ee.ImageCollection(cfg.worldcover_collection).first().select("Map")
    classes = list(cfg.exclude_worldcover)
    return landcover.remap(classes, [1] * len(classes), 0).Not()


def monthly_ndvi(cfg: VegetationConfig, month: str) -> ee.Image:
    """Mean NDVI for one month ("YYYY-MM") over vegetated land, one band: ndvi."""
    start, end = month_window(month)

    def good_pixels(image):
        return image.select("NDVI").updateMask(image.select("SummaryQA").lte(cfg.max_summary_qa))

    composites = ee.ImageCollection(cfg.collection).filterDate(start, end).map(good_pixels)
    ndvi = with_fallback(composites, ["NDVI"]).mean().multiply(NDVI_SCALE_FACTOR)
    return ndvi.updateMask(vegetated(cfg)).rename("ndvi")


def ndvi_table(
    cfg: VegetationConfig, months: list[str], counties: ee.FeatureCollection
) -> ee.FeatureCollection:
    """County mean NDVI for each month: properties county_code, month, ndvi."""
    images = [monthly_ndvi(cfg, m) for m in months]
    reducer = ee.Reducer.mean().setOutputs(["ndvi"])
    return county_stats(images, months, counties, reducer, cfg.scale_m)
