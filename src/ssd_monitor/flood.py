"""Monthly flood extent from Sentinel-1 radar and Sentinel-2 optical imagery (Earth Engine).

The method, step by step (the README explains each choice):

1. Radar reference: for each orbit direction, the median VV and VH backscatter of the dry-season
   months in the reference years. It is exported once as an asset and reused for every month.
2. Radar flag: for each orbit direction, the month's median VV and VH. A pixel is flagged when VV or
   VH is at least `drop_db` darker than the reference and VV is below `vv_water_max_db`. A pixel is
   flagged if either direction flags it.
3. Optical flag: Sentinel-2 scenes with Cloud Score+ cloud masking, MNDWI = (green - SWIR1) /
   (green + SWIR1), and the month's median MNDWI of the clear observations. Flagged above
   `mndwi_water_min`.
4. Combine: flooded where either sensor flags water, except permanent water (JRC Global Surface
   Water occurrence) and steep slopes (HydroSHEDS), and except isolated patches of fewer than
   `min_connected_pixels` pixels.

Earth Engine reads the imagery at the processing scale (flood.scale_m), averaging the finer pixels
from its image pyramids. At 100 m that averaging also smooths out radar speckle.
"""

import ee

from ssd_monitor.config import FloodConfig, OpticalConfig, SarConfig
from ssd_monitor.dates import month_window, reference_windows
from ssd_monitor.earthengine import county_stats, with_fallback

FLOOD_BANDS = ["flooded", "flooded_sar", "flooded_optical", "observed"]
CONNECTED_MAX_SIZE = 100  # pixels; larger patches all count as "big enough"


# --- Radar (Sentinel-1) ------------------------------------------------------------------


def s1_images(sar: SarConfig, region: ee.Geometry, orbit_pass: str) -> ee.ImageCollection:
    """Sentinel-1 IW scenes with both VV and VH, from one orbit direction, in dB."""
    return (
        ee.ImageCollection(sar.collection)
        .filterBounds(region)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.eq("resolution_meters", 10))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .filter(ee.Filter.eq("orbitProperties_pass", orbit_pass))
        .select(["VV", "VH"])
    )


def s1_reference(sar: SarConfig, region: ee.Geometry) -> ee.Image:
    """Dry-season median backscatter, two bands per orbit direction: VV_ASCENDING, VH_ASCENDING, ..."""
    dry_season = ee.Filter.Or(
        *[ee.Filter.date(s, e) for s, e in reference_windows(sar.reference_years, sar.reference_months)]
    )
    per_pass = []
    for orbit_pass in sar.orbit_passes:
        images = s1_images(sar, region, orbit_pass).filter(dry_season)
        median = with_fallback(images, ["VV", "VH"]).median()
        per_pass.append(median.rename([f"VV_{orbit_pass}", f"VH_{orbit_pass}"]))
    return ee.Image.cat(per_pass).toFloat()


def sar_water(sar: SarConfig, reference: ee.Image, region: ee.Geometry, month: str) -> ee.Image:
    """1 where the radar flags new water this month, 0 where it doesn't, masked where it saw nothing."""
    start, end = month_window(month)
    flags = []
    for orbit_pass in sar.orbit_passes:
        images = s1_images(sar, region, orbit_pass).filterDate(start, end)
        now = with_fallback(images, ["VV", "VH"]).median()
        before = reference.select([f"VV_{orbit_pass}", f"VH_{orbit_pass}"], ["VV", "VH"])
        drop = before.subtract(now)  # positive where this month is darker than the dry season
        darker = drop.select("VV").gte(sar.drop_db).Or(drop.select("VH").gte(sar.drop_db))
        flags.append(darker.And(now.select("VV").lte(sar.vv_water_max_db)).rename("water"))
    return ee.ImageCollection(flags).max()


# --- Optical (Sentinel-2) ----------------------------------------------------------------


def optical_water(opt: OpticalConfig, region: ee.Geometry, month: str) -> ee.Image:
    """1 where the month's median MNDWI says water, 0 where it doesn't, masked where never clear."""
    start, end = month_window(month)
    scenes = (
        ee.ImageCollection(opt.collection)
        .filterBounds(region)
        .filterDate(start, end)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", opt.max_scene_cloud_pct))
    )

    def clear_mndwi(image):
        clear = image.select("cs_cdf").gte(opt.min_clear_score)
        return image.normalizedDifference(["B3", "B11"]).rename("mndwi").updateMask(clear)

    mndwi = scenes.linkCollection(ee.ImageCollection(opt.cloud_score_collection), ["cs_cdf"]).map(clear_mndwi)
    return with_fallback(mndwi, ["mndwi"]).median().gt(opt.mndwi_water_min).rename("water")


# --- Combined ----------------------------------------------------------------------------


def excluded(cfg: FloodConfig) -> ee.Image:
    """1 where flooding is not assessed: permanent water or steep slopes."""
    occurrence = ee.Image(cfg.permanent_water.collection).select("occurrence").unmask(0)
    permanent = occurrence.gte(cfg.permanent_water.min_occurrence_pct)
    steep = ee.Terrain.slope(ee.Image(cfg.terrain.dem)).gt(cfg.terrain.max_slope_deg).unmask(0)
    return permanent.Or(steep).rename("excluded")


def monthly_flood(cfg: FloodConfig, reference: ee.Image, region: ee.Geometry, month: str) -> ee.Image:
    """Four 0/1 bands for one month: flooded, flooded_sar, flooded_optical and observed.

    flooded_sar and flooded_optical say which sensor flagged each flooded pixel (often both).
    observed is 1 where at least one sensor saw the pixel that month.
    """
    sar = sar_water(cfg.sar, reference, region, month)
    optical = optical_water(cfg.optical, region, month)
    assessed = excluded(cfg).Not()

    observed = sar.mask().gt(0).Or(optical.mask().gt(0)).And(assessed)
    sar_flag = sar.unmask(0).And(assessed)
    optical_flag = optical.unmask(0).And(assessed)
    either = sar_flag.Or(optical_flag)
    patch_size = either.selfMask().connectedPixelCount(CONNECTED_MAX_SIZE, True).unmask(0)
    big_enough = patch_size.gte(cfg.min_connected_pixels)

    return (
        ee.Image.cat(
            [either.And(big_enough), sar_flag.And(big_enough), optical_flag.And(big_enough), observed]
        )
        .rename(FLOOD_BANDS)
        .toByte()
    )


def flood_table(
    cfg: FloodConfig,
    reference: ee.Image,
    region: ee.Geometry,
    months: list[str],
    counties: ee.FeatureCollection,
) -> ee.FeatureCollection:
    """Areas in km2 per county-month: flooded_km2, flooded_sar_km2, flooded_optical_km2, observed_km2."""
    km2 = ee.Image.pixelArea().divide(1e6)
    images = [
        monthly_flood(cfg, reference, region, m).multiply(km2).rename([f"{b}_km2" for b in FLOOD_BANDS])
        for m in months
    ]
    return county_stats(images, months, counties, ee.Reducer.sum(), cfg.scale_m)


def months_flooded(cfg: FloodConfig, reference: ee.Image, region: ee.Geometry, months: list[str]) -> ee.Image:
    """Per pixel, the number of the given months that were flooded, plus the excluded mask."""
    flooded = [monthly_flood(cfg, reference, region, m).select("flooded") for m in months]
    count = ee.ImageCollection(flooded).sum().toByte().rename("months_flooded")
    return count.addBands(excluded(cfg).toByte()).clip(region)
