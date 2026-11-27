"""Local cache of 100 m layers downloaded from Earth Engine, all on the stored flood layers' grid.

Reading stored pixels from Earth Engine is cheap; computing on them there is not (the method update's
cost test measured 2-4 times the earlier county tables). So the stored yearly flood layers and the
extra layers the fixes need are downloaded once, and the rest is done locally with numpy. Every
layer uses the grid of the stored layers (0.000898 degrees, about 100 m), so pixels line up across
layers without resampling.

Each layer is a folder of tiles: one compressed GeoTIFF per tile, saved exactly as Earth Engine
returns it, plus layer.json with the band names, data type, nodata value, grid and tile size. A tile
file exists only once it is complete (written to a temporary name, then renamed), so an interrupted
download picks up where it stopped. No Earth Engine here: exports.py fetches the tiles.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.windows import Window

from ssd_monitor.rasters import Grid


@dataclass(frozen=True)
class CacheFile:
    name: str  # folder name in the cache
    bands: tuple[str, ...]
    dtype: str  # numpy dtype name, e.g. "uint8"
    nodata: int  # value of pixels Earth Engine had masked


def grid_from_asset(info: dict) -> Grid:
    """The grid of an Earth Engine image asset, from its metadata (ee.data.getAsset)."""
    band = info["bands"][0]["grid"]
    transform = band["affineTransform"]
    if transform["scaleX"] != -transform["scaleY"] or transform.get("shearX") or transform.get("shearY"):
        raise ValueError("expected square, unrotated pixels")
    return Grid(
        west=transform["translateX"],
        north=transform["translateY"],
        cell=transform["scaleX"],
        width=band["dimensions"]["width"],
        height=band["dimensions"]["height"],
    )


def affine(grid: Grid) -> Affine:
    return Affine(grid.cell, 0, grid.west, 0, -grid.cell, grid.north)


def tiles(grid: Grid, size: int) -> list[tuple[int, int, int, int]]:
    """(first row, first column, height, width) of tiles of at most size x size covering the grid."""
    return [
        (row, col, min(size, grid.height - row), min(size, grid.width - col))
        for row in range(0, grid.height, size)
        for col in range(0, grid.width, size)
    ]


def tile_grid(grid: Grid, row: int, col: int, height: int, width: int) -> Grid:
    """The sub-grid of one tile, with its origin computed exactly (no rounding, unlike rasters.split_grid)."""
    return Grid(
        west=grid.west + col * grid.cell,
        north=grid.north - row * grid.cell,
        cell=grid.cell,
        width=width,
        height=height,
    )


def strips(height: int, rows: int, halo: int) -> list[tuple[int, int, int, int]]:
    """(read start, read end, core start, core end) of row strips for neighbourhood work.

    Each strip is processed with `halo` extra rows above and below it, then only its core rows are
    kept. For the small-patch filter, a halo of min_connected_pixels rows is exact: a patch smaller
    than that spans fewer rows, and a patch that reaches the edge of the halo is at least that big.
    """
    out = []
    for core_start in range(0, height, rows):
        core_end = min(core_start + rows, height)
        out.append((max(core_start - halo, 0), min(core_end + halo, height), core_start, core_end))
    return out


def layer_dir(cache_dir: Path, name: str) -> Path:
    return cache_dir / name


def create(folder: Path, grid: Grid, spec: CacheFile, tile_px: int) -> None:
    """The layer's folder and layer.json, unless they exist (a different spec or grid is an error)."""
    meta = {"spec": asdict(spec) | {"bands": list(spec.bands)}, "grid": asdict(grid), "tile_px": tile_px}
    path = folder / "layer.json"
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != meta:
            raise ValueError(f"{folder} holds a different layer; delete it to download again")
        return
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=1), encoding="utf-8")


def read_meta(folder: Path) -> tuple[CacheFile, Grid, int]:
    meta = json.loads((folder / "layer.json").read_text(encoding="utf-8"))
    spec = meta["spec"]
    return (
        CacheFile(spec["name"], tuple(spec["bands"]), spec["dtype"], spec["nodata"]),
        Grid(**meta["grid"]),
        meta["tile_px"],
    )


def tile_path(folder: Path, row: int, col: int) -> Path:
    return folder / f"{row:05d}_{col:05d}.tif"


def missing_tiles(folder: Path, grid: Grid, tile_px: int) -> list[tuple[int, int, int, int]]:
    return [t for t in tiles(grid, tile_px) if not tile_path(folder, t[0], t[1]).exists()]


def save_tile(folder: Path, row: int, col: int, data: bytes) -> None:
    """Save one tile's GeoTIFF bytes; the file appears under its final name only when complete."""
    final = tile_path(folder, row, col)
    temporary = final.with_suffix(".part")
    temporary.write_bytes(data)
    os.replace(temporary, final)


def is_complete(folder: Path) -> bool:
    if not (folder / "layer.json").exists():
        return False
    _, grid, tile_px = read_meta(folder)
    return not missing_tiles(folder, grid, tile_px)


def read_rows(folder: Path, bands: list[str], first_row: int, last_row: int) -> dict[str, np.ndarray]:
    """The named bands for rows first_row to last_row (exclusive), full width, from the layer's tiles."""
    spec, grid, tile_px = read_meta(folder)
    index = {name: i for i, name in enumerate(spec.bands, start=1)}
    out = {band: np.full((last_row - first_row, grid.width), spec.nodata, dtype=spec.dtype) for band in bands}
    for row, col, height, width in tiles(grid, tile_px):
        top, bottom = max(row, first_row), min(row + height, last_row)
        if top >= bottom:
            continue
        with rasterio.open(tile_path(folder, row, col)) as src:
            window = Window(0, top - row, width, bottom - top)
            for band in bands:
                out[band][top - first_row : bottom - first_row, col : col + width] = src.read(
                    index[band], window=window
                )
    return out
