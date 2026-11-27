"""Candidate fixes for false radar water, built on the stored yearly flood layers (Earth Engine).

The first full run flagged burnt grassland and some dry soils as water in the dry season, because
they are as dark on radar as calm water. Four kinds of fix are tested, each removing radar-only
water (pixels flagged by radar but not by optical imagery) where:

  (a) MODIS burned area (MCD64A1) shows a burn recently (a1), or since 1 November in the burn
      season, November to May (a2: no burn mask from June to October);
  (b) the land is high above the nearest drainage (MERIT Hydro HAND), where floods can't pond;
  (c) the pixel was also dark in the dry-season radar reference (January-March 2017-2019), so
      radar alone can't tell water from dry ground there and optical imagery has to agree;
  (e) the pixel is next to a Sentinel-1 scene outline, where the scene edges leave dark lines.

No Sentinel-1 or Sentinel-2 imagery is reprocessed: everything reads the stored layers and small
auxiliary datasets. For the test, each pixel of a month gets one integer code (pixel_classes.py
lists the bits), and the km2 of each code per county-month is exported, so every fix and every
combination is scored locally from a single pass.
"""

import ee

from ssd_monitor import flood
from ssd_monitor.config import Config
from ssd_monitor.dates import burn_window, month_window, months_in_years, season_burn_window
from ssd_monitor.earthengine import with_fallback
from ssd_monitor.layer_cache import CacheFile
from ssd_monitor.pixel_classes import class_bits, hand_bit

JRC_LAST_YEAR = 2021  # JRC Global Surface Water 1.4 monthly and yearly history end in 2021


def _filled(image: ee.Image) -> ee.Image:
    """Masked pixels become 0 everywhere (unmask(0, False): see flood.excluded)."""
    return image.unmask(0, False)


def burned(cfg: Config, months: list[str]) -> ee.Image:
    """1 where MODIS mapped a burn in any of these months, else 0.

    The burn dates are combined first and filled once (filling each month separately cost more).
    The masked placeholder keeps the reduction defined if a month has no burned-area image.
    """
    start, _ = month_window(months[0])
    _, end = month_window(months[-1])
    burns = ee.ImageCollection(cfg.radar_fix_test.burn_collection).filterDate(start, end).select("BurnDate")
    latest = with_fallback(burns, ["BurnDate"]).max()
    return _filled(latest.gt(0))


def dark_reference(cfg: Config, root: str) -> ee.Image:
    """1 where either orbit direction's dry-season reference VV was as dark as the water threshold."""
    reference = ee.Image(f"{root}/{cfg.radar_fix_test.dark_reference_asset}")
    dark = ee.Image(0)
    for orbit_pass in cfg.flood.sar.orbit_passes:
        dark = dark.Or(_filled(reference.select(f"VV_{orbit_pass}").lte(cfg.flood.sar.vv_water_max_db)))
    return dark


def scene_edges(cfg: Config, search: ee.Geometry, month: str, projection: ee.Projection) -> ee.Image:
    """1 within scene_edge_m of the outline of any Sentinel-1 scene used that month."""
    start, end = month_window(month)
    scenes = ee.ImageCollection([])
    for orbit_pass in cfg.flood.sar.orbit_passes:
        scenes = scenes.merge(flood.s1_images(cfg.flood.sar, search, orbit_pass).filterDate(start, end))
    outlines = ee.FeatureCollection(
        scenes.map(lambda image: ee.Feature(ee.Geometry(image.get("system:footprint"))))
    )
    # paint() draws lines centred on the outline; width is in pixels of the layers' 100 m grid.
    width = 2 * cfg.radar_fix_test.scene_edge_m / cfg.flood.scale_m
    return ee.Image(0).byte().paint(outlines, 1, width).reproject(projection)


