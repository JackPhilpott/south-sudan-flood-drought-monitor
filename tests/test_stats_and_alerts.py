"""Tidying Earth Engine results, normals, anomalies, flood shares, flags and the national summary."""

import numpy as np
import pandas as pd
import pytest

from ssd_monitor.alerts import add_flags, describe
from ssd_monitor.stats import (
    COUNTY_MONTHLY_COLUMNS,
    add_detrended_anomaly,
    add_flood_shares,
    add_percent_of_normal,
    build_county_monthly,
    climatology,
    national_monthly,
    peak_flood_month,
    summarise_agreement,
    tidy_features,
    worst_vegetation_month,
)


def feature(code, month, **props):
    """A feature shaped like Earth Engine's output. Null properties are left out, as Earth Engine does."""
    properties = {"county_code": code, "month": month} | {k: v for k, v in props.items() if v is not None}
    return {"type": "Feature", "geometry": None, "id": f"{code}_{month}", "properties": properties}


def history(values_by_year: dict[int, float], code="SS9901", month="06", column="rain_mm") -> pd.DataFrame:
    rows = [{"county_code": code, "month": f"{y}-{month}", column: v} for y, v in values_by_year.items()]
    return pd.DataFrame(rows)


# --- tidy_features -----------------------------------------------------------------------


def test_tidy_features_keeps_empty_months_as_nan():
    features = [
        feature("SS02", "2019-02", rain_mm=12.5),
        feature("SS01", "2019-01"),
        feature("SS01", "2019-02", rain_mm=3),
    ]
    df = tidy_features(features, ["rain_mm"])
    assert df[["county_code", "month"]].values.tolist() == [
        ["SS01", "2019-01"],
        ["SS01", "2019-02"],
        ["SS02", "2019-02"],
    ]
    assert np.isnan(df.loc[0, "rain_mm"]) and df.loc[2, "rain_mm"] == 12.5


def test_tidy_features_accepts_full_dates_and_explicit_nulls():
    df = tidy_features(
        [{"properties": {"county_code": "SS01", "month": "2019-01-01", "ndvi": None}}], ["ndvi"]
    )
    assert df.loc[0, "month"] == "2019-01" and np.isnan(df.loc[0, "ndvi"])


def test_tidy_features_with_no_features_gives_an_empty_table():
    df = tidy_features([], ["flooded_km2", "observed_km2"])
    assert df.empty and list(df.columns) == ["county_code", "month", "flooded_km2", "observed_km2"]


@pytest.mark.parametrize(
    "features, message",
    [
        ([feature("SS01", "2019-01", rain_mm=1), feature("SS01", "2019-01", rain_mm=2)], "more than one row"),
        ([{"properties": {"month": "2019-01", "rain_mm": 1}}], "no county_code"),
        ([feature("SS01", "Jan 2019", rain_mm=1)], "YYYY-MM"),
    ],
)
def test_tidy_features_rejects_bad_input(features, message):
    with pytest.raises(ValueError, match=message):
        tidy_features(features, ["rain_mm"])


# --- normals and anomalies ---------------------------------------------------------------


def test_climatology_uses_only_the_baseline_years():
    df = history({2001: 10.0, 2002: 20.0, 2003: 30.0, 2004: 1000.0})
    clim = climatology(df, "rain_mm", (2001, 2003))
    assert clim.loc[("SS9901", "06"), "mean"] == 20.0
    assert clim.loc[("SS9901", "06"), "std"] == 10.0


def test_climatology_is_blank_without_enough_years():
    df = history({2001: 10.0, 2002: 20.0, 2003: np.nan, 2004: np.nan})
    clim = climatology(df, "rain_mm", (2001, 2004))  # 2 of 4 years is under the 75% minimum
    assert clim.loc[("SS9901", "06")].isna().all()


