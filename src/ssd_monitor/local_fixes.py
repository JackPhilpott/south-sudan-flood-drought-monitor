"""Test the candidate radar fixes locally, on the cached 100 m layers. No Earth Engine here.

For each month of the test years, a strip of rows at a time:

1. Read the stored flags (radar, optical, observed, excluded) and the extra layers (JRC monthly and
   yearly water, MODIS burned area, MERIT Hydro HAND, the dry-season radar reference, Sentinel-1
   scene outlines). All share one grid, so a pixel means the same place in every file.
2. For each rule (every combination of the candidate conditions, pixel_classes.candidate_rules):
   remove radar-only pixels where any of the rule's conditions holds, then re-apply the small-patch
   filter exactly as the stored layers were made (8-connected patches of at least
   min_connected_pixels pixels).
3. Add up km2 per county: flagged, flagged where JRC observed, flagged where JRC saw water, and the
   radar-only area left. Pixel areas are Earth Engine's own (exports.download_pixel_area).

Strips carry a halo of min_connected_pixels rows, which makes the patch filter exact (see
layer_cache.strips). Pixels outside the counties, on permanent water or on steep slopes are left out.
"""

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio.features import rasterize
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from ssd_monitor import layer_cache
from ssd_monitor.config import Config
from ssd_monitor.dates import burn_window, season_burn_window
from ssd_monitor.pixel_classes import class_bits, hand_bit
from ssd_monitor.rasters import Grid

FLAG_BITS = {"flooded": 1, "flooded_sar": 2, "flooded_optical": 4, "observed": 8}  # as in flood.py
OUTSIDE = 255  # nodata in the cached stored layers
EIGHT_CONNECTED = np.ones((3, 3), dtype=bool)
JRC_LAST_YEAR = 2021


def county_index(
    counties: gpd.GeoDataFrame, grid: Grid, first_row: int, last_row: int
) -> tuple[np.ndarray, list[str]]:
    """County number (1..n, 0 outside) of each pixel in the rows, by pixel centre, and the codes in order."""
    codes = sorted(counties["county_code"])
    number = {code: i for i, code in enumerate(codes, start=1)}
    sub = layer_cache.tile_grid(grid, first_row, 0, last_row - first_row, grid.width)
    shapes = (
        (geom, number[code]) for geom, code in zip(counties.geometry, counties["county_code"], strict=True)
    )
    raster = rasterize(
        shapes, out_shape=(sub.height, sub.width), transform=layer_cache.affine(sub), fill=0, dtype="uint8"
    )
    return raster, codes


def patch_filter(mask: np.ndarray, min_pixels: int) -> np.ndarray:
    """True where the pixel is in an 8-connected patch of at least min_pixels True pixels."""
    labels, count = ndimage.label(mask, structure=EIGHT_CONNECTED)
    if count == 0:
        return mask.copy()
    sizes = np.bincount(labels.ravel()).astype(np.int32)
    sizes[0] = 0
    return sizes[labels] >= min_pixels


def _file(cfg: Config, name: str) -> Path:
    return layer_cache.layer_dir(cfg.cache, name)


def _band(month: str, prefix: str) -> str:
    return f"{prefix}{month.replace('-', '_')}"