def class_planes(
    cfg: Config, root: str, layers: ee.Image, search: ee.Geometry, month: str
) -> dict[str, ee.Image]:
    """The 0/1 image of every pixel class for one month (keys as in pixel_classes.class_bits)."""
    rt = cfg.radar_fix_test
    stored = flood.decode_layer(layers, month)
    start, end = month_window(month)
    jrc = _filled(ee.ImageCollection(rt.jrc_monthly).filterDate(start, end).first().select("water"))
    jrc_water = jrc.eq(2)
    year = int(month[:4])
    jrc_year = _filled(
        ee.ImageCollection(rt.jrc_yearly).filter(ee.Filter.eq("year", year)).first().select("waterClass")
    ).gte(2)
    hand = _filled(ee.Image(rt.hand_image).select("hnd"))
    calendar_month = int(month[5:7])
    burn_recent = burned(cfg, burn_window(month, rt.burn_recent_months))
    season = season_burn_window(month, rt.burn_season_start_month, rt.in_burn_season(calendar_month))
    burn_season = burned(cfg, season) if season else ee.Image(0)
    planes = {
        "sar": stored.select("flooded_sar"),
        "optical": stored.select("flooded_optical"),
        "observed": stored.select("observed"),
        "jrc_observed": jrc.gt(0),
        "jrc_water": jrc_water,
        "jrc_near": jrc_water.focalMax(rt.jrc_near_m, "square", "meters"),
        "jrc_year": jrc_year,
        "burn_recent": burn_recent,
        "burn_season": burn_season,
        "dark_reference": dark_reference(cfg, root),
        "scene_edge": scene_edges(cfg, search, month, layers.projection()),
        "excluded": layers.select("excluded"),
    }
    planes |= {hand_bit(t): hand.gt(t) for t in rt.hand_thresholds_m}
    return planes


def class_image(cfg: Config, root: str, layers: ee.Image, search: ee.Geometry, month: str) -> ee.Image:
    """One integer code per pixel of the country (masked outside it, as the stored layers are).

    Permanent water and steep slopes keep their own bit ("excluded") rather than a mask, and are
    dropped when the table is decoded: masking them here made Earth Engine compute the code twice.
    Built with band arithmetic, like flood.pack_flags, so each pixel's bits come from that pixel.
    """
    planes = class_planes(cfg, root, layers, search, month)
    code = ee.Image(0)
    for name, bit in class_bits(cfg.radar_fix_test).items():
        code = code.add(planes[name].multiply(bit))
    return code.toInt16().rename("code")


def class_table(
    cfg: Config,
    root: str,
    layers: ee.Image,
    months: list[str],
    counties: ee.FeatureCollection,
    search: ee.Geometry,
) -> ee.FeatureCollection:
    """km2 of each pixel code per county-month: one feature per county-month, the areas as JSON.

    `groups` holds a JSON list of {"code": ..., "sum": km2} (Earth Engine's grouped reducer output).
    """
    km2 = ee.Image.pixelArea().divide(1e6).rename("km2")
    reducer = ee.Reducer.sum().group(groupField=1, groupName="code")
    tables = []
    for month in months:
        code = class_image(cfg, root, layers, search, month)
        # The grouped reducer skips pixels whose code is masked (outside the country).
        stats = km2.addBands(code).reduceRegions(
            collection=counties, reducer=reducer, scale=cfg.flood.scale_m, crs="EPSG:4326", tileScale=4
        )
        tables.append(
            stats.map(
                lambda f, month=month: ee.Feature(
                    None,
                    {
                        "county_code": f.get("county_code"),
                        "month": month,
                        "groups": ee.String.encodeJSON(f.get("groups")),
                    },
                )
            )
        )
    return ee.FeatureCollection(tables).flatten()


# --- Layers for the local cache (layer_cache.py) --------------------------------------------------
# Every image below is fetched on the stored layers' grid. Masked pixels get the file's nodata value.


