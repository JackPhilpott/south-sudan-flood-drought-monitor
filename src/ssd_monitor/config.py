"""Settings: config.yml for the method, EE_PROJECT from the environment for the Earth Engine project."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.yml"


@dataclass(frozen=True)
class RainfallConfig:
    collection: str
    normal_years: tuple[int, int]
    min_normal_mm: float
    scale_m: float
    years_per_task: int


@dataclass(frozen=True)
class VegetationConfig:
    collection: str
    baseline_years: tuple[int, int]
    max_summary_qa: int
    worldcover_collection: str
    exclude_worldcover: tuple[int, ...]
    scale_m: float
    years_per_task: int


@dataclass(frozen=True)
class SarConfig:
    collection: str
    orbit_passes: tuple[str, ...]
    vv_water_max_db: float


@dataclass(frozen=True)
class OpticalConfig:
    collection: str
    cloud_score_collection: str
    max_scene_cloud_pct: float
    min_clear_score: float
    mndwi_water_min: float


@dataclass(frozen=True)
class PermanentWaterConfig:
    collection: str
    min_occurrence_pct: float


@dataclass(frozen=True)
class TerrainConfig:
    dem: str
    max_slope_deg: float


@dataclass(frozen=True)
class FloodConfig:
    scale_m: float
    sar: SarConfig
    optical: OpticalConfig
    permanent_water: PermanentWaterConfig
    terrain: TerrainConfig
    min_connected_pixels: int
    years_per_task: int
    frequency_cell_deg: float


@dataclass(frozen=True)
class AlertThresholds:
    rain_pct_normal_below: float
    ndvi_anomaly_below: float
    flooded_pct_above: float


@dataclass(frozen=True)
class Config:
    start_month: str
    end_month: str
    boundaries: Path
    outputs: Path
    asset_folder: str
    rainfall: RainfallConfig
    vegetation: VegetationConfig
    flood: FloodConfig
    alerts: AlertThresholds

    @property
    def start_year(self) -> int:
        return int(self.start_month[:4])

    @property
    def end_year(self) -> int:
        return int(self.end_month[:4])


def _tuples(section: dict) -> dict:
    """YAML lists become tuples, so the frozen dataclasses stay hashable and read-only."""
    return {k: tuple(v) if isinstance(v, list) else v for k, v in section.items()}


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    """Read config.yml. Unknown or missing keys raise a TypeError naming the key."""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    root = path.resolve().parent
    flood = dict(raw["flood"])
    flood_cfg = FloodConfig(
        sar=SarConfig(**_tuples(flood.pop("sar"))),
        optical=OpticalConfig(**flood.pop("optical")),
        permanent_water=PermanentWaterConfig(**flood.pop("permanent_water")),
        terrain=TerrainConfig(**flood.pop("terrain")),
        **flood,
    )
    cfg = Config(
        start_month=str(raw["period"]["start"]),
        end_month=str(raw["period"]["end"]),
        boundaries=root / raw["paths"]["boundaries"],
        outputs=root / raw["paths"]["outputs"],
        asset_folder=raw["earth_engine"]["asset_folder"],
        rainfall=RainfallConfig(**_tuples(raw["rainfall"])),
        vegetation=VegetationConfig(**_tuples(raw["vegetation"])),
        flood=flood_cfg,
        alerts=AlertThresholds(**raw["alerts"]),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    """Catch settings that would silently give wrong results."""
    if cfg.start_month > cfg.end_month:
        raise ValueError(f"period.start {cfg.start_month} is after period.end {cfg.end_month}")
    for name, (first, last) in [
        ("rainfall.normal_years", cfg.rainfall.normal_years),
        ("vegetation.baseline_years", cfg.vegetation.baseline_years),
    ]:
        if first > last:
            raise ValueError(f"{name}: {first} is after {last}")
    if cfg.vegetation.baseline_years[0] < 2001:
        raise ValueError("vegetation.baseline_years must start in 2001 or later (MOD13Q1 starts in 2000)")
    bad_passes = set(cfg.flood.sar.orbit_passes) - {"ASCENDING", "DESCENDING"}
    if bad_passes:
        raise ValueError(f"flood.sar.orbit_passes: unknown pass {sorted(bad_passes)}")


def ee_project() -> str:
    """The Google Cloud project registered for Earth Engine, from EE_PROJECT."""
    project = os.environ.get("EE_PROJECT", "").strip()
    if not project:
        raise RuntimeError(
            "EE_PROJECT is not set. Set it to the Google Cloud project you registered for Earth Engine, "
            "e.g. `export EE_PROJECT=my-project` (PowerShell: `$env:EE_PROJECT = 'my-project'`)."
        )
    return project


def asset_root(project: str, folder: str) -> str:
    """The Earth Engine asset folder the exports are written to."""
    return f"projects/{project}/assets/{folder}"
