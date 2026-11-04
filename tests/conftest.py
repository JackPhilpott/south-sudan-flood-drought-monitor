"""Synthetic fixtures: four made-up counties and invented indicator values. No real data, no Earth Engine."""

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from ssd_monitor.boundaries import county_areas_km2, county_table
from ssd_monitor.config import load_config
from ssd_monitor.dates import months_between, months_in_years
from ssd_monitor.rasters import FrequencyRaster, grid_for_bounds

# Typical monthly rainfall shape (mm), January to December: dry Dec-Mar, peak in August.
NORMAL_RAIN = [0, 2, 10, 40, 90, 130, 170, 190, 150, 90, 20, 3]
# Typical monthly NDVI shape: lowest in the dry season.
NORMAL_NDVI = [0.30, 0.28, 0.27, 0.30, 0.38, 0.48, 0.58, 0.64, 0.66, 0.60, 0.48, 0.36]


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def counties_gdf():
    """Four invented counties in two invented states; Delta is twice the size of the others."""
    gdf = gpd.GeoDataFrame(
        {
            "county_code": ["SS9801", "SS9802", "SS9901", "SS9902"],
            "county_name": ["Charlie", "Delta", "Alpha", "Bravo"],
            "state_code": ["SS98", "SS98", "SS99", "SS99"],
            "state_name": ["South", "South", "North", "North"],
        },
        geometry=[
            box(30, 7.5, 30.5, 8),
            box(30.5, 7.5, 31.5, 8),
            box(30, 8, 30.5, 8.5),
            box(30.5, 8, 31, 8.5),
        ],
        crs="EPSG:4326",
    )
    gdf["area_km2"] = county_areas_km2(gdf)
    return gdf


@pytest.fixture(scope="session")
def counties(counties_gdf):
    return county_table(counties_gdf)


def make_raw(counties: pd.DataFrame, cfg) -> dict[str, pd.DataFrame]:
    """Invented raw tables shaped like the Earth Engine downloads, with a few known events:

    - Alpha (SS9901), June 2021: half its normal rainfall.
    - January 2022: a drought with NDVI 0.08 below usual everywhere, 0.15 in Charlie (SS9801).
    - Bravo (SS9902), October 2020: 40% of the county flooded.
    - Alpha, July 2019: no satellite saw the county (observed area 0).
    - Delta (SS9802), March 2023: no flood row at all.
    """
    codes = counties["county_code"].tolist()
    area = counties.set_index("county_code")["area_km2"]
    rng = np.random.default_rng(0)

    rain_rows = []
    for month in months_in_years(1991, 2025):
        year, m = int(month[:4]), int(month[5:]) - 1
        for code in codes:
            value = NORMAL_RAIN[m] * (1 + 0.05 * ((year % 5) - 2))
            if code == "SS9901" and month == "2021-06":
                value = NORMAL_RAIN[m] * 0.5
            rain_rows.append({"county_code": code, "month": month, "rain_mm": value})

    ndvi_rows = []
    for month in months_in_years(2001, 2025):
        m = int(month[5:]) - 1
        for code in codes:
            value = NORMAL_NDVI[m] + rng.normal(0, 0.01)
            if month == "2022-01":
                value -= 0.15 if code == "SS9801" else 0.08
            ndvi_rows.append({"county_code": code, "month": month, "ndvi": value})

    flood_rows = []
    for month in months_between(cfg.start_month, cfg.end_month):
        m = int(month[5:]) - 1
        for code in codes:
            if code == "SS9802" and month == "2023-03":
                continue
            share = 0.02 * NORMAL_RAIN[m] / 190
            if code == "SS9902" and month == "2020-10":
                share = 0.40
            observed = 0.0 if (code == "SS9901" and month == "2019-07") else 0.9 * area[code]
            flooded = share * area[code] if observed else 0.0
            flood_rows.append(
                {
                    "county_code": code,
                    "month": month,
                    "flooded_km2": flooded,
                    "flooded_sar_km2": 0.8 * flooded,
                    "flooded_optical_km2": 0.5 * flooded,
                    "observed_km2": observed,
                }
            )

    return {
        "rainfall": pd.DataFrame(rain_rows),
        "ndvi": pd.DataFrame(ndvi_rows),
        "flood": pd.DataFrame(flood_rows),
    }


@pytest.fixture(scope="session")
def raw(counties, cfg):
    return make_raw(counties, cfg)


def make_frequency(counties_gdf: gpd.GeoDataFrame) -> FrequencyRaster:
    """An invented flood-frequency raster over the four counties, with a strip of permanent water."""
    grid = grid_for_bounds(counties_gdf.total_bounds, 0.05)
    rows, cols = np.mgrid[0 : grid.height, 0 : grid.width]
    months = np.clip(30 - np.hypot(rows - grid.height / 2, cols - grid.width / 3) * 2.5, 0, None)
    excluded = np.zeros_like(months)
    excluded[:, 2] = 1.0
    return FrequencyRaster(months=months, excluded=excluded, grid=grid)


@pytest.fixture(scope="session")
def frequency(counties_gdf):
    return make_frequency(counties_gdf)