def test_percent_of_normal():
    df = history({2001: 80.0, 2002: 120.0, 2003: 50.0})
    out = add_percent_of_normal(df, (2001, 2002), min_normal_mm=20).set_index("month")
    assert out.loc["2003-06", "rain_normal_mm"] == 100.0
    assert out.loc["2003-06", "rain_pct_normal"] == 50.0


def test_percent_of_normal_is_blank_in_dry_months():
    df = history({2001: 4.0, 2002: 6.0, 2003: 30.0}, month="01")
    out = add_percent_of_normal(df, (2001, 2002), min_normal_mm=20).set_index("month")
    assert out.loc["2003-01", "rain_normal_mm"] == 5.0  # the normal is kept...
    assert np.isnan(out.loc["2003-01", "rain_pct_normal"])  # ...but 600% of 5 mm is not reported


def test_detrended_anomaly_follows_each_county_trend():
    # Over the baseline NDVI rises about 0.012 a year with a wobble; 2006 is far below the trend.
    df = history({2001: 0.41, 2002: 0.41, 2003: 0.44, 2004: 0.43, 2005: 0.46, 2006: 0.40}, column="ndvi")
    out = add_detrended_anomaly(df, (2001, 2005)).set_index("month")
    assert out.loc["2006-06", "ndvi_expected"] == pytest.approx(0.466)  # 0.43 + 3 x 0.012, the line extended
    assert out.loc["2006-06", "ndvi_residual_sd"] == pytest.approx(0.010954, abs=1e-6)  # sqrt(360e-6 / 3)
    assert out.loc["2006-06", "ndvi_anomaly"] == pytest.approx(-6.025, abs=0.001)
    # The baseline years sit close to their own line.
    assert out.loc[[f"{y}-06" for y in range(2001, 2006)], "ndvi_anomaly"].abs().max() < 1.5


def test_detrended_anomaly_is_blank_without_enough_baseline_years():
    df = history({2001: 0.5, 2002: np.nan, 2003: np.nan, 2004: 0.4, 2005: 0.45}, column="ndvi")
    out = add_detrended_anomaly(df, (2001, 2004))
    assert out["ndvi_anomaly"].isna().all()


# --- flood shares ------------------------------------------------------------------------


def test_flood_shares_are_percent_of_county_area():
    areas = pd.DataFrame({"county_code": ["A"], "area_km2": [200.0]})
    flood = pd.DataFrame(
        {
            "county_code": ["A"],
            "month": ["2020-10"],
            "flooded_km2": [50.0],
            "flooded_sar_km2": [40.0],
            "flooded_optical_km2": [20.0],
            "observed_km2": [150.0],
            "persistent_km2": [30.0],
            "new_km2": [20.0],
        }
    )
    out = add_flood_shares(flood, areas).iloc[0]
    assert out["flooded_pct"] == 25.0 and out["flood_observed_pct"] == 75.0
    assert out["new_pct"] == 10.0


def test_flood_is_unknown_not_zero_when_nothing_was_observed():
    areas = pd.DataFrame({"county_code": ["A"], "area_km2": [200.0]})
    flood = pd.DataFrame(
        {
            "county_code": ["A"],
            "month": ["2019-07"],
            "flooded_km2": [0.0],
            "flooded_sar_km2": [0.0],
            "flooded_optical_km2": [0.0],
            "observed_km2": [0.0],
            "persistent_km2": [0.0],
            "new_km2": [0.0],
        }
    )
    out = add_flood_shares(flood, areas).iloc[0]
    assert np.isnan(out["flooded_km2"]) and np.isnan(out["flooded_pct"])
    assert np.isnan(out["new_km2"]) and np.isnan(out["new_pct"])
    assert out["flood_observed_pct"] == 0.0


# --- alerts ------------------------------------------------------------------------------


