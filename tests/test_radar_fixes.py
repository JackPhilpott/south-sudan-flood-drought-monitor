"""Tests for the radar-fix test: month windows, pixel classes, the local cache and the local scoring.

Everything here is synthetic: invented grids and pixels, no Earth Engine.
"""

import dataclasses
import json

import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.io import MemoryFile

from ssd_monitor import fix_scores, layer_cache, local_fixes, pixel_classes
from ssd_monitor.config import validate
from ssd_monitor.dates import add_months, burn_window, season_burn_window
from ssd_monitor.rasters import Grid

# --- Month windows and settings -----------------------------------------------------------------


def test_add_months_crosses_years():
    assert add_months("2020-01", -2) == "2019-11"
    assert add_months("2019-12", 1) == "2020-01"


def test_burn_windows():
    assert burn_window("2020-07", 2) == ["2020-05", "2020-06", "2020-07"]
    # The burn-season mask counts every burn since 1 November...
    assert season_burn_window("2020-03", 11, in_season=True) == [
        "2019-11",
        "2019-12",
        "2020-01",
        "2020-02",
        "2020-03",
    ]
    assert season_burn_window("2019-11", 11, in_season=True) == ["2019-11"]
    # ...and nothing from June to October, so a dry-season burn can't remove water in a flood.
    assert season_burn_window("2020-07", 11, in_season=False) == []


def test_burn_season_months(cfg):
    rt = cfg.radar_fix_test
    assert [m for m in range(1, 13) if rt.in_burn_season(m)] == [1, 2, 3, 4, 5, 11, 12]


def test_test_years_must_end_by_2021(cfg):
    late = dataclasses.replace(cfg.radar_fix_test, years=(2020, 2022))
    with pytest.raises(ValueError, match="2021"):
        validate(dataclasses.replace(cfg, radar_fix_test=late))


# --- Pixel classes and rules ----------------------------------------------------------------------


def test_class_bits_are_distinct_powers_of_two(cfg):
    bits = pixel_classes.class_bits(cfg.radar_fix_test)
    assert len(set(bits.values())) == len(bits) == 15
    assert all(b & (b - 1) == 0 for b in bits.values())
    assert max(bits.values()) < 2**15  # fits Earth Engine's int16 code


def test_candidate_rules_cover_every_combination(cfg):
    rules = pixel_classes.candidate_rules(cfg.radar_fix_test)
    assert len(rules) == 3 * 4 * 2 * 2
    assert rules["current"] == ()
    assert rules["a2+b15+c+e"] == ("burn_season", "hand_over_15", "dark_reference", "scene_edge")


def test_classes_from_features_and_decode(cfg):
    bits = pixel_classes.class_bits(cfg.radar_fix_test)
    groups = [{"code": bits["sar"], "sum": 2.5}, {"code": bits["sar"] | bits["excluded"], "sum": 9.0}]
    features = [{"properties": {"county_code": "SS9901", "month": "2020-03", "groups": json.dumps(groups)}}]
    classes = pixel_classes.classes_from_features(features)
    assert classes["km2"].sum() == 11.5
    decoded = pixel_classes.decode(classes, bits)
    assert len(decoded) == 1 and decoded["sar"].all() and not decoded["optical"].any()


# --- Local cache ----------------------------------------------------------------------------------

GRID = Grid(west=30.0, north=8.5, cell=0.025, width=60, height=40)  # covers the four test counties


def test_tiles_cover_the_grid_once():
    covered = np.zeros((GRID.height, GRID.width), dtype=int)
    for row, col, height, width in layer_cache.tiles(GRID, 16):
        covered[row : row + height, col : col + width] += 1
    assert (covered == 1).all()


def test_strips_keep_every_row_once():
    strips = layer_cache.strips(40, 15, 3)
    assert [(c0, c1) for _, _, c0, c1 in strips] == [(0, 15), (15, 30), (30, 40)]
    assert strips[1][:2] == (12, 33)


