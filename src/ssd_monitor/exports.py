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

from ssd_monitor import flood, rainfall, rasters, vegetation
from ssd_monitor.config import Config
from ssd_monitor.earthengine import counties_to_ee, country_to_ee
from ssd_monitor.stats import RAW_COLUMNS, tidy_features
from ssd_monitor.tasks import TABLE_KINDS, Job, Plan, job_months, next_steps, plan_jobs

DESCRIPTION_PREFIX = "ssd_monitor_"
ACTIVE_STATES = {"PENDING", "RUNNING", "CANCELLING"}
MAX_PIXELS = 1e11
PAGE_SIZE = 5000  # features per request when reading a table


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


def current_plan(cfg: Config, root: str) -> tuple[Plan, dict[str, dict]]:
    ops = operations()
    active = {name for name, op in ops.items() if op["metadata"].get("state") in ACTIVE_STATES}
    return next_steps(plan_jobs(cfg), existing_assets(root), active), ops


def _start(job: Job, cfg: Config, root: str, counties: ee.FeatureCollection, country: ee.Geometry) -> None:
    asset_id = f"{root}/{job.name}"
    description = DESCRIPTION_PREFIX + job.name
    if job.kind == "s1_reference":
        task = ee.batch.Export.image.toAsset(
            image=flood.s1_reference(cfg.flood.sar, country).clip(country),
            description=description,
            assetId=asset_id,
            region=country,
            scale=cfg.flood.scale_m,
            crs="EPSG:4326",
            maxPixels=MAX_PIXELS,
            pyramidingPolicy={".default": "mean"},
        )
    elif job.kind == "flood_frequency":
        reference = ee.Image(f"{root}/s1_reference")
        task = ee.batch.Export.image.toAsset(
            image=flood.months_flooded(cfg.flood, reference, country, job_months(job, cfg)),
            description=description,
            assetId=asset_id,
            region=country,
            scale=cfg.flood.scale_m,
            crs="EPSG:4326",
            maxPixels=MAX_PIXELS,
            pyramidingPolicy={".default": "mean"},
        )
    else:
        months = job_months(job, cfg)
        if job.kind == "rainfall":
            table = rainfall.rainfall_table(cfg.rainfall, months, counties)
        elif job.kind == "ndvi":
            table = vegetation.ndvi_table(cfg.vegetation, months, counties)
        elif job.kind == "flood_table":
            reference = ee.Image(f"{root}/s1_reference")
            table = flood.flood_table(cfg.flood, reference, country, months, counties)
        else:
            raise ValueError(f"unknown job kind {job.kind!r}")
        task = ee.batch.Export.table.toAsset(collection=table, description=description, assetId=asset_id)
    task.start()


def submit(cfg: Config, root: str, counties_gdf: gpd.GeoDataFrame) -> Plan:
    """Start every export that is ready and not done or running. Safe to run again."""
    ensure_folder(root)
    plan, _ = current_plan(cfg, root)
    counties, country = counties_to_ee(counties_gdf), country_to_ee(counties_gdf)
    for job in plan.start:
        _start(job, cfg, root, counties, country)
    return plan


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
        pd.concat(frames).sort_values(["county_code", "month"]).to_csv(path, index=False)
        written[indicator] = path
    return written


def download_frequency(cfg: Config, root: str, counties_gdf: gpd.GeoDataFrame, path: Path) -> Path | None:
    """Sum the yearly flood-frequency assets and download them as one GeoTIFF on a ~500 m grid."""
    jobs = [j for j in plan_jobs(cfg) if j.kind == "flood_frequency"]
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
    # Average the 100 m pixels into each cell, then scale to integers so the download stays small.
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
    pixels = ee.data.computePixels(
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
    rasters.write_frequency(path, np.asarray(pixels["months_flooded"]), np.asarray(pixels["excluded"]), grid)
    return path
