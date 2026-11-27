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
    download-layers  copy the stored flood layers and the extra layers to the local cache (--years)
    radar-fix-test   score every candidate radar fix against JRC water, locally (step 1)
    reproduce-check  check that the local route reproduces Earth Engine for one month (--month)
    ocha             download OCHA's flood-affected counties from HDX into data/ocha_flood_counties.csv
    optical-test     re-run the optical rule with stricter cloud and water tests on a sample (item 5)
    optical-clear    export the Sentinel-2 clear-view layer (--years, --dry-only), then cache it
    flag-check       compare the flood flag rules with OCHA's flood-affected counties
    rebuild          rebuild the flood layers locally with the chosen fix: outputs/raw/flood.csv, JRC
                     agreement and the frequency GeoTIFFs (needs download-layers for every year)

cost-test, submit, status, download, benchmark, download-layers and optical-test need Earth Engine
and the EE_PROJECT variable. boundaries, ocha, build, plots, radar-fix-test, reproduce-check and rebuild
don't.
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
    new_raster = rasters.read_frequency(
        cfg.outputs / f"new_water_frequency_{cfg.start_year}_{cfg.end_year}.tif"
    )
    plots.save(
        plots.plot_flood_frequency(new_raster, counties, period, cfg.flood.scale_m, new_only=True),
        out / "new_water_frequency.png",
    )
    plots.save(
        plots.plot_national(national, cfg.alerts.rain_pct_normal_below, cfg.rainfall.min_normal_mm),
        out / "national_monthly.png",
    )
    flood_month = stats.peak_flood_month(national)
    if flood_month:
        fig = plots.plot_county_flood(counties, county_monthly, flood_month, cfg.alerts.flood_new_percentile)
        plots.save(fig, out / "county_flood_peak.png")
    drought_month = stats.worst_vegetation_month(national)
    if drought_month:
        fig = plots.plot_county_ndvi(counties, county_monthly, drought_month, cfg.alerts.ndvi_anomaly_below)
        plots.save(fig, out / "county_ndvi_drought.png")
    print(f"Figures written to {out} (peak new-water month {flood_month}, drought month {drought_month})")


def _years(text: str | None, default: tuple[int, int]) -> list[int]:
    first, last = (int(y) for y in text.split("-")) if text else default
    return list(range(first, last + 1))


def cmd_download_layers(cfg: Config, years: str | None = None) -> None:
    from ssd_monitor import exports, layer_cache, radar_fixes
    from ssd_monitor.earthengine import search_area_to_ee

    root = _earth_engine(cfg)
    wanted = _years(years, cfg.radar_fix_test.years)
    grid = exports.layer_grid(root, wanted[0])
    area_path = cfg.cache / "pixel_area_km2.npy"
    if not area_path.exists():
        exports.download_pixel_area(grid, area_path)
    files = radar_fixes.cache_files(
        cfg, root, search_area_to_ee(boundaries.load_counties(cfg.boundaries)), wanted
    )
    used = exports.download_cache(grid, files, cfg.cache, cfg.download_tile_px, cfg.download_workers)
    log = cfg.cache / "eecu_log.csv"
    new = pd.DataFrame({"file": list(used), "eecu_seconds": list(used.values())})
    new.to_csv(log, mode="a", header=not log.exists(), index=False)
    print(f"EECU-hours for this download: {sum(used.values()) / 3600:.3f} (log: {log})")
    incomplete = [
        s.name for s, _ in files if not layer_cache.is_complete(layer_cache.layer_dir(cfg.cache, s.name))
    ]
    print("all files complete" if not incomplete else f"incomplete: {incomplete}")


def cmd_ocha(cfg: Config) -> None:
    from ssd_monitor import ocha

    raw_dir = cfg.boundaries.parent / "raw" / "ocha"
    ocha.download(raw_dir)
    table = ocha.reference_table(raw_dir, boundaries.county_table(boundaries.load_counties(cfg.boundaries)))
    path = cfg.boundaries.parent / "ocha_flood_counties.csv"
    table.to_csv(path, index=False)
    print(table.groupby("year").agg(counties=("county_code", "size"), partial=("partial", "max")).to_string())
    print(f"written to {path}")