def cache_files(
    cfg: Config, root: str, search: ee.Geometry, years: list[int]
) -> list[tuple[CacheFile, ee.Image]]:
    """What the local fixes need for these years: (file spec, image to download), in download order.

    Burned area starts a year early, because the burn season of January to May starts in November.
    JRC monthly and yearly water exist only up to 2021.
    """
    rt = cfg.radar_fix_test
    projection = ee.Image(f"{root}/flood_layers_{years[0]}").projection()
    files: list[tuple[CacheFile, ee.Image]] = []
    for year in years:
        months = months_in_years(year, year)
        stored = ee.Image(f"{root}/flood_layers_{year}")
        names = (*[flood.layer_band(m) for m in months], "months_flooded", "excluded")
        files.append(
            (CacheFile(f"flood_layers_{year}", names, "uint8", 255), stored.unmask(255, False).toByte())
        )
    for year in range(years[0] - 1, years[-1] + 1):
        months = months_in_years(year, year)
        image = ee.Image.cat([burned(cfg, [m]).toByte().rename(f"b{m.replace('-', '_')}") for m in months])
        files.append((CacheFile(f"burned_{year}", tuple(image_band_names(months, "b")), "uint8", 255), image))
    for year in years:
        months = months_in_years(year, year)
        edges = [
            scene_edges(cfg, search, m, projection).toByte().rename(f"e{m.replace('-', '_')}") for m in months
        ]
        files.append(
            (
                CacheFile(f"scene_edges_{year}", tuple(image_band_names(months, "e")), "uint8", 255),
                ee.Image.cat(edges),
            )
        )
    jrc_years = [y for y in years if y <= JRC_LAST_YEAR]
    for year in jrc_years:
        months = months_in_years(year, year)
        bands = []
        for m in months:
            start, end = month_window(m)
            water = ee.ImageCollection(rt.jrc_monthly).filterDate(start, end).first().select("water")
            bands.append(_filled(water).toByte().rename(f"j{m.replace('-', '_')}"))
        files.append(
            (
                CacheFile(f"jrc_monthly_{year}", tuple(image_band_names(months, "j")), "uint8", 255),
                ee.Image.cat(bands),
            )
        )
    if jrc_years:
        yearly = [
            _filled(
                ee.ImageCollection(rt.jrc_yearly).filter(ee.Filter.eq("year", y)).first().select("waterClass")
            )
            .toByte()
            .rename(f"y{y}")
            for y in jrc_years
        ]
        files.append(
            (CacheFile("jrc_yearly", tuple(f"y{y}" for y in jrc_years), "uint8", 255), ee.Image.cat(yearly))
        )
    hand_cm = ee.Image(rt.hand_image).select("hnd").multiply(100).round().clamp(0, 65534)
    files.append(
        (
            CacheFile("hand_cm", ("hand_cm",), "uint16", 65535),
            hand_cm.unmask(65535, False).toUint16().rename("hand_cm"),
        )
    )
    files.append(
        (
            CacheFile("dark_reference", ("dark",), "uint8", 255),
            dark_reference(cfg, root).toByte().rename("dark"),
        )
    )
    return files


def image_band_names(months: list[str], prefix: str) -> list[str]:
    return [f"{prefix}{m.replace('-', '_')}" for m in months]


def optical_clear(cfg: Config, search: ee.Geometry, month: str) -> ee.Image:
    """1 where at least one Sentinel-2 scene that month had a clear view of the pixel, else 0.

    Uses the optical layer's own rules (scenes up to max_scene_cloud_pct cloud; Cloud Score+ cs_cdf of
    at least min_clear_score), but only the Cloud Score+ band, not the imagery. The stored layers
    don't record this: their "observed" flag means either sensor, and radar sees almost every pixel.
    """
    opt = cfg.flood.optical
    start, end = month_window(month)
    scenes = (
        ee.ImageCollection(opt.collection)
        .filterBounds(search)
        .filterDate(start, end)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", opt.max_scene_cloud_pct))
    )
    clear = scenes.linkCollection(ee.ImageCollection(opt.cloud_score_collection), ["cs_cdf"]).map(
        lambda image: image.select("cs_cdf").gte(opt.min_clear_score).rename("clear")
    )
    return _filled(with_fallback(clear, ["clear"]).max().gt(0))


def optical_clear_file(
    cfg: Config, search: ee.Geometry, months: list[str], name: str
) -> tuple[CacheFile, ee.Image]:
    """A cache file with one optical clear-view band per month (o2020_01, ...)."""
    bands = [optical_clear(cfg, search, m).toByte().rename(f"o{m.replace('-', '_')}") for m in months]
    return CacheFile(name, tuple(image_band_names(months, "o")), "uint8", 255), ee.Image.cat(bands)
