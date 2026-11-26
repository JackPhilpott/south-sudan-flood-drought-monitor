"""Turning Earth Engine results into the county-month table. No Earth Engine here.

The raw tables (outputs/raw/) hold one row per county-month with the values Earth Engine computed.
This module adds the normals, anomalies and shares, joins the indicators and builds the national
monthly summary.
"""

import re

import numpy as np
import pandas as pd

from ssd_monitor.alerts import add_flags
from ssd_monitor.config import Config
from ssd_monitor.dates import months_between

KEYS = ["county_code", "month"]
RAW_COLUMNS = {
    "rainfall": ["rain_mm"],
    "ndvi": ["ndvi"],
    "flood": ["flooded_km2", "flooded_sar_km2", "flooded_optical_km2", "observed_km2"],
}
# Precision kept in the raw downloads: well beyond what the data supports, and small files.
RAW_ROUNDING = {
    "rain_mm": 2,
    "ndvi": 4,
    "flooded_km2": 3,
    "flooded_sar_km2": 3,
    "flooded_optical_km2": 3,
    "observed_km2": 3,
}
COUNTY_COLUMNS = ["county_code", "county_name", "state_code", "state_name", "area_km2"]
COUNTY_MONTHLY_COLUMNS = [
    *COUNTY_COLUMNS[:4],
    "month",
    "area_km2",
    "rain_mm",
    "rain_normal_mm",
    "rain_pct_normal",
    "ndvi",
    "ndvi_baseline_mean",
    "ndvi_baseline_sd",
    "ndvi_anomaly",
    "flooded_km2",
    "flooded_pct",
    "flooded_sar_km2",
    "flooded_optical_km2",
    "flood_observed_pct",
    "flag_rain_low",
    "flag_ndvi_low",
    "flag_flood",
]
ROUNDING = {
    "rain_mm": 1,
    "rain_normal_mm": 1,
    "rain_pct_normal": 1,
    "ndvi": 4,
    "ndvi_baseline_mean": 4,
    "ndvi_baseline_sd": 4,
    "ndvi_anomaly": 2,
    "flooded_km2": 2,
    "flooded_pct": 2,
    "flooded_sar_km2": 2,
    "flooded_optical_km2": 2,
    "flood_observed_pct": 1,
}
# A normal or baseline needs data in at least this share of its years, or it is left blank.
MIN_BASELINE_SHARE = 0.75
MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}")


# --- Raw Earth Engine results ------------------------------------------------------------


def tidy_features(features: list[dict], value_columns: list[str]) -> pd.DataFrame:
    """One row per county-month from Earth Engine features, sorted.

    Earth Engine leaves out a property when its value is null (for example a month with no valid
    pixels), so a missing property becomes NaN rather than an error. A feature without a county
    code or month, or two features for the same county-month, is an error.
    """
    rows = []
    for feature in features:
        props = feature.get("properties") or {}
        rows.append(
            {"county_code": props.get("county_code"), "month": props.get("month")}
            | {c: props.get(c) for c in value_columns}
        )
    df = pd.DataFrame(rows, columns=[*KEYS, *value_columns])
    if df[KEYS].isna().any().any():
        raise ValueError("a feature has no county_code or month")
    bad = df.loc[~df["month"].astype(str).str.match(MONTH_PATTERN), "month"]
    if not bad.empty:
        raise ValueError(f"months must look like YYYY-MM, got {bad.iloc[0]!r}")
    df["month"] = df["month"].astype(str).str[:7]
    for col in value_columns:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    duplicated = df.duplicated(KEYS)
    if duplicated.any():
        first = df.loc[duplicated].iloc[0]
        raise ValueError(f"more than one row for {first['county_code']} {first['month']}")
    return df.sort_values(KEYS).reset_index(drop=True)


# --- Normals and anomalies ---------------------------------------------------------------


def climatology(df: pd.DataFrame, value: str, years: tuple[int, int]) -> pd.DataFrame:
    """Mean and standard deviation of `value` per county and calendar month over `years`.

    Left blank (NaN) where fewer than MIN_BASELINE_SHARE of the years have a value.
    """
    year = df["month"].str[:4].astype(int)
    base = df.loc[year.between(*years)].assign(calendar_month=lambda d: d["month"].str[5:7])
    stats = base.groupby(["county_code", "calendar_month"])[value].agg(["mean", "std", "count"])
    enough = stats["count"] >= MIN_BASELINE_SHARE * (years[1] - years[0] + 1)
    return stats[["mean", "std"]].where(enough, np.nan)


