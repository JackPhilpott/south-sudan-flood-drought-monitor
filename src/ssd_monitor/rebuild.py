"""Rebuild the monthly flood layers locally with the chosen radar fix, and split persistent from new water.

No Earth Engine here: everything reads the local cache (layer_cache.py). For every month, a strip of
rows at a time:

1. The fix: start from the stored flags and remove radar-only water where any of the fix's conditions
   holds (flood.radar_fix in config.yml), then re-apply the small-patch filter.
2. Persistent water: water in both March and April of the preceding dry season, the driest point of
   the year, after the fix. May to December use that year's March and April; January to April use
   the year before's (2019, the first year, uses its own). New water is the rest.
3. Add up whole-pixel areas per county (no mask weighting: see exports.py on the first run's tables),
   JRC agreement areas for the months JRC covers, and per pixel the number of months with water and
   with new water, for the frequency maps.
"""

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window

from ssd_monitor import layer_cache
from ssd_monitor.config import Config
from ssd_monitor.local_fixes import StripReader
from ssd_monitor.rasters import NODATA, Grid

FLOOD_COLUMNS = [
    "flooded_km2",
    "flooded_sar_km2",
    "flooded_optical_km2",
    "observed_km2",
    "persistent_km2",
    "new_km2",
]
JRC_COLUMNS = ["jrc_observed_km2", "jrc_water_km2", "flagged_jrc_observed_km2", "flagged_jrc_water_km2"]
FREQUENCY_BANDS = ("months_water", "months_new", "status")  # status: 0 assessed, 1 excluded, 255 outside


def baseline_year(month: str, first_year: int) -> int:
    """The year whose March-April water counts as persistent for `month`."""
    year, calendar_month = int(month[:4]), int(month[5:7])
    return year if calendar_month >= 5 else max(year - 1, first_year)