def _geotiff_bytes(grid: Grid, bands: dict[str, np.ndarray], dtype: str) -> bytes:
    """A GeoTIFF like the ones Earth Engine returns for a tile."""
    with MemoryFile() as mem:
        profile = {
            "driver": "GTiff",
            "width": grid.width,
            "height": grid.height,
            "count": len(bands),
            "dtype": dtype,
            "crs": "EPSG:4326",
            "transform": layer_cache.affine(grid),
            "compress": "deflate",
        }
        with mem.open(**profile) as dst:
            for i, values in enumerate(bands.values(), start=1):
                dst.write(values.astype(dtype), i)
        return mem.read()


def write_layer(cache_dir, name: str, bands: dict[str, np.ndarray], dtype="uint8", nodata=255, tile_px=16):
    """Save a whole synthetic layer into the cache, tile by tile."""
    spec = layer_cache.CacheFile(name, tuple(bands), dtype, nodata)
    folder = layer_cache.layer_dir(cache_dir, name)
    layer_cache.create(folder, GRID, spec, tile_px)
    for row, col, height, width in layer_cache.tiles(GRID, tile_px):
        part = {k: v[row : row + height, col : col + width] for k, v in bands.items()}
        tile = layer_cache.tile_grid(GRID, row, col, height, width)
        layer_cache.save_tile(folder, row, col, _geotiff_bytes(tile, part, dtype))
    return folder


def test_cache_roundtrip(tmp_path):
    values = np.arange(GRID.height * GRID.width, dtype="uint16").reshape(GRID.height, GRID.width)
    folder = write_layer(tmp_path, "test", {"a": values, "b": values[::-1]}, dtype="uint16", nodata=65535)
    assert layer_cache.is_complete(folder)
    rows = layer_cache.read_rows(folder, ["a", "b"], 5, 23)
    assert np.array_equal(rows["a"], values[5:23]) and np.array_equal(rows["b"], values[::-1][5:23])
    with rasterio.open(layer_cache.tile_path(folder, 16, 32)) as src:
        assert src.width == 16 and src.height == 16
    other = layer_cache.CacheFile("test", ("a",), "uint16", 65535)
    with pytest.raises(ValueError, match="different layer"):
        layer_cache.create(folder, GRID, other, 16)


# --- Patch filter and rules -------------------------------------------------------------------------


def test_patch_filter_is_eight_connected():
    mask = np.zeros((12, 12), dtype=bool)
    for i in range(8):  # a diagonal chain of 8 pixels: one patch when diagonals connect
        mask[i, i] = True
    mask[0, 10] = mask[1, 10] = True  # a patch of 2
    kept = local_fixes.patch_filter(mask, 8)
    assert kept[:8, :8].sum() == 8 and not kept[:, 10].any()


def test_patch_filter_in_strips_matches_the_whole_image():
    rng = np.random.default_rng(1)
    mask = rng.random((90, 50)) < 0.18
    whole = local_fixes.patch_filter(mask, 8)
    pieces = np.zeros_like(mask)
    for read0, read1, core0, core1 in layer_cache.strips(90, 20, 8):
        part = local_fixes.patch_filter(mask[read0:read1], 8)
        pieces[core0:core1] = part[core0 - read0 : core1 - read0]
    assert np.array_equal(whole, pieces)


def test_kept_only_removes_radar_only_pixels():
    shape = (10, 10)
    planes = {k: np.zeros(shape, dtype=bool) for k in ("sar", "optical", "burn_recent")}
    planes["sar"][2, 0:9] = True  # 9 radar pixels in a row...
    planes["optical"][2, 0:2] = True  # ...two of them also optical
    planes["burn_recent"][2, 0:5] = True  # burned under the first five
    assert local_fixes.kept(planes, (), 8).sum() == 9
    # The burn mask removes radar-only pixels 2-4; what is left (0-1 and 5-8) are patches of 2 and 4.
    assert local_fixes.kept(planes, ("burn_recent",), 8).sum() == 0
    assert local_fixes.kept(planes, ("burn_recent",), 2).sum() == 6