def _with_climatology(df: pd.DataFrame, value: str, years: tuple[int, int]) -> pd.DataFrame:
    clim = climatology(df, value, years)
    keyed = df.assign(calendar_month=df["month"].str[5:7])
    joined = keyed.join(clim, on=["county_code", "calendar_month"])
    return joined.drop(columns="calendar_month")


def add_percent_of_normal(rain: pd.DataFrame, years: tuple[int, int], min_normal_mm: float) -> pd.DataFrame:
    """Add rain_normal_mm (mean total for the calendar month over `years`) and rain_pct_normal.

    The % is left blank when the normal is below min_normal_mm: in the dry season a few mm against
    a normal of almost nothing gives huge, meaningless percentages.
    """
    out = (
        _with_climatology(rain, "rain_mm", years)
        .rename(columns={"mean": "rain_normal_mm"})
        .drop(columns="std")
    )
    pct = 100 * out["rain_mm"] / out["rain_normal_mm"]
    out["rain_pct_normal"] = pct.where(out["rain_normal_mm"] >= min_normal_mm)
    return out


def add_standardised_anomaly(ndvi: pd.DataFrame, years: tuple[int, int]) -> pd.DataFrame:
    """Add the baseline mean and standard deviation for the calendar month, and the anomaly in SDs."""
    out = _with_climatology(ndvi, "ndvi", years).rename(
        columns={"mean": "ndvi_baseline_mean", "std": "ndvi_baseline_sd"}
    )
    sd = out["ndvi_baseline_sd"].where(out["ndvi_baseline_sd"] > 0)
    out["ndvi_anomaly"] = (out["ndvi"] - out["ndvi_baseline_mean"]) / sd
    return out


def add_flood_shares(flood: pd.DataFrame, areas: pd.DataFrame) -> pd.DataFrame:
    """Add flooded_pct and flood_observed_pct, as % of each county's area.

    When neither sensor saw any of the county that month, the flooded areas are unknown, not zero,
    so they are blanked.
    """
    out = flood.merge(
        areas[["county_code", "area_km2"]], on="county_code", how="left", validate="many_to_one"
    )
    seen = out["observed_km2"] > 0
    for col in ("flooded_km2", "flooded_sar_km2", "flooded_optical_km2"):
        out[col] = out[col].where(seen)
    out["flooded_pct"] = 100 * out["flooded_km2"] / out["area_km2"]
    out["flood_observed_pct"] = 100 * out["observed_km2"] / out["area_km2"]
    return out.drop(columns="area_km2")


# --- The county-month table --------------------------------------------------------------


def _check_counties(raw: pd.DataFrame, counties: pd.DataFrame, name: str) -> None:
    unknown = sorted(set(raw["county_code"]) - set(counties["county_code"]))
    if unknown:
        raise ValueError(f"{name} has county codes that are not in the boundaries: {unknown[:5]}")


def build_county_monthly(
    counties: pd.DataFrame, rain: pd.DataFrame, ndvi: pd.DataFrame, flood: pd.DataFrame, cfg: Config
) -> pd.DataFrame:
    """One row per county and month of the monitoring period, with every indicator and flag.

    Every county-month appears, even when an indicator has no value for it, so gaps stay visible.
    """
    for name, raw in (("rainfall", rain), ("ndvi", ndvi), ("flood", flood)):
        _check_counties(raw, counties, name)

    months = pd.DataFrame({"month": months_between(cfg.start_month, cfg.end_month)})
    table = counties[COUNTY_COLUMNS].merge(months, how="cross")

    rain = add_percent_of_normal(rain, cfg.rainfall.normal_years, cfg.rainfall.min_normal_mm)
    ndvi = add_standardised_anomaly(ndvi, cfg.vegetation.baseline_years)
    flood = add_flood_shares(flood, counties).drop(columns="observed_km2")
    for indicator in (rain, ndvi, flood):
        table = table.merge(indicator, on=KEYS, how="left", validate="one_to_one")

    table = add_flags(table, cfg.alerts)
    table = table.round(ROUNDING)
    return table[COUNTY_MONTHLY_COLUMNS].sort_values(KEYS).reset_index(drop=True)