def rebuild(
    cfg: Config,
    counties: gpd.GeoDataFrame,
    grid: Grid,
    area_by_row: np.ndarray,
    months: list[str],
    frequency_path: Path,
    strip_rows: int = 1000,
    log=None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The county-month flood table (FLOOD_COLUMNS) and JRC agreement areas (JRC_COLUMNS, JRC months only).

    Also writes frequency_path: a 100 m GeoTIFF with the months of water and of new water per pixel.
    """
    conditions = cfg.flood.radar_fix
    min_px = cfg.flood.min_connected_pixels
    codes = sorted(counties["county_code"])
    n = len(codes)
    first_year = int(months[0][:4])
    years = sorted({int(m[:4]) for m in months})
    flood_sums = {m: {k: np.zeros(n + 1) for k in FLOOD_COLUMNS} for m in months}
    jrc_sums: dict[str, dict[str, np.ndarray]] = {}
    _create_frequency(frequency_path, grid)
    all_strips = layer_cache.strips(grid.height, strip_rows, min_px)
    for number, strip in enumerate(all_strips, start=1):
        read0, read1, core0, core1 = strip
        shape = (read1 - read0, grid.width)
        baselines = {}
        reader = StripReader(cfg, counties, grid, area_by_row, strip)
        for year in years:
            water = []
            for calendar_month in cfg.flood.persistent_months:
                px = reader.month(f"{year}-{calendar_month:02d}")
                water.append(_to_array(px, px.kept(conditions, min_px), shape))
            baselines[year] = np.logical_and.reduce(water)
        reader = StripReader(cfg, counties, grid, area_by_row, strip)
        months_water = np.zeros(shape, dtype=np.uint8)
        months_new = np.zeros(shape, dtype=np.uint8)
        status = None
        for month in months:
            px = reader.month(month)
            keep = px.kept(conditions, min_px)
            persistent = keep & baselines[baseline_year(month, first_year)][px.rows, px.cols]
            new = keep & ~persistent
            sums = flood_sums[month]
            sums["flooded_km2"] += px.node_sum(keep, n)
            sums["flooded_sar_km2"] += px.node_sum(keep & px.node["sar"], n)
            sums["flooded_optical_km2"] += px.node_sum(keep & px.node["optical"], n)
            sums["observed_km2"] += px.dense_sum(px.observed, n)
            sums["persistent_km2"] += px.node_sum(persistent, n)
            sums["new_km2"] += px.node_sum(new, n)
            if px.jrc_water is not None:
                jrc = jrc_sums.setdefault(month, {k: np.zeros(n + 1) for k in JRC_COLUMNS})
                jrc["jrc_observed_km2"] += px.dense_sum(px.jrc_observed, n)
                jrc["jrc_water_km2"] += px.dense_sum(px.jrc_water, n)
                jrc["flagged_jrc_observed_km2"] += px.node_sum(keep & px.node["jrc_observed"], n)
                jrc["flagged_jrc_water_km2"] += px.node_sum(keep & px.node["jrc_water"], n)
            months_water[px.rows[keep], px.cols[keep]] += 1
            months_new[px.rows[new], px.cols[new]] += 1
            if status is None:
                status = _status(cfg, month, read0, read1)
        core = slice(core0 - read0, core1 - read0)
        _write_frequency_rows(frequency_path, core0, months_water[core], months_new[core], status[core])
        if log:
            log(f"strip {number}/{len(all_strips)} done")
    flood = _frame(codes, flood_sums)
    jrc = _frame(codes, jrc_sums)
    return flood, jrc


def _to_array(px, keep: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    out = np.zeros(shape, dtype=bool)
    out[px.rows[keep], px.cols[keep]] = True
    return out


def _status(cfg: Config, month: str, read0: int, read1: int) -> np.ndarray:
    stored = layer_cache.read_rows(
        layer_cache.layer_dir(cfg.cache, f"flood_layers_{month[:4]}"),
        ["m" + month.replace("-", "_"), "excluded"],
        read0,
        read1,
    )
    status = np.where(stored["excluded"] == 1, 1, 0).astype(np.uint8)
    status[stored["m" + month.replace("-", "_")] == 255] = 255
    return status


def _frame(codes: list[str], sums: dict[str, dict[str, np.ndarray]]) -> pd.DataFrame:
    frames = []
    for month, columns in sums.items():
        frame = pd.DataFrame({"county_code": codes, "month": month})
        for key, values in columns.items():
            frame[key] = values[1:]
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _create_frequency(path: Path, grid: Grid) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "width": grid.width,
        "height": grid.height,
        "count": len(FREQUENCY_BANDS),
        "dtype": "uint8",
        "crs": "EPSG:4326",
        "transform": layer_cache.affine(grid),
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        for i, band in enumerate(FREQUENCY_BANDS, start=1):
            dst.set_band_description(i, band)


def _write_frequency_rows(path: Path, first_row: int, *bands: np.ndarray) -> None:
    height, width = bands[0].shape
    with rasterio.open(path, "r+") as dst:
        for i, values in enumerate(bands, start=1):
            dst.write(values, i, window=Window(0, first_row, width, height))


def frequency_cells(path: Path, cells: Grid, band: str) -> tuple[np.ndarray, np.ndarray]:
    """Mean months (x10) and excluded share (%) per frequency-map cell, as int16 with NODATA outside.

    The 100 m pixels are averaged into each cell by area (GDAL's average resampling), leaving out
    pixels outside the country; excluded pixels count as 0 months, as in the first run's map.
    """
    with rasterio.open(path) as src:
        index = {name: i for i, name in enumerate(src.descriptions, start=1)}
        status = src.read(index["status"])
        months = src.read(index[band]).astype("float32")
        months[status == 255] = np.nan
        excluded = np.where(status == 255, np.nan, (status == 1).astype("float32"))
        out = {}
        for name, values in (("months", months), ("excluded", excluded)):
            target = np.full((cells.height, cells.width), np.nan, dtype="float32")
            reproject(
                values,
                target,
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=np.nan,
                dst_transform=layer_cache.affine(cells),
                dst_crs=src.crs,
                dst_nodata=np.nan,
                resampling=Resampling.average,
            )
            out[name] = target
    months_x10 = np.where(np.isnan(out["months"]), NODATA, np.round(out["months"] * 10)).astype("int16")
    excluded_pct = np.where(np.isnan(out["excluded"]), NODATA, np.round(out["excluded"] * 100)).astype(
        "int16"
    )
    return months_x10, excluded_pct


def low_lying_land(
    cfg: Config,
    counties: gpd.GeoDataFrame,
    grid: Grid,
    area_by_row: np.ndarray,
    max_hand_m: float,
    strip_rows=1000,
) -> pd.DataFrame:
    """km2 of each county's assessed land at most max_hand_m above the nearest drainage (MERIT Hydro HAND).

    Derived from MERIT Hydro, so this table is published under ODbL 1.0 (see the README).
    """
    from ssd_monitor.local_fixes import county_index

    codes = sorted(counties["county_code"])
    low = np.zeros(len(codes) + 1)
    assessed = np.zeros(len(codes) + 1)
    first_year = cfg.start_year
    for read0, read1, _, _ in layer_cache.strips(grid.height, strip_rows, 0):
        county, _ = county_index(counties, grid, read0, read1)
        hand_cm = layer_cache.read_rows(
            layer_cache.layer_dir(cfg.cache, "hand_cm"), ["hand_cm"], read0, read1
        )["hand_cm"]
        excluded = layer_cache.read_rows(
            layer_cache.layer_dir(cfg.cache, f"flood_layers_{first_year}"), ["excluded"], read0, read1
        )["excluded"]
        ok = (county > 0) & (excluded == 0)
        rows, cols = np.nonzero(ok)
        area = area_by_row[read0:read1][rows]
        assessed += np.bincount(county[rows, cols], weights=area, minlength=len(codes) + 1)
        is_low = hand_cm[rows, cols] <= round(max_hand_m * 100)
        low += np.bincount(county[rows, cols][is_low], weights=area[is_low], minlength=len(codes) + 1)
    return pd.DataFrame({"county_code": codes, "assessed_km2": assessed[1:], "low_lying_km2": low[1:]})