def test_graph_patches_match_full_array_labelling():
    rng = np.random.default_rng(7)
    for density in (0.05, 0.2, 0.35):
        planes = {"sar": rng.random((60, 70)) < density, "optical": rng.random((60, 70)) < density / 3}
        planes["burn_recent"] = rng.random((60, 70)) < 0.5
        union = planes["sar"] | planes["optical"]
        rows, cols, starts, ends = local_fixes.flagged_graph(union)
        for conditions in ((), ("burn_recent",)):
            drop = planes["burn_recent"][rows, cols] if conditions else np.zeros(len(rows), dtype=bool)
            radar_only = planes["sar"][rows, cols] & ~planes["optical"][rows, cols]
            keep = local_fixes.big_patches(~(drop & radar_only), starts, ends, 8)
            from_graph = np.zeros_like(union)
            from_graph[rows[keep], cols[keep]] = True
            assert np.array_equal(from_graph, local_fixes.kept(planes, conditions, 8))


# --- End to end on a synthetic cache ------------------------------------------------------------------


@pytest.fixture
def synthetic_cache(tmp_path, cfg):
    """A made-up cache for March 2020: in county SS9901 (Alpha, the north-west box), a 4 x 4 radar-only
    patch on burnt land that JRC calls dry, and a 4 x 4 patch both sensors flag that JRC calls water."""
    shape = (GRID.height, GRID.width)
    months = [f"2020-{m:02d}" for m in range(1, 13)]
    packed = np.full(shape, 8, dtype="uint8")  # observed everywhere, nothing flagged
    packed[2:6, 2:6] = 8 | 2 | 1  # radar only
    packed[10:14, 2:6] = 8 | 2 | 4 | 1  # both sensors
    both = np.full(shape, 8, dtype="uint8")
    both[10:14, 2:6] = 8 | 2 | 4 | 1  # April and May: only the patch both sensors see
    layers = {f"m{m.replace('-', '_')}": np.full(shape, 8, "uint8") for m in months}
    layers |= {"m2020_03": packed, "m2020_04": both, "m2020_05": both}
    layers |= {"months_flooded": np.zeros(shape, "uint8"), "excluded": np.zeros(shape, "uint8")}
    write_layer(tmp_path, "flood_layers_2020", layers)
    jrc = np.ones(shape, "uint8")  # observed, not water
    jrc[10:14, 2:6] = 2
    write_layer(tmp_path, "jrc_monthly_2020", {f"j{m.replace('-', '_')}": jrc for m in months})
    write_layer(tmp_path, "jrc_yearly", {"y2020": np.ones(shape, "uint8")})
    burned = np.zeros(shape, "uint8")
    burned[0:8, 0:8] = 1
    for year in (2019, 2020):
        bands = {
            f"b{year}_{m:02d}": burned if (year, m) == (2020, 2) else np.zeros(shape, "uint8")
            for m in range(1, 13)
        }
        write_layer(tmp_path, f"burned_{year}", bands)
    write_layer(
        tmp_path, "scene_edges_2020", {f"e{m.replace('-', '_')}": np.zeros(shape, "uint8") for m in months}
    )
    write_layer(tmp_path, "hand_cm", {"hand_cm": np.full(shape, 100, "uint16")}, dtype="uint16", nodata=65535)
    write_layer(tmp_path, "dark_reference", {"dark": np.zeros(shape, "uint8")})
    return dataclasses.replace(cfg, cache=tmp_path)


def test_month_tables_and_scores(synthetic_cache, counties_gdf):
    cfg = synthetic_cache
    area = np.full(GRID.height, 2.0)  # 2 km2 per pixel, to keep the arithmetic simple
    rules = {"current": (), "a1": ("burn_recent",), "c": ("dark_reference",)}
    rule_table, county_table = local_fixes.rule_tables(
        cfg, counties_gdf, GRID, area, ["2020-03"], rules, strip_rows=13
    )
    alpha = rule_table[rule_table["county_code"] == "SS9901"].set_index("rule")
    assert alpha.loc["current", "flagged_km2"] == 64  # 32 pixels
    assert alpha.loc["a1", "flagged_km2"] == 32  # the burnt radar-only patch is gone (burned in February)
    assert alpha.loc["c", "flagged_km2"] == 64  # nothing is dark in the reference
    assert alpha.loc["current", "radar_only_km2"] == 32 and alpha.loc["a1", "radar_only_km2"] == 0
    c = county_table.set_index("county_code").loc["SS9901"]
    assert c["jrc_water_km2"] == 32 and c["radar_only_jrc_dry_other_km2"] == 32

    scores = fix_scores.score_rules(rule_table, county_table, cfg.radar_fix_test.wet_season_months)
    dry = scores[scores["season"] == "dry"].set_index("rule")
    assert dry.loc["current", "recall"] == 1 and dry.loc["current", "precision"] == 0.5
    assert dry.loc["a1", "precision"] == 1 and dry.loc["a1", "removed_km2"] == 32
    assert (
        scores[scores["season"] == "wet"].empty
        or scores[scores["season"] == "wet"]["jrc_water_km2"].sum() == 0
    )

    fw = fix_scores.false_water(rule_table, county_table, ["SS9901"], ["2020-03"]).set_index("rule")
    assert fw.loc["current", "jrc_dry_flagged_km2"] == 32 and fw.loc["a1", "jrc_dry_flagged_km2"] == 0

    local_codes = local_fixes.class_codes(cfg, counties_gdf, GRID, area, "2020-03", strip_rows=13)
    bits = pixel_classes.class_bits(cfg.radar_fix_test)
    same = fix_scores.compare_classes(local_codes, local_codes, bits)
    assert (same["earth_engine_km2"] == same["local_km2"]).all()


