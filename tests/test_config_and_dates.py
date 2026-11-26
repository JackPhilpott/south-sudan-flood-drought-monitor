"""Settings and date windows."""

import dataclasses

import pytest

from ssd_monitor.config import asset_root, ee_project, load_config, validate
from ssd_monitor.dates import month_window, months_between, months_in_years, year_chunks

# --- config ------------------------------------------------------------------------------


def test_repo_config_loads_with_absolute_paths(cfg):
    assert (cfg.start_month, cfg.end_month) == ("2019-01", "2025-12")
    assert cfg.boundaries.is_absolute() and cfg.boundaries.exists()
    assert cfg.rainfall.normal_years == (1991, 2020)
    assert cfg.vegetation.baseline_years == (2001, 2020)
    assert cfg.flood.sar.orbit_passes == ("ASCENDING", "DESCENDING")


def test_unknown_key_is_an_error(tmp_path, cfg):
    text = (cfg.boundaries.parents[1] / "config.yml").read_text(encoding="utf-8")
    bad = tmp_path / "config.yml"
    bad.write_text(text.replace("min_normal_mm: 20", "min_normal_mm: 20\n  typo_key: 1"), encoding="utf-8")
    with pytest.raises(TypeError, match="typo_key"):
        load_config(bad)


def _with_sar(cfg, **changes):
    sar = dataclasses.replace(cfg.flood.sar, **changes)
    return dataclasses.replace(cfg, flood=dataclasses.replace(cfg.flood, sar=sar))


def test_validate_rejects_a_reversed_period(cfg):
    with pytest.raises(ValueError, match="after"):
        validate(dataclasses.replace(cfg, start_month="2026-01"))


def test_validate_rejects_an_unknown_orbit_pass(cfg):
    with pytest.raises(ValueError, match="unknown pass"):
        validate(_with_sar(cfg, orbit_passes=("SIDEWAYS",)))


def test_ee_project_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("EE_PROJECT", "  my-project ")
    assert ee_project() == "my-project"
    monkeypatch.delenv("EE_PROJECT")
    with pytest.raises(RuntimeError, match="EE_PROJECT"):
        ee_project()


def test_asset_root():
    assert asset_root("my-project", "ssd_monitor") == "projects/my-project/assets/ssd_monitor"


# --- dates -------------------------------------------------------------------------------


def test_months_between_includes_both_ends_and_crosses_years():
    assert months_between("2019-11", "2020-02") == ["2019-11", "2019-12", "2020-01", "2020-02"]
    assert len(months_between("2019-01", "2025-12")) == 84


def test_months_between_rejects_reversed_range():
    with pytest.raises(ValueError):
        months_between("2020-02", "2019-11")


def test_months_in_years():
    months = months_in_years(1991, 2020)
    assert len(months) == 360 and months[0] == "1991-01" and months[-1] == "2020-12"


@pytest.mark.parametrize(
    "month, window",
    [
        ("2020-02", ("2020-02-01", "2020-03-01")),  # leap year: end is still the 1st of the next month
        ("2019-12", ("2019-12-01", "2020-01-01")),
    ],
)
def test_month_window_is_half_open(month, window):
    assert month_window(month) == window


def test_year_chunks():
    assert year_chunks(1991, 2025, 35) == [(1991, 2025)]
    assert year_chunks(2001, 2025, 5) == [
        (2001, 2005),
        (2006, 2010),
        (2011, 2015),
        (2016, 2020),
        (2021, 2025),
    ]
    assert year_chunks(2019, 2025, 3) == [(2019, 2021), (2022, 2024), (2025, 2025)]
    with pytest.raises(ValueError):
        year_chunks(2019, 2025, 0)
