"""Earth Engine input and output: start the exports, report their state, download the results.

This is the only module that sends requests to Earth Engine. The exports run as batch tasks
(no time limit, queued by Earth Engine) and write to assets in projects/$EE_PROJECT/assets/<folder>/.
Downloading reads those assets, which is quick.
"""

from pathlib import Path

import ee
import geopandas as gpd
import numpy as np
import pandas as pd

from ssd_monitor import flood, layer_cache, rainfall, rasters, vegetation
from ssd_monitor.config import Config
from ssd_monitor.earthengine import counties_to_ee, country_to_ee, search_area_to_ee
from ssd_monitor.stats import RAW_COLUMNS, RAW_ROUNDING, tidy_features
from ssd_monitor.tasks import TABLE_KINDS, Job, Plan, job_months, next_steps, plan_jobs

DESCRIPTION_PREFIX = "ssd_monitor_"
ACTIVE_STATES = {"PENDING", "RUNNING", "CANCELLING"}
MAX_PIXELS = 1e11
PAGE_SIZE = 5000  # features per request when reading a table
FREQUENCY_TILE_CELLS = 250  # cells per side of each flood-frequency download tile


def initialise(project: str) -> None:
    ee.Initialize(project=project)


def ensure_folder(root: str) -> None:
    try:
        ee.data.getAsset(root)
    except ee.EEException:
        ee.data.createAsset({"type": "FOLDER"}, root)


def existing_assets(root: str) -> set[str]:
    """Names (not full IDs) of the assets already in the export folder; empty if there is no folder yet."""
    try:
        listing = ee.data.listAssets({"parent": root})
    except ee.EEException:
        return set()
    return {a["name"].rsplit("/", 1)[-1] for a in listing.get("assets", [])}


def _created(op: dict) -> str:
    return op.get("metadata", {}).get("createTime", "")


def operations() -> dict[str, dict]:
    """The latest Earth Engine operation for each of this project's export names."""
    latest: dict[str, dict] = {}
    for op in ee.data.listOperations():
        description = op.get("metadata", {}).get("description", "")
        if description.startswith(DESCRIPTION_PREFIX):
            name = description.removeprefix(DESCRIPTION_PREFIX)
            if name not in latest or _created(op) > _created(latest[name]):
                latest[name] = op
    return latest


def eecu_seconds(op: dict) -> float | None:
    """Earth Engine compute used by a finished export, in EECU-seconds (None until it is reported)."""
    value = op.get("metadata", {}).get("batchEecuUsageSeconds")
    return None if value is None else float(value)


def current_plan(cfg: Config, root: str, jobs: list[Job] | None = None) -> tuple[Plan, dict[str, dict]]:
    """Where each job stands (the full run unless `jobs` is given), and the latest operation per job."""
    ops = operations()
    active = {name for name, op in ops.items() if op["metadata"].get("state") in ACTIVE_STATES}
    return next_steps(jobs or plan_jobs(cfg), existing_assets(root), active), ops


def _start(
    job: Job,
    cfg: Config,
    root: str,
    counties: ee.FeatureCollection,
    country: ee.Geometry,
    search: ee.Geometry,
) -> None:
    """Start one export. `country` is the detailed outline (for clipping), `search` the simple one."""
    asset_id = f"{root}/{job.name}"
    description = DESCRIPTION_PREFIX + job.name
    if job.kind == "flood_layers":
        task = ee.batch.Export.image.toAsset(
            image=flood.flood_layers(cfg.flood, search, job_months(job, cfg)).clip(country),
            description=description,
            assetId=asset_id,
            region=country.bounds(),
            scale=cfg.flood.scale_m,
            crs="EPSG:4326",
            maxPixels=MAX_PIXELS,
            # Packed flags must not be averaged in the pyramid; every band is read at full resolution.
            pyramidingPolicy={".default": "sample"},
        )
    else:
        months = job_months(job, cfg)
        if job.kind == "rainfall":
            table = rainfall.rainfall_table(cfg.rainfall, months, counties)
        elif job.kind == "ndvi":
            table = vegetation.ndvi_table(cfg.vegetation, months, counties)
        elif job.kind == "flood_table":
            layers = ee.Image(f"{root}/{job.needs[0]}")
            table = flood.table_from_layers(layers, months, counties, cfg.flood.scale_m)
        else:
            raise ValueError(f"unknown job kind {job.kind!r}")
        task = ee.batch.Export.table.toAsset(collection=table, description=description, assetId=asset_id)
    task.start()


