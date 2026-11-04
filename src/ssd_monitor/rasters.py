"""The flood-frequency GeoTIFF: its grid, and writing and reading it. No Earth Engine here.

The file has two int16 bands on a regular longitude/latitude grid:
    1  months_flooded_x10   mean number of flooded months in the cell, times 10
    2  excluded_pct         share of the cell that is permanent water or steep slope, in %
Cells outside South Sudan are -1 (nodata).
"""

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

NODATA = -1


@dataclass(frozen=True)
class Grid:
    west: float
    north: float
    cell: float
    width: int
    height: int

    @property
    def extent(self) -> tuple[float, float, float, float]:
        """(west, east, south, north), the order matplotlib's imshow expects."""
        return (
            self.west,
            self.west + self.width * self.cell,
            self.north - self.height * self.cell,
            self.north,
        )

    @property
    def crs_transform(self) -> list[float]:
        """Earth Engine's affine transform for the grid."""
        return [self.cell, 0, self.west, 0, -self.cell, self.north]


@dataclass(frozen=True)
class FrequencyRaster:
    months: np.ndarray  # mean months flooded, NaN outside the country
    excluded: np.ndarray  # share 0-1 of each cell excluded, NaN outside the country
    grid: Grid


def grid_for_bounds(bounds, cell: float) -> Grid:
    """The smallest grid of whole cells, aligned to multiples of `cell`, covering (minx, miny, maxx, maxy)."""
    minx, miny, maxx, maxy = bounds
    # Round before floor/ceil so float noise (e.g. 4.999999999) doesn't add a column.
    west = math.floor(round(minx / cell, 9)) * cell
    south = math.floor(round(miny / cell, 9)) * cell
    east = math.ceil(round(maxx / cell, 9)) * cell
    north = math.ceil(round(maxy / cell, 9)) * cell
    return Grid(
        west=round(west, 9),
        north=round(north, 9),
        cell=cell,
        width=round((east - west) / cell),
        height=round((north - south) / cell),
    )


def write_frequency(path: Path, months_x10: np.ndarray, excluded_pct: np.ndarray, grid: Grid) -> None:
    """Write the two bands as a compressed GeoTIFF. Values below 0 are treated as outside the country."""
    if months_x10.shape != (grid.height, grid.width) or excluded_pct.shape != months_x10.shape:
        raise ValueError(f"arrays must be {grid.height} x {grid.width}")
    bands = [np.where(a < 0, NODATA, a).astype("int16") for a in (months_x10, excluded_pct)]
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "width": grid.width,
        "height": grid.height,
        "count": 2,
        "dtype": "int16",
        "crs": "EPSG:4326",
        "transform": from_origin(grid.west, grid.north, grid.cell, grid.cell),
        "nodata": NODATA,
        "compress": "deflate",
        "predictor": 2,
    }
    with rasterio.open(path, "w", **profile) as dst:
        for i, band in enumerate(bands, start=1):
            dst.write(band, i)
        dst.set_band_description(1, "months_flooded_x10")
        dst.set_band_description(2, "excluded_pct")


def read_frequency(path: Path) -> FrequencyRaster:
    with rasterio.open(path) as src:
        months_x10, excluded_pct = (src.read(i).astype(float) for i in (1, 2))
        t = src.transform
        grid = Grid(west=t.c, north=t.f, cell=t.a, width=src.width, height=src.height)
    outside = months_x10 == NODATA
    months = np.where(outside, np.nan, months_x10 / 10)
    excluded = np.where(outside, np.nan, excluded_pct / 100)
    return FrequencyRaster(months=months, excluded=excluded, grid=grid)