def cmd_optical_test(cfg: Config) -> None:
    import io

    import ee

    from ssd_monitor import exports, validation
    from ssd_monitor.earthengine import counties_to_ee

    _earth_engine(cfg)
    counties = counties_to_ee(boundaries.load_counties(cfg.boundaries))
    path = cfg.outputs / "radar_fix_test" / "optical_variants.csv"
    done = (
        pd.read_csv(path, dtype={"month": str})
        if path.exists()
        else pd.DataFrame(columns=["county_code", "month"])
    )
    rows = done.to_dict("records")
    for month in validation.OPTICAL_SAMPLE_MONTHS:
        for code in validation.OPTICAL_SAMPLE_COUNTIES:
            if ((done["county_code"] == code) & (done["month"] == month)).any():
                continue
            region = counties.filter(ee.Filter.eq("county_code", code)).geometry()
            buffer, values = io.StringIO(), None
            try:
                with ee.profilePrinting(destination=buffer):
                    values = validation.optical_scores(cfg.flood, region, month).getInfo()
                eecu = exports.profile_eecu_seconds(buffer.getvalue())
            except ee.EEException as error:
                if values is None or "Profile" not in str(error):
                    raise
                eecu = None  # the result arrived but its profile couldn't be fetched
            rows.append({"county_code": code, "month": month, "eecu_seconds": eecu} | values)
            pd.DataFrame(rows).to_csv(path, index=False)
            print(f"{month} {code}: {eecu if eecu is None else round(eecu)} EECU-s", flush=True)
    table = pd.DataFrame(rows)
    measured = table["eecu_seconds"].dropna()
    total = measured.sum() + measured.mean() * table["eecu_seconds"].isna().sum()
    print(f"EECU-hours: {total / 3600:.3f} ({table['eecu_seconds'].isna().sum()} estimated at the mean)")


def cmd_flag_check(cfg: Config) -> None:
    """Score the flood flag rules against OCHA's flood-affected counties (needs build, ocha, rebuild)."""
    from ssd_monitor import flood_flags

    county_monthly = pd.read_csv(cfg.outputs / "county_monthly.csv", dtype={"county_code": str, "month": str})
    low = pd.read_csv(cfg.outputs / "raw" / "low_lying_land.csv", dtype={"county_code": str})
    reference = pd.read_csv(cfg.boundaries.parent / "ocha_flood_counties.csv", dtype={"county_code": str})
    check = cfg.flood_check
    table = county_monthly.merge(low[["county_code", "low_lying_km2"]], on="county_code", how="left")
    table = flood_flags.add_rule_flags(
        table,
        cfg.alerts.flood_new_percentile,
        cfg.alerts.flood_new_min_km2,
        check.low_lying_share_pct,
        check.old_flooded_pct_above,
    )
    years = flood_flags.county_years(table)
    scores = flood_flags.score(years, reference, county_monthly["county_code"].nunique())
    scores.round(2).to_csv(cfg.outputs / "flood_flag_check.csv", index=False)
    totals = scores.groupby("rule")[
        ["listed", "flagged", "hits", "misses", "false_alarms", "chance_hits"]
    ].sum()
    print(scores.round(1).to_string(index=False))
    print(totals.round(1).to_string())
    print(f"adopted (most hits above chance): {flood_flags.best_rule(scores)}")


def cmd_rebuild(cfg: Config) -> None:
    from ssd_monitor import rasters, rebuild
    from ssd_monitor.dates import months_between

    counties, grid, area = _local_inputs(cfg)
    months = months_between(cfg.start_month, cfg.end_month)
    work = cfg.cache / "rebuild"
    pixels = work / "frequency_100m.tif"
    flood, jrc = rebuild.rebuild(
        cfg, counties, grid, area, months, pixels, log=lambda t: print(t, flush=True)
    )
    raw = cfg.outputs / "raw"
    flood.round(stats.RAW_ROUNDING).to_csv(raw / "flood.csv", index=False)
    jrc.round(3).to_csv(raw / "jrc_agreement.csv", index=False)
    cells = rasters.grid_for_bounds(counties.total_bounds, cfg.flood.frequency_cell_deg)
    period = f"{cfg.start_year}_{cfg.end_year}"
    for band, name in (
        ("months_water", f"flood_frequency_{period}.tif"),
        ("months_new", f"new_water_frequency_{period}.tif"),
    ):
        months_x10, excluded_pct = rebuild.frequency_cells(pixels, cells, band)
        rasters.write_frequency(cfg.outputs / name, months_x10, excluded_pct, cells)
    print(
        f"flood.csv: {len(flood)} rows; jrc_agreement.csv: {len(jrc)} rows; frequency maps in {cfg.outputs}"
    )


