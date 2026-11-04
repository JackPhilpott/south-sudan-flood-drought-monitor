"""County boundaries and the flood-frequency GeoTIFF."""

import geopandas as gpd
import numpy as np
import pytest
from pyproj import Geod
from shapely.geometry import Polygon, box

from ssd_monitor.boundaries import load_counties, make_counties, save_counties
from ssd_monitor.rasters import Grid, grid_for_bounds, read_frequency, write_frequency


def admin2(geometries, codes):
    """A layer shaped like the COD-AB admin 2 file, with extra columns that should be dropped."""
    return gpd.GeoDataFrame(
        {
            "adm2_pcode": codes,
            "adm2_name": [f"County {c}" for c in codes],
            "adm1_pcode": ["SS99"] * len(codes),
            "adm1_name": ["State"] * len(codes),
            "valid_on": ["2022-12-19"] * len(codes),
        },
        geometry=geometries,
        crs="EPSG:4326",
    )


def test_make_counties_renames_sorts_and_measures():
    counties = make_counties(admin2([box(30.5, 7, 31, 7.5), box(30, 7, 30.5, 7.5)], ["SS9902", "SS9901"]))
    assert list(counties.columns) == [
        "county_code",
        "county_name",
        "state_code",
        "state_name",
        "geometry",
        "area_km2",
    ]
    assert counties["county_code"].tolist() == ["SS9901", "SS9902"]
    geodesic_km2 = abs(Geod(ellps="WGS84").geometry_area_perimeter(box(30, 7, 30.5, 7.5))[0]) / 1e6
    assert counties.loc[0, "area_km2"] == pytest.approx(geodesic_km2, rel=0.005)


def test_simplification_keeps_shared_borders_shared():
    # Two counties sharing a jagged border: simplifying each on its own would open gaps and overlaps.
    border = [(30.5 + 0.004 * ((i % 3) - 1), 7 + i * 0.01) for i in range(51)]
    west = Polygon([(30, 7), *border, (30, 7.5)])
    east = Polygon([(31, 7), (31, 7.5), *reversed(border)])
    counties = make_counties(admin2([west, east], ["SS9901", "SS9902"]), tolerance=0.01)
    a, b = counties.geometry
    assert a.intersection(b).area == pytest.approx(0, abs=1e-12)  # no overlap
    assert a.union(b).area == pytest.approx(box(30, 7, 31, 7.5).area, rel=1e-9)  # no gap


def test_make_counties_needs_the_cod_columns():
    with pytest.raises(ValueError, match="adm1_name"):
        make_counties(admin2([box(30, 7, 30.5, 7.5)], ["SS9901"]).drop(columns="adm1_name"))


def test_save_and_load_round_trip(tmp_path, counties_gdf):
    path = tmp_path / "counties.geojson"
    save_counties(counties_gdf, path)
    loaded = load_counties(path)
    assert loaded["county_code"].tolist() == sorted(counties_gdf["county_code"])
    assert loaded["area_km2"].sum() == pytest.approx(counties_gdf["area_km2"].sum())


# --- rasters -----------------------------------------------------------------------------


def test_grid_snaps_outwards_to_whole_cells():
    grid = grid_for_bounds((23.44, 3.49, 35.95, 12.24), 0.005)
    assert (grid.west, grid.north) == (23.44, 12.24)
    assert (grid.width, grid.height) == (2502, 1750)
    grid = grid_for_bounds((30.01, 7.01, 30.99, 7.99), 0.5)
    assert grid == Grid(west=30.0, north=8.0, cell=0.5, width=2, height=2)
    assert grid.extent == (30.0, 31.0, 7.0, 8.0)
    assert grid.crs_transform == [0.5, 0, 30.0, 0, -0.5, 8.0]


def test_frequency_round_trip(tmp_path):
    grid = Grid(west=30.0, north=8.0, cell=0.5, width=3, height=2)
    months_x10 = np.array([[0, 125, -1], [840, 5, 0]])
    excluded_pct = np.array([[0, 100, -1], [0, 50, 0]])
    path = tmp_path / "frequency.tif"
    write_frequency(path, months_x10, excluded_pct, grid)
    raster = read_frequency(path)
    assert raster.grid == grid
    assert raster.months[0, 1] == 12.5 and raster.months[1, 0] == 84.0
    assert np.isnan(raster.months[0, 2]) and np.isnan(raster.excluded[0, 2])
    assert raster.excluded[1, 1] == 0.5


def test_frequency_rejects_the_wrong_shape(tmp_path):
    grid = Grid(west=30.0, north=8.0, cell=0.5, width=3, height=2)
    with pytest.raises(ValueError, match="2 x 3"):
        write_frequency(tmp_path / "f.tif", np.zeros((3, 2)), np.zeros((3, 2)), grid)