def test_flags_use_strict_thresholds_and_stay_blank_without_data(cfg):
    df = pd.DataFrame(
        {
            "county_code": "A",
            "month": ["2020-01", "2021-01", "2022-01"],
            "rain_pct_normal": [74.9, 75.0, np.nan],
            "ndvi_anomaly": [-1.01, -1.0, np.nan],
            "new_km2": [0.0, 0.0, np.nan],
        }
    )
    out = add_flags(df, cfg.alerts)
    for col in ("flag_rain_low", "flag_ndvi_low"):
        assert out[col].tolist()[:2] == [True, False]
        assert out[col].isna().tolist() == [False, False, True]
    assert out["flag_flood"].isna().tolist() == [False, False, True]


def test_flood_flag_is_relative_to_the_county_own_record(cfg):
    # One county's October new water over seven years: only the clearly unusual years are flagged,
    # and never a year below the 10 km2 floor.
    new = [2.0, 5.0, 20.0, 30.0, 12.0, 60.0, 15.0]
    df = pd.DataFrame({"county_code": "A", "month": [f"{y}-10" for y in range(2019, 2026)], "new_km2": new})
    out = add_flags(df.assign(rain_pct_normal=100.0, ndvi_anomaly=0.0), cfg.alerts)
    assert out["flag_flood"].tolist() == [False, False, False, True, False, True, False]
    tiny = df.assign(new_km2=[0.1, 0.2, 0.3, 0.4, 0.5, 9.0, 0.6])
    assert not add_flags(tiny.assign(rain_pct_normal=100.0, ndvi_anomaly=0.0), cfg.alerts)["flag_flood"].any()


def test_describe_states_each_rule(cfg):
    rules = describe(cfg.alerts)
    assert rules["flag_rain_low"] == "rainfall below 75% of normal"
    assert rules["flag_ndvi_low"] == "NDVI anomaly below -1 standard deviations"
    assert rules["flag_flood"] == (
        "new water above the county's own 80th percentile for the month and at least 10 km²"
    )


# --- the county-month table and the national summary -------------------------------------


@pytest.fixture(scope="module")
def county_monthly(counties, raw, cfg):
    return build_county_monthly(counties, raw["rainfall"], raw["ndvi"], raw["flood"], cfg)


def row(table, code, month):
    return table[(table["county_code"] == code) & (table["month"] == month)].iloc[0]


def test_county_monthly_has_every_county_month(county_monthly, counties):
    assert len(county_monthly) == len(counties) * 84
    assert list(county_monthly.columns) == COUNTY_MONTHLY_COLUMNS
    assert not county_monthly.duplicated(["county_code", "month"]).any()


def test_county_monthly_flags_the_invented_events(county_monthly):
    assert row(county_monthly, "SS9901", "2021-06")["flag_rain_low"]
    assert row(county_monthly, "SS9901", "2021-06")["rain_pct_normal"] == pytest.approx(50, abs=3)
    assert row(county_monthly, "SS9801", "2022-01")["flag_ndvi_low"]
    assert row(county_monthly, "SS9902", "2020-10")["flag_flood"]
    assert row(county_monthly, "SS9902", "2020-10")["flooded_pct"] == pytest.approx(40)
    assert not row(county_monthly, "SS9902", "2020-09")["flag_flood"]


def test_county_monthly_keeps_months_without_data(county_monthly):
    unseen = row(county_monthly, "SS9901", "2019-07")  # observed area 0
    missing = row(county_monthly, "SS9802", "2023-03")  # no flood row at all
    for r in (unseen, missing):
        assert np.isnan(r["flooded_pct"]) and pd.isna(r["flag_flood"])
    assert not np.isnan(missing["rain_mm"])  # the other indicators are still there


def test_county_monthly_rejects_unknown_counties(counties, raw, cfg):
    rain = pd.concat(
        [raw["rainfall"], pd.DataFrame([{"county_code": "SS0000", "month": "2019-01", "rain_mm": 1.0}])]
    )
    with pytest.raises(ValueError, match="SS0000"):
        build_county_monthly(counties, rain, raw["ndvi"], raw["flood"], cfg)