def cmd_optical_clear(cfg: Config, years: str | None, dry_only: bool) -> None:
    import ee

    from ssd_monitor import exports, layer_cache
    from ssd_monitor.dates import months_in_years

    root = _earth_engine(cfg)
    wanted = _years(years, cfg.radar_fix_test.years)
    wet = cfg.radar_fix_test.wet_season_months
    months = {y: [m for m in months_in_years(y, y) if not (dry_only and int(m[5:7]) in wet)] for y in wanted}
    started = exports.submit_optical_clear(cfg, root, boundaries.load_counties(cfg.boundaries), months)
    print(f"started: {started or '-'}")
    done = exports.existing_assets(root)
    grid = exports.layer_grid(root, wanted[0])
    for name in exports.optical_clear_names(wanted).values():
        if name not in done:
            print(f"{name}: not finished yet; run again later")
            continue
        asset = ee.Image(f"{root}/{name}")
        bands = tuple(asset.bandNames().getInfo())
        spec = layer_cache.CacheFile(name, bands, "uint8", 255)
        used = exports.download_cache(
            grid,
            [(spec, asset.unmask(255, False).toByte())],
            cfg.cache,
            cfg.download_tile_px,
            cfg.download_workers,
        )
        log = cfg.cache / "eecu_log.csv"
        pd.DataFrame({"file": list(used), "eecu_seconds": list(used.values())}).to_csv(
            log, mode="a", header=not log.exists(), index=False
        )


TEST_COUNTIES = {
    "SS0801": "Gogrial East",
    "SS0804": "Tonj North",
    "SS0805": "Tonj South",
    "SS0203": "Kapoeta East",
}


def _local_inputs(cfg: Config):
    import numpy as np

    from ssd_monitor import layer_cache

    first_year = cfg.radar_fix_test.years[0]
    _, grid, _ = layer_cache.read_meta(layer_cache.layer_dir(cfg.cache, f"flood_layers_{first_year}"))
    return boundaries.load_counties(cfg.boundaries), grid, np.load(cfg.cache / "pixel_area_km2.npy")


def cmd_radar_fix_test(cfg: Config) -> None:
    from ssd_monitor import fix_scores, local_fixes, pixel_classes
    from ssd_monitor.dates import months_in_years

    counties, grid, area = _local_inputs(cfg)
    rules = pixel_classes.candidate_rules(cfg.radar_fix_test)
    work = cfg.cache / "radar_fix_test"
    work.mkdir(parents=True, exist_ok=True)
    rule_path, county_path = work / "rules.csv", work / "counties.csv"
    if not (rule_path.exists() and county_path.exists()):
        months = months_in_years(*cfg.radar_fix_test.years)
        rule_table, county_table = local_fixes.rule_tables(
            cfg, counties, grid, area, months, rules, log=lambda text: print(text, flush=True)
        )
        rule_table.to_csv(rule_path, index=False)
        county_table.to_csv(county_path, index=False)
    rule_table = pd.read_csv(rule_path, dtype={"month": str})
    county_table = pd.read_csv(county_path, dtype={"month": str})
    out = cfg.outputs / "radar_fix_test"
    out.mkdir(parents=True, exist_ok=True)
    wet = cfg.radar_fix_test.wet_season_months
    scores = fix_scores.score_rules(rule_table, county_table, wet)
    scores.round(4).to_csv(out / "rule_scores.csv", index=False)
    marches = [f"{y}-03" for y in range(cfg.radar_fix_test.years[0], cfg.radar_fix_test.years[1] + 1)]
    fw = fix_scores.false_water(rule_table, county_table, list(TEST_COUNTIES), marches)
    fw.insert(1, "county_name", fw["county_code"].map(TEST_COUNTIES))
    fw.round(2).to_csv(out / "false_water_march.csv", index=False)
    fix_scores.false_positive_causes(county_table, wet).round(4).to_csv(
        out / "false_positive_causes.csv", index=False
    )
    wide = scores.pivot_table(index="rule", columns="season", values=["recall", "precision"])
    print(wide.round(3).sort_values(("precision", "dry"), ascending=False).to_string())
    print(f"Tables written to {out}")


def cmd_veto_test(cfg: Config) -> None:
    """Score the optical veto on top of the chosen fix, and apply the decision rule set beforehand."""
    from ssd_monitor import fix_scores, local_fixes
    from ssd_monitor.dates import months_in_years

    counties, grid, area = _local_inputs(cfg)
    base = cfg.flood.radar_fix
    rules = {
        "chosen fix": base,
        "+ veto Dec-May (a)": (*base, "optical_veto_dry"),
        "+ veto all year (b)": (*base, "optical_veto"),
    }
    months = months_in_years(*cfg.radar_fix_test.years)
    rule_table, county_table = local_fixes.rule_tables(
        cfg, counties, grid, area, months, rules, log=lambda text: print(text, flush=True)
    )
    scores = fix_scores.score_rules(
        rule_table.replace({"rule": {"chosen fix": "current"}}),
        county_table,
        cfg.radar_fix_test.wet_season_months,
    )
    scores["rule"] = scores["rule"].replace({"current": "chosen fix"})
    out = cfg.outputs / "radar_fix_test"
    scores.round(4).to_csv(out / "veto_scores.csv", index=False)
    wide = scores.pivot_table(index="rule", columns="season", values=["recall", "precision", "removed_km2"])
    print(wide.round(3).to_string())
    a, b = wide.loc["+ veto Dec-May (a)"], wide.loc["chosen fix"]
    gain, loss = (
        a[("precision", "dry")] - b[("precision", "dry")],
        b[("recall", "all")] - a[("recall", "all")],
    )
    adopt = gain >= 0.05 and loss <= 0.01
    print(
        f"(a): dry-season precision {gain:+.3f}, overall recall {-loss:+.3f}: "
        f"{'adopt' if adopt else 'keep the fix'}"
    )


