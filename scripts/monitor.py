"""South Sudan flood and drought monitor: command-line entry point.

    python scripts/monitor.py <command>

    boundaries  download COD-AB from HDX and rebuild data/ssd_admin2_simplified.geojson
    submit      start the Earth Engine exports that are ready (run again until all are done)
    status      show each export: done, running, ready, waiting or failed
    download    copy finished exports to outputs/raw/ and outputs/flood_frequency_*.tif
    build       outputs/county_monthly.csv and national_monthly.csv from outputs/raw/
    plots       the four figures in outputs/figures/

submit, status and download need Earth Engine and the EE_PROJECT environment variable.
boundaries, build and plots don't.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from ssd_monitor import boundaries, plots, rasters, stats  # noqa: E402
from ssd_monitor.config import Config, asset_root, ee_project, load_config  # noqa: E402


def frequency_path(cfg: Config) -> Path:
    return cfg.outputs / f"flood_frequency_{cfg.start_year}_{cfg.end_year}.tif"


def cmd_boundaries(cfg: Config) -> None:
    raw_dir = cfg.boundaries.parent / "raw"
    archive = boundaries.download_cod_ab(raw_dir)
    counties = boundaries.make_counties(boundaries.read_admin2(archive))
    boundaries.save_counties(counties, cfg.boundaries)
    size_kb = cfg.boundaries.stat().st_size / 1024
    print(f"{len(counties)} counties written to {cfg.boundaries} ({size_kb:.0f} KB)")


def _earth_engine(cfg: Config) -> str:
    from ssd_monitor import exports

    project = ee_project()
    exports.initialise(project)
    return asset_root(project, cfg.asset_folder)


def cmd_submit(cfg: Config) -> None:
    from ssd_monitor import exports

    root = _earth_engine(cfg)
    plan = exports.submit(cfg, root, boundaries.load_counties(cfg.boundaries))
    print(f"Started {len(plan.start)}: {', '.join(j.name for j in plan.start) or '-'}")
    print(f"Already running {len(plan.running)}, done {len(plan.done)}, waiting {len(plan.waiting)}")
    if plan.waiting:
        print("Waiting jobs need s1_reference: run `submit` again once it has finished.")


def cmd_status(cfg: Config) -> None:
    from ssd_monitor import exports

    root = _earth_engine(cfg)
    plan, ops = exports.current_plan(cfg, root)
    groups = [
        ("done", plan.done),
        ("running", plan.running),
        ("ready", plan.start),
        ("waiting", plan.waiting),
    ]
    for label, jobs in groups:
        for job in jobs:
            line = f"{label:8} {job.name}"
            op = ops.get(job.name)
            if label == "ready" and op and op["metadata"].get("state") == "FAILED":
                line += f"  (last attempt failed: {op.get('error', {}).get('message', 'no message')})"
            print(line)


def cmd_download(cfg: Config) -> None:
    from ssd_monitor import exports

    root = _earth_engine(cfg)
    for indicator, path in exports.download_tables(cfg, root, cfg.outputs / "raw").items():
        print(f"{indicator:9} {path or 'not all exports have finished'}")
    tif = exports.download_frequency(cfg, root, boundaries.load_counties(cfg.boundaries), frequency_path(cfg))
    print(f"frequency {tif or 'not all exports have finished'}")


def cmd_build(cfg: Config) -> None:
    raw_dir = cfg.outputs / "raw"
    raw = {
        name: pd.read_csv(raw_dir / f"{name}.csv", dtype={"county_code": str, "month": str})
        for name in stats.RAW_COLUMNS
    }
    counties = boundaries.county_table(boundaries.load_counties(cfg.boundaries))
    county_monthly = stats.build_county_monthly(counties, raw["rainfall"], raw["ndvi"], raw["flood"], cfg)
    national = stats.national_monthly(county_monthly, cfg.rainfall.min_normal_mm)
    county_monthly.to_csv(cfg.outputs / "county_monthly.csv", index=False)
    national.to_csv(cfg.outputs / "national_monthly.csv", index=False)
    print(f"county_monthly.csv: {len(county_monthly)} rows; national_monthly.csv: {len(national)} rows")


def cmd_plots(cfg: Config) -> None:
    counties = boundaries.load_counties(cfg.boundaries)
    county_monthly = pd.read_csv(cfg.outputs / "county_monthly.csv", dtype={"county_code": str, "month": str})
    for col in ("flag_rain_low", "flag_ndvi_low", "flag_flood"):
        county_monthly[col] = county_monthly[col].astype("boolean")
    national = pd.read_csv(cfg.outputs / "national_monthly.csv", dtype={"month": str})
    out = cfg.outputs / "figures"
    period = f"{cfg.start_year}-{cfg.end_year}"

    raster = rasters.read_frequency(frequency_path(cfg))
    plots.save(
        plots.plot_flood_frequency(raster, counties, period, cfg.flood.scale_m), out / "flood_frequency.png"
    )
    plots.save(
        plots.plot_national(national, cfg.alerts.rain_pct_normal_below, cfg.rainfall.min_normal_mm),
        out / "national_monthly.png",
    )
    flood_month = stats.peak_flood_month(national)
    if flood_month:
        fig = plots.plot_county_flood(counties, county_monthly, flood_month, cfg.alerts.flooded_pct_above)
        plots.save(fig, out / "county_flood_peak.png")
    drought_month = stats.worst_vegetation_month(national)
    if drought_month:
        fig = plots.plot_county_ndvi(counties, county_monthly, drought_month, cfg.alerts.ndvi_anomaly_below)
        plots.save(fig, out / "county_ndvi_drought.png")
    print(f"Figures written to {out} (peak flood month {flood_month}, drought month {drought_month})")


COMMANDS = {
    "boundaries": cmd_boundaries,
    "submit": cmd_submit,
    "status": cmd_status,
    "download": cmd_download,
    "build": cmd_build,
    "plots": cmd_plots,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--config", default=None, help="path to config.yml (default: the one in the repo)")
    args = parser.parse_args(argv)
    cfg = load_config(args.config) if args.config else load_config()
    COMMANDS[args.command](cfg)


if __name__ == "__main__":
    main()
