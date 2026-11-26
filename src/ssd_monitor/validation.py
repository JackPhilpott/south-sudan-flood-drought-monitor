"""Agreement of the flood layers with JRC Global Surface Water monthly history (Earth Engine).

JRC's monthly water maps come from Landsat and were made independently of this project. They only
show open water that Landsat saw, and stop at the end of 2021, so this measures agreement on open
water for a sample of counties and months. It is not an accuracy assessment: JRC has its own errors,
and flooding under vegetation is missing from both.

For each county-month the comparison is pixel by pixel at the flood processing scale, only where
JRC observed the pixel, and without permanent water or steep slopes (as in the flood layers). Each
radar rule and the optical rule are scored before the small-patch filter; the final flood layer
(radar or optical, after the patch filter) is scored as exported.
"""

import ee

from ssd_monitor import flood
from ssd_monitor.config import FloodConfig, SarConfig
from ssd_monitor.dates import month_window
from ssd_monitor.earthengine import with_fallback

JRC_MONTHLY = "JRC/GSW1_4/MonthlyHistory"  # 0 no data, 1 not water, 2 water
JRC_WATER = 2
# Flood-prone counties in Jonglei, Unity and Upper Nile, in two flood months and one dry month.
SAMPLE_COUNTIES = ("SS0303", "SS0306", "SS0310", "SS0607", "SS0609", "SS0704")
SAMPLE_MONTHS = ("2020-09", "2021-02", "2021-10")

# The radar rule this project first used, kept only for this comparison: a pixel was water when VV
# or VH dropped by DROP_DB from the median of the dry-season months below, and VV was below VV_MAX.
CHANGE_REFERENCE_YEARS = (2017, 2018, 2019)
CHANGE_REFERENCE_MONTHS = (1, 2, 3)
CHANGE_DROP_DB = 3.0
CHANGE_VV_MAX_DB = -15.0


def change_rule_water(sar: SarConfig, region: ee.Geometry, month: str) -> ee.Image:
    """The first-design radar rule: darker than the dry-season reference, in either orbit direction."""
    start, end = month_window(month)
    windows = [month_window(f"{y}-{m:02d}") for y in CHANGE_REFERENCE_YEARS for m in CHANGE_REFERENCE_MONTHS]
    dry_season = ee.Filter.Or(*[ee.Filter.date(s, e) for s, e in windows])
    flags = []
    for orbit_pass in sar.orbit_passes:
        images = flood.s1_images(sar, region, orbit_pass)
        before = with_fallback(images.filter(dry_season), ["VV", "VH"]).median()
        now = with_fallback(images.filterDate(start, end), ["VV", "VH"]).median()
        drop = before.subtract(now)
        darker = drop.select("VV").gte(CHANGE_DROP_DB).Or(drop.select("VH").gte(CHANGE_DROP_DB))
        flags.append(darker.And(now.select("VV").lte(CHANGE_VV_MAX_DB)).rename("water"))
    return ee.ImageCollection(flags).max()


def agreement_km2(cfg: FloodConfig, region: ee.Geometry, month: str) -> ee.Dictionary:
    """km2 that JRC calls water, that each layer flags, and that both agree on, within `region`."""
    start, end = month_window(month)
    jrc = ee.ImageCollection(JRC_MONTHLY).filterDate(start, end).first()
    jrc_water = jrc.eq(JRC_WATER)
    assessed = flood.excluded(cfg).Not()
    layers = {
        "flooded": flood.monthly_flood(cfg, region, month).select("flooded"),
        "radar_dark": flood.sar_water(cfg.sar, region, month).unmask(0).And(assessed),
        "radar_change": change_rule_water(cfg.sar, region, month).unmask(0).And(assessed),
        "optical": flood.optical_water(cfg.optical, region, month).unmask(0).And(assessed),
    }
    bands = [jrc_water.And(assessed).rename("jrc")]
    for name, flag in layers.items():
        bands += [flag.rename(f"{name}_flag"), flag.And(jrc_water).rename(f"{name}_both")]
    image = ee.Image.cat(bands).updateMask(jrc.gt(0)).multiply(ee.Image.pixelArea().divide(1e6))
    return image.reduceRegion(
        ee.Reducer.sum(), region, cfg.scale_m, crs="EPSG:4326", maxPixels=1e10, tileScale=4
    )


def benchmark(cfg: FloodConfig, counties: ee.FeatureCollection, codes=SAMPLE_COUNTIES, months=SAMPLE_MONTHS):
    """One row of km2 per county-month. Earth Engine limits concurrent aggregations, so one request each."""
    rows = []
    for month in months:
        for code in codes:
            region = counties.filter(ee.Filter.eq("county_code", code)).geometry()
            rows.append({"county_code": code, "month": month} | agreement_km2(cfg, region, month).getInfo())
    return rows
