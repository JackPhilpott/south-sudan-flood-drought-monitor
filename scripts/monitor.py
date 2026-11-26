"""South Sudan flood and drought monitor: command-line entry point.

    python scripts/monitor.py <command>

    boundaries  download COD-AB from HDX and rebuild data/ssd_admin2_simplified.geojson
    cost-test   export one month of everything, then estimate the full run's EECU-hours (run again)
    submit      start the Earth Engine exports that are ready (run again until all are done)
    status      show each export: done, running, ready, waiting or failed, with the EECU it used
    download    copy finished exports to outputs/raw/ and outputs/flood_frequency_*.tif
    benchmark   compare the flood layers with JRC's Landsat monthly water on sample county-months
    build       outputs/county_monthly.csv and national_monthly.csv from outputs/raw/
    plots       the four figures in outputs/figures/

cost-test, submit, status, download and benchmark need Earth Engine and the EE_PROJECT variable.
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


def cmd_submit(cfg: Config, flood_years: str | None = None) -> None:
    from ssd_monitor import exports, tasks

    root = _earth_engine(cfg)
    jobs = tasks.plan_jobs(cfg)
    if flood_years:
        first, last = (int(y) for y in flood_years.split("-"))
        jobs = tasks.select_flood_years(jobs, first, last)
    plan = exports.submit(cfg, root, boundaries.load_counties(cfg.boundaries), jobs)
    print(f"Started {len(plan.start)}: {', '.join(j.name for j in plan.start) or '-'}")
    print(f"Already running {len(plan.running)}, done {len(plan.done)}, waiting {len(plan.waiting)}")
    if plan.waiting:
        print("Waiting flood tables start once their layers exist: run `submit` again then.")


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
    total = 0.0
    for label, jobs in groups:
        for job in jobs:
            line = f"{label:8} {job.name}"
            op = ops.get(job.name)
            eecu = exports.eecu_seconds(op) if op else None
            if label == "done" and eecu is not None:
                total += eecu
                line += f"  {eecu / 3600:.2f} EECU-hours"
            if label == "ready" and op and op["metadata"].get("state") == "FAILED":
                line += f"  (last attempt failed: {op.get('error', {}).get('message', 'no message')})"
            print(line)
    print(f"EECU-hours used by the finished exports: {total / 3600:.2f}")


def cmd_cost_test(cfg: Config, month: str) -> None:
    from ssd_monitor import exports, tasks

    root = _earth_engine(cfg)
    jobs = tasks.cost_test_jobs(month)
    plan = exports.submit(cfg, root, boundaries.load_counties(cfg.boundaries), jobs)
    print(
        f"Cost test for {month}: started {len(plan.start)}, running {len(plan.running)}, "
        f"waiting {len(plan.waiting)}, done {len(plan.done)}"
    )
    if len(plan.done) < len(jobs):
        print("Run `cost-test` again when the running exports have finished.")
        return
    _, ops = exports.current_plan(cfg, root, jobs)
    measured = {job.kind: exports.eecu_seconds(ops[job.name]) for job in jobs if job.name in ops}
    if len(measured) < len(jobs) or None in measured.values():
        print("Earth Engine hasn't reported the EECU usage of every export yet; try again shortly.")
        return
    estimate = tasks.estimate_full_run(measured, cfg)
    months = tasks.months_per_kind(cfg)
    print(f"{'export':16} {'EECU-s, 1 month':>16} {'months':>7} {'full run, EECU-h':>17}")
    for kind, seconds in measured.items():
        print(f"{kind:16} {seconds:16.1f} {months[kind]:7d} {estimate[kind]:17.2f}")
    print(f"{'total':16} {'':16} {'':7} {sum(estimate.values()):17.2f}")
    print(
        "Note: the EECU Earth Engine reports for table exports can be far below what its profiler "
        "measures for the same work; check them with ee.profilePrinting() or the Cloud console."
    )


def cmd_download(cfg: Config) -> None:
    from ssd_monitor import exports

    root = _earth_engine(cfg)
    for indicator, path in exports.download_tables(cfg, root, cfg.outputs / "raw").items():
        print(f"{indicator:9} {path or 'not all exports have finished'}")
    tif = exports.download_frequency(cfg, root, boundaries.load_counties(cfg.boundaries), frequency_path(cfg))
    print(f"frequency {tif or 'not all exports have finished'}")


def cmd_benchmark(cfg: Config) -> None:
    from ssd_monitor import validation
    from ssd_monitor.earthengine import counties_to_ee

    _earth_engine(cfg)
    rows = validation.benchmark(cfg.flood, counties_to_ee(boundaries.load_counties(cfg.boundaries)))
    pd.DataFrame(rows).round(2).to_csv(cfg.outputs / "benchmark_jrc.csv", index=False)
    summary = stats.summarise_agreement(rows)
    summary.to_csv(cfg.outputs / "benchmark_jrc_summary.csv", index=False)
    print(summary.to_string(index=False))


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
    "benchmark": cmd_benchmark,
    "build": cmd_build,
    "plots": cmd_plots,
}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=[*COMMANDS, "cost-test"])
    parser.add_argument("--config", default=None, help="path to config.yml (default: the one in the repo)")
    parser.add_argument("--month", default="2020-09", help="month for cost-test (default: 2020-09)")
    parser.add_argument("--flood-years", default=None, help="submit only these flood years, e.g. 2019-2022")
    args = parser.parse_args(argv)
    cfg = load_config(args.config) if args.config else load_config()
    if args.command == "cost-test":
        cmd_cost_test(cfg, args.month)
    elif args.command == "submit":
        cmd_submit(cfg, args.flood_years)
    else:
        COMMANDS[args.command](cfg)


if __name__ == "__main__":
    main()