def load_planes(cfg: Config, month: str, first_row: int, last_row: int) -> dict[str, np.ndarray]:
    """Every pixel class for one month and range of rows, as True/False arrays (plus "assessed").

    Keys match pixel_classes.class_bits, so the local codes can be checked against Earth Engine's.
    """
    rt = cfg.radar_fix_test
    year = int(month[:4])
    stored = layer_cache.read_rows(
        _file(cfg, f"flood_layers_{year}"), ["m" + month.replace("-", "_"), "excluded"], first_row, last_row
    )
    packed = stored["m" + month.replace("-", "_")]
    inside = packed != OUTSIDE
    planes = {
        "sar": inside & (packed & FLAG_BITS["flooded_sar"] > 0),
        "optical": inside & (packed & FLAG_BITS["flooded_optical"] > 0),
        "observed": inside & (packed & FLAG_BITS["observed"] > 0),
        "excluded": inside & (stored["excluded"] == 1),
    }
    planes["assessed"] = inside & ~planes["excluded"]
    if year <= JRC_LAST_YEAR:
        jrc = layer_cache.read_rows(
            _file(cfg, f"jrc_monthly_{year}"), [_band(month, "j")], first_row, last_row
        )[_band(month, "j")]
        jrc_year = layer_cache.read_rows(_file(cfg, "jrc_yearly"), [f"y{year}"], first_row, last_row)[
            f"y{year}"
        ]
        planes["jrc_observed"] = (jrc > 0) & (jrc != OUTSIDE)
        planes["jrc_water"] = jrc == 2
        # Earth Engine's focalMax with a 150 m square radius at 100 m is a 3 x 3 window.
        planes["jrc_near"] = ndimage.maximum_filter(planes["jrc_water"], size=3)
        planes["jrc_year"] = (jrc_year >= 2) & (jrc_year != OUTSIDE)
    calendar_month = int(month[5:7])
    windows = {
        "burn_recent": burn_window(month, rt.burn_recent_months),
        "burn_season": season_burn_window(
            month, rt.burn_season_start_month, rt.in_burn_season(calendar_month)
        ),
    }
    for name, months in windows.items():
        burned = np.zeros(packed.shape, dtype=bool)
        for m in months:
            band = _band(m, "b")
            burned |= (
                layer_cache.read_rows(_file(cfg, f"burned_{m[:4]}"), [band], first_row, last_row)[band] == 1
            )
        planes[name] = burned
    hand_cm = layer_cache.read_rows(_file(cfg, "hand_cm"), ["hand_cm"], first_row, last_row)["hand_cm"]
    for t in rt.hand_thresholds_m:
        planes[hand_bit(t)] = (hand_cm != 65535) & (hand_cm > round(t * 100))
    planes["dark_reference"] = (
        layer_cache.read_rows(_file(cfg, "dark_reference"), ["dark"], first_row, last_row)["dark"] == 1
    )
    edge_band = _band(month, "e")
    planes["scene_edge"] = (
        layer_cache.read_rows(_file(cfg, f"scene_edges_{year}"), [edge_band], first_row, last_row)[edge_band]
        == 1
    )
    return planes


def kept(planes: dict[str, np.ndarray], conditions: tuple[str, ...], min_pixels: int) -> np.ndarray:
    """Flagged pixels after a rule: radar-only pixels with any condition removed, then the patch filter."""
    flagged = planes["sar"] | planes["optical"]
    if conditions:
        drop = np.zeros_like(flagged)
        for name in conditions:
            drop |= planes[name]
        flagged = flagged & ~(drop & planes["sar"] & ~planes["optical"])
    return patch_filter(flagged, min_pixels)


NEIGHBOURS = ((0, 1), (1, -1), (1, 0), (1, 1))  # half the 8-neighbourhood: edges count both ways


