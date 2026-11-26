"""Monthly flood extent from Sentinel-1 radar and Sentinel-2 optical imagery (Earth Engine).

The method, step by step (the README explains each choice):

1. Radar flag: for each orbit direction, the month's median VV backscatter. Calm open water is dark,
   so a pixel is flagged when the median is at or below `vv_water_max_db`. A pixel is flagged if
   either direction flags it. (A change rule against a dry-season reference was tried first and
   agreed worse with JRC's Landsat water: dry clay plains are dark in the dry season too. See the
   README and validation.py.)
2. Optical flag: Sentinel-2 scenes with Cloud Score+ cloud masking, MNDWI = (green - SWIR1) /
   (green + SWIR1), and the month's median MNDWI of the clear observations. Flagged above
   `mndwi_water_min`.
3. Combine: flooded where either sensor flags water, except permanent water (JRC Global Surface
   Water occurrence) and steep slopes (HydroSHEDS), and except isolated patches of fewer than
   `min_connected_pixels` pixels.
4. Store: each month's four flags (flooded, flagged by radar, flagged by optical, observed) are
   packed into one byte, and a year's months are exported together as one image asset. The county
   tables and the flood-frequency map are then read from those assets, so the expensive part runs
   once per month.

Earth Engine reads the imagery at the processing scale (flood.scale_m), averaging the finer pixels
from its image pyramids. At 100 m that averaging also smooths out radar speckle.

`region` in these functions is only used to find the scenes that touch the area (filterBounds);
pass a simple outline (earthengine.search_area_to_ee), not the detailed one.
"""

import ee

from ssd_monitor.config import FloodConfig, OpticalConfig, SarConfig
from ssd_monitor.dates import month_window
from ssd_monitor.earthengine import county_stats, with_fallback

FLOOD_BANDS = ["flooded", "flooded_sar", "flooded_optical", "observed"]
FLAG_BITS = {"flooded": 1, "flooded_sar": 2, "flooded_optical": 4, "observed": 8}


def layer_band(month: str) -> str:
    """Name of a month's band in the yearly layers asset, e.g. "m2020_09"."""
    return "m" + month.replace("-", "_")


def unpack_flags(value: int) -> dict[str, bool]:
    """The four flags packed in one layer value (the same decoding the county tables use)."""
    return {name: bool(value & bit) for name, bit in FLAG_BITS.items()}


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


def sar_water(sar: SarConfig, region: ee.Geometry, month: str) -> ee.Image:
    """1 where the radar sees open water this month, 0 where it doesn't, masked where it saw nothing."""
    start, end = month_window(month)
    flags = []
    for orbit_pass in sar.orbit_passes:
        images = s1_images(sar, region, orbit_pass).filterDate(start, end)
        median_vv = with_fallback(images, ["VV", "VH"]).median().select("VV")
        flags.append(median_vv.lte(sar.vv_water_max_db).rename("water"))
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
    """1 where flooding is not assessed: permanent water or steep slopes.

    unmask(0, False) fills without keeping the input's footprint: with the default (keep the
    footprint), the derived slope image came back fully masked and masked everything downstream.
    """
    occurrence = ee.Image(cfg.permanent_water.collection).select("occurrence").unmask(0, False)
    permanent = occurrence.gte(cfg.permanent_water.min_occurrence_pct)
    steep = ee.Terrain.slope(ee.Image(cfg.terrain.dem)).gt(cfg.terrain.max_slope_deg).unmask(0, False)
    return permanent.Or(steep).rename("excluded")


def monthly_flood(cfg: FloodConfig, region: ee.Geometry, month: str) -> ee.Image:
    """Four 0/1 bands for one month: flooded, flooded_sar, flooded_optical and observed.

    flooded_sar and flooded_optical say which sensor flagged each flooded pixel (often both).
    observed is 1 where at least one sensor saw the pixel that month.
    """
    sar = sar_water(cfg.sar, region, month)
    optical = optical_water(cfg.optical, region, month)
    assessed = excluded(cfg).Not()

    observed = sar.mask().gt(0).Or(optical.mask().gt(0)).And(assessed)
    sar_flag = sar.unmask(0).And(assessed)
    optical_flag = optical.unmask(0).And(assessed)
    either = sar_flag.Or(optical_flag)
    # connectedPixelCount stops counting at maxSize, so maxSize = the minimum patch size answers
    # "is this patch big enough?" exactly; a larger maxSize gave identical areas at ~2.6x the cost.
    patch_size = either.selfMask().connectedPixelCount(cfg.min_connected_pixels, True).unmask(0)
    big_enough = patch_size.gte(cfg.min_connected_pixels)

    return (
        ee.Image.cat(
            [either.And(big_enough), sar_flag.And(big_enough), optical_flag.And(big_enough), observed]
        )
        .rename(FLOOD_BANDS)
        .toByte()
    )


def pack_flags(image: ee.Image) -> ee.Image:
    """One byte per pixel holding the four 0/1 flood bands (see FLAG_BITS).

    Built with band arithmetic: summing the bands with Image.reduce gave values that mixed flags
    from different pixels (for example "flooded" without "observed").
    """
    packed = ee.Image(0)
    for name, bit in FLAG_BITS.items():
        packed = packed.add(image.select(name).multiply(bit))
    return packed.toByte()


def flood_layers(cfg: FloodConfig, region: ee.Geometry, months: list[str]) -> ee.Image:
    """One packed byte band per month (see FLAG_BITS), plus months_flooded and excluded."""
    monthly = [monthly_flood(cfg, region, m) for m in months]
    packed = [pack_flags(image).rename(layer_band(m)) for image, m in zip(monthly, months, strict=True)]
    count = ee.ImageCollection([image.select("flooded") for image in monthly]).sum().toByte()
    return ee.Image.cat([*packed, count.rename("months_flooded"), excluded(cfg).toByte()])


def decode_layer(layers: ee.Image, month: str) -> ee.Image:
    """The four 0/1 flags of one month, unpacked from a layers asset."""
    packed = layers.select(layer_band(month))
    return ee.Image.cat([packed.bitwiseAnd(bit).gt(0) for bit in FLAG_BITS.values()]).rename(FLOOD_BANDS)


def table_from_layers(
    layers: ee.Image, months: list[str], counties: ee.FeatureCollection, scale: float
) -> ee.FeatureCollection:
    """Areas in km2 per county-month (flooded_km2, flooded_sar_km2, flooded_optical_km2, observed_km2)."""
    km2 = ee.Image.pixelArea().divide(1e6)
    images = [decode_layer(layers, m).multiply(km2).rename([f"{b}_km2" for b in FLOOD_BANDS]) for m in months]
    return county_stats(images, months, counties, ee.Reducer.sum(), scale)