def test_national_rainfall_is_area_weighted():
    table = pd.DataFrame(
        {
            "county_code": ["A", "B"],
            "month": ["2020-07", "2020-07"],
            "area_km2": [100.0, 300.0],
            "rain_mm": [100.0, 20.0],
            "rain_normal_mm": [100.0, 100.0],
            "ndvi_anomaly": [0.0, -2.0],
            "flooded_km2": [10.0, np.nan],
            "persistent_km2": [4.0, np.nan],
            "new_km2": [6.0, np.nan],
            "flag_rain_low": pd.array([False, True], dtype="boolean"),
            "flag_ndvi_low": pd.array([False, True], dtype="boolean"),
            "flag_flood": pd.array([False, pd.NA], dtype="boolean"),
        }
    )
    out = national_monthly(table, min_normal_mm=20).iloc[0]
    assert out["rain_mm"] == 40.0  # (100*100 + 20*300) / 400
    assert out["rain_pct_normal"] == 40.0
    assert out["ndvi_anomaly"] == -1.5
    assert out["flooded_km2"] == 10.0 and out["flooded_pct"] == 2.5
    assert (out["persistent_km2"], out["new_km2"]) == (4.0, 6.0)
    assert (out["counties_flag_rain_low"], out["counties_flag_flood"]) == (1, 0)


def test_national_summary_and_month_picks(county_monthly):
    national = national_monthly(county_monthly, min_normal_mm=20)
    assert len(national) == 84
    assert np.isnan(national.set_index("month").loc["2019-01", "rain_pct_normal"])  # dry month
    assert peak_flood_month(national) == "2020-10"
    assert worst_vegetation_month(national) == "2022-01"


def test_month_picks_without_data():
    empty = pd.DataFrame(
        {
            "month": ["2020-01"],
            "flooded_km2": [np.nan],
            "new_km2": [np.nan],
            "ndvi_anomaly": [np.nan],
            "counties_flag_ndvi_low": [0],
        }
    )
    assert peak_flood_month(empty) is None
    assert worst_vegetation_month(empty) is None


def test_worst_vegetation_month_breaks_ties_on_the_lowest_anomaly():
    national = pd.DataFrame(
        {"month": ["2020-01", "2020-02"], "ndvi_anomaly": [-0.5, -0.9], "counties_flag_ndvi_low": [3, 3]}
    )
    assert worst_vegetation_month(national) == "2020-02"


def _agreement_row(jrc, **layers):
    row = {"jrc": jrc}
    for name in ("flooded", "radar_dark", "radar_change", "optical"):
        flag, both = layers.get(name, (0.0, 0.0))
        row |= {f"{name}_flag": flag, f"{name}_both": both}
    return row


def test_agreement_is_pooled_by_area():
    rows = [
        _agreement_row(10.0, flooded=(8.0, 6.0), radar_dark=(4.0, 4.0), optical=(6.0, 3.0)),
        _agreement_row(30.0, flooded=(12.0, 10.0), radar_change=(5.0, 1.0), optical=(12.0, 10.0)),
    ]
    out = summarise_agreement(rows).set_index("layer")
    combined = out.loc["flood layer (radar or optical)"]
    assert combined["recall"] == 0.4  # (6 + 10) / (10 + 30)
    assert combined["precision"] == 0.8  # (6 + 10) / (8 + 12)
    assert out.loc["radar, darkness rule (used)", "precision"] == 1.0
    assert out.loc["radar, dry-season change rule (dropped)", "recall"] == 0.02  # 1 / 40, rounded
    assert out.loc["optical, MNDWI", "flagged_km2"] == 18.0


def test_agreement_without_flags_has_no_precision():
    out = summarise_agreement([_agreement_row(5.0)])
    assert out["precision"].isna().all() and (out["recall"] == 0).all()