def submit(cfg: Config, root: str, counties_gdf: gpd.GeoDataFrame, jobs: list[Job] | None = None) -> Plan:
    """Start every export that is ready and not done or running. Safe to run again."""
    ensure_folder(root)
    plan, _ = current_plan(cfg, root, jobs)
    counties, country, search = (
        counties_to_ee(counties_gdf),
        country_to_ee(counties_gdf),
        search_area_to_ee(counties_gdf),
    )
    for job in plan.start:
        _start(job, cfg, root, counties, country, search)
    return plan


def delete_assets(root: str, names: list[str]) -> list[str]:
    """Delete the named assets from the export folder (used to clear the cost-test outputs)."""
    existing = existing_assets(root)
    deleted = [name for name in names if name in existing]
    for name in deleted:
        ee.data.deleteAsset(f"{root}/{name}")
    return deleted


def read_table(asset_id: str, columns: list[str]) -> list[dict]:
    """Every feature of a table asset, without geometry, a page at a time."""
    table = ee.FeatureCollection(asset_id).select(
        propertySelectors=["county_code", "month", *columns], retainGeometry=False
    )
    size = table.size().getInfo()
    features: list[dict] = []
    for offset in range(0, size, PAGE_SIZE):
        features += table.toList(PAGE_SIZE, offset).getInfo()
    return features


def download_tables(cfg: Config, root: str, raw_dir: Path) -> dict[str, Path | None]:
    """Write outputs/raw/<indicator>.csv for each indicator whose exports have all finished."""
    done = existing_assets(root)
    written: dict[str, Path | None] = {}
    for kind, indicator in zip(TABLE_KINDS, ("rainfall", "ndvi", "flood"), strict=True):
        jobs = [j for j in plan_jobs(cfg) if j.kind == kind]
        if not all(j.name in done for j in jobs):
            written[indicator] = None
            continue
        columns = RAW_COLUMNS[indicator]
        frames = [tidy_features(read_table(f"{root}/{j.name}", columns), columns) for j in jobs]
        path = raw_dir / f"{indicator}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pd.concat(frames).sort_values(["county_code", "month"]).round(RAW_ROUNDING)
        table.to_csv(path, index=False)
        written[indicator] = path
    return written


def _radar_fix_table(cfg: Config, root: str, counties_gdf: gpd.GeoDataFrame, months: list[str]):
    from ssd_monitor import radar_fixes

    years = {m[:4] for m in months}
    if len(years) != 1:
        raise ValueError("one pixel-class table covers months of a single year")
    layers = ee.Image(f"{root}/flood_layers_{years.pop()}")
    return radar_fixes.class_table(
        cfg, root, layers, months, counties_to_ee(counties_gdf), search_area_to_ee(counties_gdf)
    )


def profile_radar_fix_month(
    cfg: Config, root: str, counties_gdf: gpd.GeoDataFrame, month: str
) -> tuple[list, float]:
    """One month of the pixel-class table, computed in Earth Engine under the profiler: (features, EECU-s).

    This measured the cost of scoring the fixes in Earth Engine (about 1,080 EECU-seconds a month,
    which is why the test runs locally) and made the reference table that reproduce-check compares
    the local route with (outputs/radar_fix_test/ee_pixel_classes_2020_03.csv).
    """
    import io

    buffer = io.StringIO()
    with ee.profilePrinting(destination=buffer):
        features = _radar_fix_table(cfg, root, counties_gdf, [month]).getInfo()["features"]
    return features, profile_eecu_seconds(buffer.getvalue())


def profile_eecu_seconds(profile_text: str) -> float:
    """Total EECU-seconds in the text ee.profilePrinting writes (first column of each row)."""
    import re

    total = 0.0
    for line in profile_text.splitlines():
        match = re.match(r"\s*([0-9.]+)\s+\S+\s+\d+\s", line)
        if match:
            total += float(match.group(1))
    return total