# --- National summary --------------------------------------------------------------------


def _area_mean(month: pd.DataFrame, value: str) -> float:
    valid = month[value].notna()
    area = month.loc[valid, "area_km2"]
    return float((month.loc[valid, value] * area).sum() / area.sum()) if valid.any() else np.nan


def national_monthly(county_monthly: pd.DataFrame, min_normal_mm: float) -> pd.DataFrame:
    """One row per month for the whole country.

    Rainfall and its normal are area-weighted means of the counties; the % of normal follows the same
    dry-month rule as the counties. Flooded area is the sum over the counties that were observed.
    """
    national_area = county_monthly.drop_duplicates("county_code")["area_km2"].sum()
    rows = []
    for month, m in county_monthly.groupby("month", sort=True):
        both = m.dropna(subset=["rain_mm", "rain_normal_mm"])
        rain, normal = _area_mean(both, "rain_mm"), _area_mean(both, "rain_normal_mm")
        flooded = m["flooded_km2"].sum(min_count=1)
        rows.append(
            {
                "month": month,
                "rain_mm": rain,
                "rain_normal_mm": normal,
                "rain_pct_normal": 100 * rain / normal if normal >= min_normal_mm else np.nan,
                "ndvi_anomaly": _area_mean(m, "ndvi_anomaly"),
                "flooded_km2": flooded,
                "flooded_pct": 100 * flooded / national_area,
                "counties_flag_rain_low": int(m["flag_rain_low"].fillna(False).sum()),
                "counties_flag_ndvi_low": int(m["flag_ndvi_low"].fillna(False).sum()),
                "counties_flag_flood": int(m["flag_flood"].fillna(False).sum()),
            }
        )
    return pd.DataFrame(rows).round(
        {
            "rain_mm": 1,
            "rain_normal_mm": 1,
            "rain_pct_normal": 1,
            "ndvi_anomaly": 2,
            "flooded_km2": 1,
            "flooded_pct": 2,
        }
    )


def peak_flood_month(national: pd.DataFrame) -> str | None:
    """The month with the largest flooded area, or None if there is no flood data."""
    flooded = national.set_index("month")["flooded_km2"].dropna()
    return None if flooded.empty else str(flooded.idxmax())


def worst_vegetation_month(national: pd.DataFrame) -> str | None:
    """The month with the most counties flagged for low NDVI; ties go to the lowest national anomaly."""
    ranked = national.dropna(subset=["ndvi_anomaly"]).sort_values(
        ["counties_flag_ndvi_low", "ndvi_anomaly"], ascending=[False, True]
    )
    return None if ranked.empty else str(ranked["month"].iloc[0])


# --- Agreement with JRC monthly water ----------------------------------------------------

AGREEMENT_LAYERS = {
    "flooded": "flood layer (radar or optical)",
    "radar_dark": "radar, darkness rule (used)",
    "radar_change": "radar, dry-season change rule (dropped)",
    "optical": "optical, MNDWI",
}


def summarise_agreement(rows: list[dict]) -> pd.DataFrame:
    """Recall and precision of each flood layer against JRC water, pooled over all rows by area.

    recall: share of JRC's water that the layer also flags.
    precision: share of the layer's flagged area that JRC also calls water.
    """
    df = pd.DataFrame(rows)
    jrc = df["jrc"].sum()
    out = []
    for layer, label in AGREEMENT_LAYERS.items():
        flagged, both = df[f"{layer}_flag"].sum(), df[f"{layer}_both"].sum()
        out.append(
            {
                "layer": label,
                "flagged_km2": flagged,
                "jrc_water_km2": jrc,
                "recall": both / jrc if jrc else np.nan,
                "precision": both / flagged if flagged else np.nan,
            }
        )
    return pd.DataFrame(out).round({"flagged_km2": 1, "jrc_water_km2": 1, "recall": 2, "precision": 2})
