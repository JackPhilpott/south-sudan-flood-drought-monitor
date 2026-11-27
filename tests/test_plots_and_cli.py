"""Figures from the synthetic fixture, the offline CLI commands, and keeping Earth Engine isolated."""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

import monitor
from ssd_monitor import plots
from ssd_monitor.boundaries import save_counties
from ssd_monitor.rasters import write_frequency
from ssd_monitor.stats import build_county_monthly, national_monthly

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tables(counties, raw, cfg):
    county_monthly = build_county_monthly(counties, raw["rainfall"], raw["ndvi"], raw["flood"], cfg)
    return county_monthly, national_monthly(county_monthly, cfg.rainfall.min_normal_mm)


def texts(fig):
    return [t.get_text() for t in fig.texts] + [fig._suptitle.get_text()]


def legend_labels(fig):
    return [t.get_text() for ax in fig.axes if ax.get_legend() for t in ax.get_legend().get_texts()]


def test_flood_frequency_map(frequency, counties_gdf):
    fig = plots.plot_flood_frequency(frequency, counties_gdf, "2019-2025", 100)
    assert "Open water, 2019-2025" in texts(fig)
    assert "New water, 2019-2025" in texts(
        plots.plot_flood_frequency(frequency, counties_gdf, "2019-2025", 100, True)
    )
    assert "24 or more" in legend_labels(fig)
    assert any("Permanent water" in label for label in legend_labels(fig))


def test_national_chart_has_two_panels_and_labels_the_peak(tables, cfg):
    _, national = tables
    fig = plots.plot_national(national, cfg.alerts.rain_pct_normal_below, cfg.rainfall.min_normal_mm)
    top, bottom = fig.axes
    assert len(top.patches) == 84  # one bar per month (dry months have no height)
    stacked = [
        c.get_label() for c in bottom.collections if c.get_label() in ("persistent water", "new water")
    ]
    assert stacked == ["persistent water", "new water"]
    assert any(t.get_text().startswith("October 2020:") for t in bottom.texts)


def test_county_flood_map_outlines_flagged_counties(counties_gdf, tables, cfg):
    county_monthly, _ = tables
    fig = plots.plot_county_flood(counties_gdf, county_monthly, "2020-10", cfg.alerts.flood_new_percentile)
    assert "New water in each county, October 2020" in texts(fig)
    assert "Flagged: above the county's\nown 80th percentile" in legend_labels(fig)
    labels = [t.get_text() for t in fig.axes[0].texts]
    assert "Bravo" in labels


def test_county_map_marks_missing_data(counties_gdf, tables, cfg):
    county_monthly, _ = tables
    fig = plots.plot_county_flood(counties_gdf, county_monthly, "2019-07", cfg.alerts.flood_new_percentile)
    assert "No data" in legend_labels(fig)


def test_county_ndvi_map(counties_gdf, tables, cfg):
    county_monthly, _ = tables
    fig = plots.plot_county_ndvi(counties_gdf, county_monthly, "2022-01", cfg.alerts.ndvi_anomaly_below)
    assert "Vegetation anomaly by county, January 2022" in texts(fig)
    assert "Charlie" in [t.get_text() for t in fig.axes[0].texts]


def test_county_ndvi_map_can_pick_out_a_state(counties_gdf, tables, cfg):
    county_monthly, _ = tables
    fig = plots.plot_county_ndvi(
        counties_gdf, county_monthly, "2022-01", cfg.alerts.ndvi_anomaly_below, highlight_state="South"
    )
    assert "South" in legend_labels(fig)
    assert not fig.axes[0].texts  # the state outline is the focus: no county labels
    assert any("Flagged in South: Charlie" in t for t in texts(fig))
    assert cfg.figures.ndvi_month == "2022-03"


def test_save_writes_a_png(tmp_path, frequency, counties_gdf):
    path = tmp_path / "figures" / "map.png"
    plots.save(plots.plot_flood_frequency(frequency, counties_gdf, "2019-2025", 100), path)
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_build_and_plots_commands_run_offline(tmp_path, counties_gdf, raw, frequency):
    """The two CLI commands that rebuild everything from cached outputs, without Earth Engine."""
    config = yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))
    config["paths"] = {"boundaries": "counties.geojson", "outputs": "outputs", "cache": "cache"}
    (tmp_path / "config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    save_counties(counties_gdf, tmp_path / "counties.geojson")
    for name, df in raw.items():
        (tmp_path / "outputs" / "raw").mkdir(parents=True, exist_ok=True)
        df.to_csv(tmp_path / "outputs" / "raw" / f"{name}.csv", index=False)
    for name in ("flood_frequency_2019_2025.tif", "new_water_frequency_2019_2025.tif"):
        write_frequency(
            tmp_path / "outputs" / name,
            np.round(frequency.months * 10),
            np.round(frequency.excluded * 100),
            frequency.grid,
        )

    monitor.main(["build", "--config", str(tmp_path / "config.yml")])
    monitor.main(["plots", "--config", str(tmp_path / "config.yml")])

    outputs = tmp_path / "outputs"
    assert (outputs / "county_monthly.csv").exists() and (outputs / "national_monthly.csv").exists()
    figures = sorted(p.name for p in (outputs / "figures").glob("*.png"))
    assert figures == [
        "county_flood_peak.png",
        "county_ndvi_drought.png",
        "flood_frequency.png",
        "national_monthly.png",
        "new_water_frequency.png",
    ]


def test_offline_modules_never_import_earth_engine():
    code = (
        "import sys; sys.path[:0] = ['src', 'scripts']; "
        "import monitor, ssd_monitor.stats, ssd_monitor.alerts, ssd_monitor.plots, ssd_monitor.tasks, "
        "ssd_monitor.rasters, ssd_monitor.boundaries, ssd_monitor.layer_cache, ssd_monitor.local_fixes, "
        "ssd_monitor.rebuild, ssd_monitor.flood_flags, ssd_monitor.fix_scores, ssd_monitor.pixel_classes, "
        "ssd_monitor.ocha; print('ee' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_earth_engine_modules_import():
    """They can't run without an account, but they must at least import cleanly."""
    import ssd_monitor.exports  # noqa: F401
    import ssd_monitor.validation  # noqa: F401