def download_frequency(cfg: Config, root: str, counties_gdf: gpd.GeoDataFrame, path: Path) -> Path | None:
    """Sum months_flooded over the yearly layers assets and download it as one GeoTIFF on a ~550 m grid."""
    jobs = [j for j in plan_jobs(cfg) if j.kind == "flood_layers"]
    if not all(j.name in existing_assets(root) for j in jobs):
        return None
    yearly = [ee.Image(f"{root}/{j.name}") for j in jobs]
    native = yearly[0].select("months_flooded").projection()
    total = (
        ee.ImageCollection([y.select("months_flooded").toFloat() for y in yearly])
        .sum()
        .setDefaultProjection(native)
    )
    image = total.addBands(yearly[0].select("excluded").toFloat())

    grid = rasters.grid_for_bounds(counties_gdf.total_bounds, cfg.flood.frequency_cell_deg)
    months_x10 = np.full((grid.height, grid.width), rasters.NODATA, dtype="int16")
    excluded_pct = months_x10.copy()
    # One request for the whole country runs out of memory in Earth Engine, so fetch it in tiles.
    for row, col, tile in rasters.split_grid(grid, FREQUENCY_TILE_CELLS):
        pixels = _frequency_pixels(image, tile)
        months_x10[row : row + tile.height, col : col + tile.width] = pixels["months_flooded"]
        excluded_pct[row : row + tile.height, col : col + tile.width] = pixels["excluded"]
    rasters.write_frequency(path, months_x10, excluded_pct, grid)
    return path


def _frequency_pixels(image: ee.Image, grid: rasters.Grid) -> np.ndarray:
    """Average the 100 m pixels into the grid's cells, as integers (months x 10, excluded %)."""
    cells = image.reduceResolution(ee.Reducer.mean(), maxPixels=1024).reproject(
        crs="EPSG:4326", crsTransform=grid.crs_transform
    )
    scaled = (
        ee.Image.cat(
            [
                cells.select("months_flooded").multiply(10).round(),
                cells.select("excluded").multiply(100).round(),
            ]
        )
        .unmask(-1)
        .toInt16()
    )
    return ee.data.computePixels(
        {
            "expression": scaled,
            "fileFormat": "NUMPY_NDARRAY",
            "grid": {
                "dimensions": {"width": grid.width, "height": grid.height},
                "affineTransform": {
                    "scaleX": grid.cell,
                    "shearX": 0,
                    "translateX": grid.west,
                    "shearY": 0,
                    "scaleY": -grid.cell,
                    "translateY": grid.north,
                },
                "crsCode": "EPSG:4326",
            },
        }
    )


# --- Local cache downloads -------------------------------------------------------------------------


def layer_grid(root: str, year: int) -> rasters.Grid:
    """The 100 m grid of the stored flood layers (every cached file uses it)."""
    return layer_cache.grid_from_asset(ee.data.getAsset(f"{root}/flood_layers_{year}"))


def _fetch_tile(image: ee.Image, grid: rasters.Grid, file_format: str = "GEO_TIFF", tries: int = 5):
    """One tile (GeoTIFF bytes, or a numpy array) and the EECU-seconds the profiler measured for it.

    GEO_TIFF comes back compressed (about 0.1 MB for a tile that is 20 MB as a numpy array), so
    tiles are saved as Earth Engine returns them. Transient errors are retried. The profiler hook is
    per thread in the Earth Engine client, so each worker thread profiles its own requests.
    """
    import io
    import time

    request = {
        "expression": image,
        "fileFormat": file_format,
        "grid": {
            "dimensions": {"width": grid.width, "height": grid.height},
            "affineTransform": {
                "scaleX": grid.cell,
                "shearX": 0,
                "translateX": grid.west,
                "shearY": 0,
                "scaleY": -grid.cell,
                "translateY": grid.north,
            },
            "crsCode": "EPSG:4326",
        },
    }
    for attempt in range(tries):
        buffer = io.StringIO()
        pixels = None
        try:
            with ee.profilePrinting(destination=buffer):
                pixels = ee.data.computePixels(request)
            return pixels, profile_eecu_seconds(buffer.getvalue())
        except ee.EEException as error:
            if pixels is not None and "Profile" in str(error):
                # The pixels arrived but the profile couldn't be fetched (this happens now and then
                # under parallel load): keep them, and let the caller estimate the cost.
                return pixels, None
            transient = any(
                w in str(error) for w in ("Too many", "429", "500", "503", "timed out", "Deadline")
            )
            if not transient or attempt == tries - 1:
                raise
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("unreachable")