def cmd_reproduce_check(cfg: Config, month: str) -> None:
    from ssd_monitor import fix_scores, local_fixes, pixel_classes

    counties, grid, area = _local_inputs(cfg)
    bits = pixel_classes.class_bits(cfg.radar_fix_test)
    county_path = cfg.cache / "radar_fix_test" / "counties.csv"
    if county_path.exists():
        county_table = pd.read_csv(county_path, dtype={"month": str})
    else:
        _, county_table = local_fixes.rule_tables(cfg, counties, grid, area, [month], {"current": ()})
    flood_raw = pd.read_csv(cfg.outputs / "raw" / "flood.csv", dtype={"month": str})
    stored = fix_scores.compare_stored_sums(county_table, flood_raw, month)
    local_codes = local_fixes.class_codes(cfg, counties, grid, area, month)
    ee_codes = pd.read_csv(
        cfg.outputs / "radar_fix_test" / f"ee_pixel_classes_{month.replace('-', '_')}.csv",
        dtype={"month": str},
    )
    classes = fix_scores.compare_classes(local_codes, ee_codes, bits)
    single = {"current": ()} | {
        k: v
        for k, v in pixel_classes.candidate_rules(cfg.radar_fix_test).items()
        if "+" not in k and k != "current"
    }
    by_rule = {
        route: fix_scores.radar_only_by_condition(codes, bits, list(TEST_COUNTIES), single).rename(
            columns=TEST_COUNTIES
        )
        for route, codes in (("earth_engine", ee_codes), ("local", local_codes))
    }
    out = cfg.outputs / "radar_fix_test"
    stored.round(3).to_csv(out / f"reproduction_{month}_stored_sums.csv", index=False)
    classes.round(3).to_csv(out / f"reproduction_{month}_classes.csv", index=False)
    pd.concat(by_rule, names=["route", "rule"]).round(1).to_csv(out / f"reproduction_{month}_radar_only.csv")
    print("Stored flags, county sums: local vs Earth Engine (outputs/raw/flood.csv)")
    print(stored.round(3).to_string(index=False))
    print("\nPixel classes nationally: local vs Earth Engine (radar_fixes.class_table)")
    print(classes.round(2).to_string(index=False))
    print("\nRadar-only km2 after each single condition, before the patch filter")
    print(pd.concat(by_rule, names=["route", "rule"]).round(1).to_string())


COMMANDS = {
    "radar-fix-test": cmd_radar_fix_test,
    "ocha": cmd_ocha,
    "optical-test": cmd_optical_test,
    "rebuild": cmd_rebuild,
    "veto-test": cmd_veto_test,
    "flag-check": cmd_flag_check,
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
    parser.add_argument(
        "command", choices=[*COMMANDS, "cost-test", "download-layers", "reproduce-check", "optical-clear"]
    )
    parser.add_argument("--config", default=None, help="path to config.yml (default: the one in the repo)")
    parser.add_argument(
        "--month",
        default="2020-09",
        help="month for cost-test (default: 2020-09) or reproduce-check (use 2020-03)",
    )
    parser.add_argument("--flood-years", default=None, help="submit only these flood years, e.g. 2019-2022")
    parser.add_argument(
        "--years", default=None, help="download-layers: years, e.g. 2019-2021 (default: the test years)"
    )
    parser.add_argument("--dry-only", action="store_true", help="optical-clear: December to May only")
    args = parser.parse_args(argv)
    cfg = load_config(args.config) if args.config else load_config()
    if args.command == "cost-test":
        cmd_cost_test(cfg, args.month)
    elif args.command == "submit":
        cmd_submit(cfg, args.flood_years)
    elif args.command == "download-layers":
        cmd_download_layers(cfg, args.years)
    elif args.command == "reproduce-check":
        cmd_reproduce_check(cfg, args.month)
    elif args.command == "optical-clear":
        cmd_optical_clear(cfg, args.years, args.dry_only)
    else:
        COMMANDS[args.command](cfg)


if __name__ == "__main__":
    main()