def flagged_graph(union: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The flagged pixels as graph nodes (rows, columns) and the 8-neighbour edges between them.

    Only about 0.3% of pixels are ever flagged, so finding patches on this graph is much faster than
    labelling the whole array for every rule, and gives the same patches (tests check this).
    """
    rows, cols = np.nonzero(union)
    height, width = union.shape
    linear = rows.astype(np.int64) * width + cols
    starts, ends = [], []
    if len(linear):
        for dr, dc in NEIGHBOURS:
            r, c = rows + dr, cols + dc
            inside = (r < height) & (c >= 0) & (c < width)
            source = np.nonzero(inside)[0]
            target = r[inside].astype(np.int64) * width + c[inside]
            position = np.minimum(np.searchsorted(linear, target), len(linear) - 1)
            hit = linear[position] == target
            starts.append(source[hit])
            ends.append(position[hit])
    empty = np.zeros(0, dtype=np.int64)
    return rows, cols, np.concatenate(starts) if starts else empty, np.concatenate(ends) if ends else empty


def big_patches(keep: np.ndarray, starts: np.ndarray, ends: np.ndarray, min_pixels: int) -> np.ndarray:
    """Which kept nodes lie in 8-connected patches of at least min_pixels kept nodes."""
    n = len(keep)
    if n == 0:
        return keep.copy()
    both = keep[starts] & keep[ends]
    graph = coo_matrix((np.ones(int(both.sum()), dtype=np.int8), (starts[both], ends[both])), shape=(n, n))
    _, labels = connected_components(graph, directed=False)
    sizes = np.bincount(labels[keep], minlength=labels.max() + 1)
    return keep & (sizes[labels] >= min_pixels)


def _sums(index: np.ndarray, area: np.ndarray, n: int) -> np.ndarray:
    """km2 per county number (index 0 = outside or not assessed, dropped later)."""
    return np.bincount(index, weights=area, minlength=n + 1)


def _read(cfg: Config, layer: str, band: str, first_row: int, last_row: int) -> np.ndarray:
    return layer_cache.read_rows(_file(cfg, layer), [band], first_row, last_row)[band]


def _has_band(cfg: Config, layer: str, band: str) -> bool:
    folder = _file(cfg, layer)
    return (folder / "layer.json").exists() and band in layer_cache.read_meta(folder)[0].bands


@dataclass
class MonthPixels:
    """One month of one strip: dense layers where needed, everything else at the flagged pixels only."""

    month: str
    county: np.ndarray  # county number of each pixel; 0 outside the strip's core rows or not assessed
    row_area: np.ndarray  # km2 of a pixel in each row of the strip
    observed: np.ndarray  # stored flag: seen by either sensor
    jrc_observed: np.ndarray | None  # JRC monthly water, up to 2021
    jrc_water: np.ndarray | None
    rows: np.ndarray  # the flagged pixels (radar or optical) ...
    cols: np.ndarray
    starts: np.ndarray  # ... and the 8-neighbour edges between them
    ends: np.ndarray
    node: dict[str, np.ndarray]  # flags and conditions at the flagged pixels

    @property
    def radar_only(self) -> np.ndarray:
        return self.node["sar"] & ~self.node["optical"]

    def dense_sum(self, mask: np.ndarray, n: int) -> np.ndarray:
        r, c = np.nonzero(mask & (self.county > 0))
        return _sums(self.county[r, c], self.row_area[r], n)

    def node_sum(self, flag: np.ndarray, n: int) -> np.ndarray:
        county = self.county[self.rows, self.cols]
        return _sums(county[flag], self.row_area[self.rows][flag], n)

    def kept(self, conditions: tuple[str, ...], min_pixels: int) -> np.ndarray:
        """Flagged pixels left after a rule: radar-only pixels under a condition go, then the patch filter."""
        drop = np.zeros(len(self.rows), dtype=bool)
        for condition in conditions:
            drop |= self.node[condition]
        return big_patches(~(drop & self.radar_only), self.starts, self.ends, min_pixels)


class StripReader:
    """Reads the cached layers for one strip of rows, a month at a time.

    The static layers (counties, HAND, dry-season reference) are read once per strip, and each
    month's burned area once, for every burn window that needs it. Months must be asked for in order.
    """

    def __init__(self, cfg: Config, counties: gpd.GeoDataFrame, grid: Grid, area_by_row, strip):
        self.cfg, self.rt = cfg, cfg.radar_fix_test
        self.read0, self.read1, core0, core1 = strip
        self.county_raster, self.codes = county_index(counties, grid, self.read0, self.read1)
        hand_cm = _read(cfg, "hand_cm", "hand_cm", self.read0, self.read1)
        self.static = {
            hand_bit(t): (hand_cm != 65535) & (hand_cm > round(t * 100)) for t in self.rt.hand_thresholds_m
        }
        self.static["dark_reference"] = _read(cfg, "dark_reference", "dark", self.read0, self.read1) == 1
        self.core_rows = np.zeros(self.read1 - self.read0, dtype=bool)
        self.core_rows[core0 - self.read0 : core1 - self.read0] = True
        self.row_area = area_by_row[self.read0 : self.read1]
        self.burned: dict[str, np.ndarray] = {}
        self.jrc_years: dict[int, np.ndarray] = {}

    def _rows(self, layer: str, band: str) -> np.ndarray:
        return _read(self.cfg, layer, band, self.read0, self.read1)

    def month(self, month: str) -> MonthPixels:
        rt, year = self.rt, int(month[:4])
        band = "m" + month.replace("-", "_")
        stored = layer_cache.read_rows(
            _file(self.cfg, f"flood_layers_{year}"), [band, "excluded"], self.read0, self.read1
        )
        packed = stored[band]
        inside = packed != OUTSIDE
        assessed = inside & (stored["excluded"] != 1)
        county = np.where(assessed & self.core_rows[:, None], self.county_raster, 0)
        sar = inside & (packed & FLAG_BITS["flooded_sar"] > 0)
        optical = inside & (packed & FLAG_BITS["flooded_optical"] > 0)
        observed = inside & (packed & FLAG_BITS["observed"] > 0)
        windows = {
            "burn_recent": burn_window(month, rt.burn_recent_months),
            "burn_season": season_burn_window(
                month, rt.burn_season_start_month, rt.in_burn_season(int(month[5:7]))
            ),
        }
        needed = {m for w in windows.values() for m in w}
        for m in sorted(needed - set(self.burned)):
            self.burned[m] = self._rows(f"burned_{m[:4]}", _band(m, "b")) == 1
        for m in [m for m in self.burned if m not in needed and m < month]:
            del self.burned[m]
        jrc_observed = jrc_water = None
        if year <= JRC_LAST_YEAR:
            water_code = self._rows(f"jrc_monthly_{year}", _band(month, "j"))
            if year not in self.jrc_years:
                self.jrc_years[year] = self._rows("jrc_yearly", f"y{year}")
            jrc_observed = (water_code > 0) & (water_code != OUTSIDE)
            jrc_water = water_code == 2

        rows, cols, starts, ends = flagged_graph(sar | optical)
        node = {"sar": sar[rows, cols], "optical": optical[rows, cols]}
        node["scene_edge"] = self._rows(f"scene_edges_{year}", _band(month, "e"))[rows, cols] == 1
        node |= {k: v[rows, cols] for k, v in self.static.items()}
        for name, window in windows.items():
            plane = np.zeros(len(rows), dtype=bool)
            for m in window:
                plane |= self.burned[m][rows, cols]
            node[name] = plane
        clear_layer, clear_band = f"optical_clear_{year}", _band(month, "o")
        if _has_band(self.cfg, clear_layer, clear_band):
            clear = self._rows(clear_layer, clear_band)[rows, cols] == 1
            node["optical_veto"] = clear
            dry_season = int(month[5:7]) not in rt.wet_season_months
            node["optical_veto_dry"] = clear if dry_season else np.zeros(len(rows), dtype=bool)
        if jrc_water is not None:
            padded = np.pad(jrc_water, 1)
            near = np.zeros(len(rows), dtype=bool)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    near |= padded[rows + 1 + dr, cols + 1 + dc]
            year_class = self.jrc_years[year][rows, cols]
            node["jrc_observed"] = jrc_observed[rows, cols]
            node["jrc_water"] = jrc_water[rows, cols]
            node["jrc_near"] = near
            node["jrc_year"] = (year_class >= 2) & (year_class != OUTSIDE)
        return MonthPixels(
            month, county, self.row_area, observed, jrc_observed, jrc_water, rows, cols, starts, ends, node
        )


RULE_KEYS = ("flagged", "flagged_jrc_observed", "flagged_jrc_water", "radar_only")


def rule_tables(
    cfg: Config,
    counties: gpd.GeoDataFrame,
    grid: Grid,
    area_by_row: np.ndarray,
    months: list[str],
    rules: dict[str, tuple[str, ...]],
    strip_rows: int = 1000,
    log=None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """km2 per rule, county and month, and the rule-independent km2 per county and month.

    rules table: rule, county_code, month, flagged_km2, flagged_jrc_observed_km2, flagged_jrc_water_km2,
    radar_only_km2. county table: county_code, month, assessed, stored flags, JRC areas, and where
    each sensor's stored flags disagree with JRC, by likely cause (fix_scores.false_positive_causes).
    Works a strip at a time with every month inside it, so the static layers are read once per strip.
    """
    min_px = cfg.flood.min_connected_pixels
    codes = sorted(counties["county_code"])
    n = len(codes)
    rule_sums = {m: {r: {k: np.zeros(n + 1) for k in RULE_KEYS} for r in rules} for m in months}
    county_sums: dict[str, dict[str, np.ndarray]] = {m: {} for m in months}
    all_strips = layer_cache.strips(grid.height, strip_rows, min_px)
    for number, strip in enumerate(all_strips, start=1):
        reader = StripReader(cfg, counties, grid, area_by_row, strip)
        for month in months:
            px = reader.month(month)
            sums = county_sums[month]

            def add(key, values, sums=sums):
                sums[key] = sums.get(key, np.zeros(n + 1)) + values

            add("assessed_km2", px.dense_sum(np.ones_like(px.observed), n))
            add("stored_observed_km2", px.dense_sum(px.observed, n))
            node, radar_only = px.node, px.radar_only
            for key, flag in {
                "stored_flooded_km2": np.ones(len(px.rows), dtype=bool),
                "stored_sar_km2": node["sar"],
                "stored_optical_km2": node["optical"],
                "radar_only_stored_km2": radar_only,
            }.items():
                add(key, px.node_sum(flag, n))
            jrc = px.jrc_water is not None
            if jrc:
                add("jrc_observed_km2", px.dense_sum(px.jrc_observed, n))
                add("jrc_water_km2", px.dense_sum(px.jrc_water, n))
                dry = node["jrc_observed"] & ~node["jrc_water"]
                causes = {
                    "next_to_jrc_water": node["jrc_near"],
                    "jrc_water_other_month": ~node["jrc_near"] & node["jrc_year"],
                    "other": ~node["jrc_near"] & ~node["jrc_year"],
                }
                layers = {
                    "optical": node["optical"],
                    "radar_only": radar_only,
                    "both_sensors": node["sar"] & node["optical"],
                }
                for layer, flag in layers.items():
                    add(f"{layer}_jrc_observed_km2", px.node_sum(flag & node["jrc_observed"], n))
                    for cause, where in causes.items():
                        add(f"{layer}_jrc_dry_{cause}_km2", px.node_sum(flag & dry & where, n))
            for name, conditions in rules.items():
                keep = px.kept(conditions, min_px)
                out = rule_sums[month][name]
                out["flagged"] += px.node_sum(keep, n)
                out["radar_only"] += px.node_sum(keep & radar_only, n)
                if jrc:
                    out["flagged_jrc_observed"] += px.node_sum(keep & node["jrc_observed"], n)
                    out["flagged_jrc_water"] += px.node_sum(keep & node["jrc_water"], n)
        if log:
            log(f"strip {number}/{len(all_strips)} done")
    rule_rows, county_rows = [], []
    for month in months:
        jrc = int(month[:4]) <= JRC_LAST_YEAR
        for name, sums in rule_sums[month].items():
            frame = pd.DataFrame({"county_code": codes, "month": month, "rule": name})
            for key, values in sums.items():
                frame[f"{key}_km2"] = values[1:] if jrc or not key.startswith("flagged_jrc") else np.nan
            rule_rows.append(frame)
        frame = pd.DataFrame({"county_code": codes, "month": month})
        for key, values in county_sums[month].items():
            frame[key] = values[1:]
        county_rows.append(frame)
    return pd.concat(rule_rows, ignore_index=True), pd.concat(county_rows, ignore_index=True)


def class_codes(
    cfg: Config,
    counties: gpd.GeoDataFrame,
    grid: Grid,
    area_by_row: np.ndarray,
    month: str,
    strip_rows: int = 1000,
) -> pd.DataFrame:
    """km2 of each pixel code per county, as radar_fixes.class_table builds it in Earth Engine.

    Used to check that the local route reproduces Earth Engine (no patch filter re-applied here, as
    in the Earth Engine table). Codes include the "excluded" bit.
    """
    bits = class_bits(cfg.radar_fix_test)
    span = 2 ** len(bits)  # every possible code
    codes = sorted(counties["county_code"])
    totals = np.zeros((len(codes) + 1) * span)
    for read0, read1, core0, core1 in layer_cache.strips(grid.height, strip_rows, 1):
        planes = load_planes(cfg, month, read0, read1)
        county_full, _ = county_index(counties, grid, read0, read1)
        core = slice(core0 - read0, core1 - read0)
        # Inside the stored layers' footprint, as the Earth Engine code image is.
        inside = (planes["assessed"] | planes["excluded"]) & (county_full > 0)
        code = np.zeros(county_full.shape, dtype=np.int64)
        for name, bit in bits.items():
            code += planes[name].astype(np.int64) * bit
        rows, cols = np.nonzero(inside[core])
        key = county_full[core][rows, cols].astype(np.int64) * span + code[core][rows, cols]
        totals += np.bincount(key, weights=area_by_row[core0:core1][rows], minlength=totals.size)
    nonzero = np.nonzero(totals)[0]
    return pd.DataFrame(
        {
            "county_code": [codes[i - 1] for i in nonzero // span],
            "code": nonzero % span,
            "km2": totals[nonzero],
            "month": month,
        }
    )