def download_cache(
    grid: rasters.Grid,
    files: list[tuple[layer_cache.CacheFile, ee.Image]],
    cache_dir: Path,
    tile_px: int,
    workers: int,
    log=print,
) -> dict[str, float]:
    """Download every layer's missing tiles into the cache. Returns EECU-seconds used per layer.

    Safe to run again: tiles already saved are skipped. Each worker fetches a tile and saves it.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def fetch_and_save(folder: Path, image: ee.Image, tile: tuple[int, int, int, int]) -> float | None:
        data, eecu = _fetch_tile(image, layer_cache.tile_grid(grid, *tile))
        layer_cache.save_tile(folder, tile[0], tile[1], data)
        return eecu

    used: dict[str, float] = {}
    for spec, image in files:
        folder = layer_cache.layer_dir(cache_dir, spec.name)
        layer_cache.create(folder, grid, spec, tile_px)
        todo = layer_cache.missing_tiles(folder, grid, tile_px)
        used[spec.name] = 0.0
        if not todo:
            log(f"{spec.name}: already complete")
            continue
        ordered = image.select(list(spec.bands))
        unmeasured = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch_and_save, folder, ordered, t) for t in todo]
            for n, future in enumerate(as_completed(futures), start=1):
                eecu = future.result()
                if eecu is None:
                    unmeasured += 1
                else:
                    used[spec.name] += eecu
                if n % 33 == 0 or n == len(todo):
                    log(f"{spec.name}: {n}/{len(todo)} tiles, {used[spec.name]:.0f} EECU-s so far")
        if unmeasured:
            measured = len(todo) - unmeasured
            estimate = used[spec.name] / measured * unmeasured if measured else 0.0
            used[spec.name] += estimate
            log(
                f"{spec.name}: {unmeasured} tiles had no profile; added {estimate:.0f} EECU-s (measured mean)"
            )
    return used


def download_pixel_area(grid: rasters.Grid, path: Path) -> np.ndarray:
    """Earth Engine's area of one pixel in each grid row, in km2 (it depends only on latitude)."""
    column = layer_cache.tile_grid(grid, 0, grid.width // 2, grid.height, 1)
    image = ee.Image.pixelArea().divide(1e6).toDouble().rename("km2")
    pixels, _ = _fetch_tile(image, column, file_format="NUMPY_NDARRAY")
    area = np.asarray(pixels["km2"], dtype="float64").reshape(grid.height)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.save(path, area)
    return area


def optical_clear_names(years: list[int]) -> dict[int, str]:
    return {year: f"optical_clear_{year}" for year in years}


def submit_optical_clear(
    cfg: Config, root: str, counties_gdf: gpd.GeoDataFrame, months_by_year: dict[int, list[str]]
):
    """Start a batch export of the Sentinel-2 clear-view layer for each year not done or running.

    Batch exports cost far less than computing the layer tile by tile (the cost test measured 0.5-1.3
    EECU-hours a month by tiles), and judging clear views on 200 m cells a quarter of 100 m.
    """
    from ssd_monitor import radar_fixes

    country, search = country_to_ee(counties_gdf), search_area_to_ee(counties_gdf)
    active = {n for n, op in operations().items() if op["metadata"].get("state") in ACTIVE_STATES}
    done = existing_assets(root)
    started = []
    for year, name in optical_clear_names(list(months_by_year)).items():
        if name in done or name in active:
            continue
        _, image = radar_fixes.optical_clear_file(cfg, search, months_by_year[year], name)
        ee.batch.Export.image.toAsset(
            image=image.clip(country),
            description=DESCRIPTION_PREFIX + name,
            assetId=f"{root}/{name}",
            region=country.bounds(),
            scale=cfg.radar_fix_test.optical_clear_scale_m,
            crs="EPSG:4326",
            maxPixels=MAX_PIXELS,
            pyramidingPolicy={".default": "sample"},
        ).start()
        started.append(name)
    return started