def test_compare_stored_sums_reports_differences():
    counties = pd.DataFrame(
        {
            "county_code": ["A", "B"],
            "month": "2020-03",
            "stored_flooded_km2": [10.0, 5.0],
            "stored_sar_km2": [8.0, 4.0],
            "stored_optical_km2": [3.0, 2.0],
            "stored_observed_km2": [100.0, 50.0],
        }
    )
    raw = pd.DataFrame(
        {
            "county_code": ["A", "B"],
            "month": "2020-03",
            "flooded_km2": [10.0, 4.0],
            "flooded_sar_km2": [8.0, 4.0],
            "flooded_optical_km2": [3.0, 2.0],
            "observed_km2": [100.0, 50.0],
        }
    )
    out = fix_scores.compare_stored_sums(counties, raw, "2020-03").set_index("quantity")
    assert out.loc["flooded_km2", "largest_county_difference_km2"] == 1
    assert out.loc["observed_km2", "difference_pct"] == 0


def test_rebuild_splits_persistent_and_new_water(synthetic_cache, counties_gdf, tmp_path):
    from ssd_monitor import rasters, rebuild

    cfg = synthetic_cache
    area = np.full(GRID.height, 2.0)
    pixels = tmp_path / "frequency_100m.tif"
    flood, jrc = rebuild.rebuild(
        cfg, counties_gdf, GRID, area, ["2020-03", "2020-05", "2020-06"], pixels, strip_rows=13
    )
    alpha = flood[flood["county_code"] == "SS9901"].set_index("month")
    # March: the radar-only patch sits on land burned in February, so the fix removes it. The patch
    # both sensors see is in both March and April, so it is persistent from May on (and in March,
    # since 2020 is the first year here and uses its own dry season).
    assert alpha.loc["2020-03", "flooded_km2"] == 32 and alpha.loc["2020-03", "persistent_km2"] == 32
    assert alpha.loc["2020-05", "persistent_km2"] == 32 and alpha.loc["2020-05", "new_km2"] == 0
    assert alpha.loc["2020-06", "flooded_km2"] == 0
    assert (flood["persistent_km2"] + flood["new_km2"] == flood["flooded_km2"]).all()
    march = jrc[(jrc["county_code"] == "SS9901") & (jrc["month"] == "2020-03")].iloc[0]
    assert march["flagged_jrc_water_km2"] == march["jrc_water_km2"] == march["flagged_jrc_observed_km2"] == 32
    with rasterio.open(pixels) as src:
        water = src.read(1)
    assert water[10:14, 2:6].min() == 2 and water[2:6, 2:6].max() == 0  # March and May
    cells = rasters.grid_for_bounds(counties_gdf.total_bounds, 0.05)
    months_x10, excluded_pct = rebuild.frequency_cells(pixels, cells, "months_water")
    assert months_x10.max() > 0 and (excluded_pct[months_x10 >= 0] == 0).all()


def test_baseline_year():
    from ssd_monitor.rebuild import baseline_year

    assert baseline_year("2021-05", 2019) == 2021 and baseline_year("2021-04", 2019) == 2020
    assert baseline_year("2019-02", 2019) == 2019
