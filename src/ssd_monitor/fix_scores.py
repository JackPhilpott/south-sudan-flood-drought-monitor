"""Scores and checks for the radar-fix test, from the tables local_fixes.py writes. No Earth Engine here.

Agreement with JRC monthly water uses only pixels JRC observed that month. Recall is the share of
JRC's water that a rule flags; precision is the share of the rule's flagged area (where JRC observed)
that JRC calls water. These measure agreement with JRC's Landsat water maps, not accuracy: JRC has
its own errors and misses water under vegetation, and its monthly maps have gaps under cloud.
"""

import pandas as pd

from ssd_monitor.pixel_classes import decode

SEASONS = ("all", "dry", "wet")


def season_of(months: pd.Series, wet_months: tuple[int, ...]) -> pd.Series:
    """ "wet" for the wet-season months (by calendar month), "dry" for the rest."""
    return months.str[5:7].astype(int).isin(wet_months).map({True: "wet", False: "dry"})


def _by_season(
    frame: pd.DataFrame, keys: list[str], values: list[str], wet_months: tuple[int, ...]
) -> pd.DataFrame:
    """Sums by keys and season, with an extra "all" season."""
    frame = frame.assign(season=season_of(frame["month"], wet_months))
    seasonal = frame.groupby([*keys, "season"], as_index=False)[values].sum()
    if keys:
        overall = frame.groupby(keys, as_index=False)[values].sum()
    else:
        overall = frame[values].sum().to_frame().T
    return pd.concat([overall.assign(season="all"), seasonal], ignore_index=True)


def score_rules(rules: pd.DataFrame, counties: pd.DataFrame, wet_months: tuple[int, ...]) -> pd.DataFrame:
    """Recall and precision against JRC per rule and season, with the area each rule removes.

    removed_km2 is the flagged area (all pixels, observed by JRC or not) that the rule removes
    compared with the current layer, including patches that the filter then drops.
    """
    jrc_months = counties.dropna(subset=["jrc_water_km2"])["month"].unique()
    rules = rules[rules["month"].isin(jrc_months)]
    values = ["flagged_km2", "flagged_jrc_observed_km2", "flagged_jrc_water_km2", "radar_only_km2"]
    scores = _by_season(rules, ["rule"], values, wet_months)
    jrc = _by_season(
        counties[counties["month"].isin(jrc_months)], [], ["jrc_water_km2", "jrc_observed_km2"], wet_months
    )
    scores = scores.merge(jrc, on="season")
    current = scores[scores["rule"] == "current"].set_index("season")["flagged_km2"]
    scores["removed_km2"] = scores["season"].map(current) - scores["flagged_km2"]
    scores["recall"] = scores["flagged_jrc_water_km2"] / scores["jrc_water_km2"]
    scores["precision"] = scores["flagged_jrc_water_km2"] / scores["flagged_jrc_observed_km2"]
    order = {s: i for i, s in enumerate(SEASONS)}
    return scores.sort_values(
        ["rule", "season"], key=lambda c: c.map(order) if c.name == "season" else c
    ).reset_index(drop=True)


def false_water(
    rules: pd.DataFrame, counties: pd.DataFrame, codes: list[str], months: list[str]
) -> pd.DataFrame:
    """Per county, month and rule: radar-only km2 left, and flagged km2 that JRC saw as dry or as water."""
    part = rules[rules["county_code"].isin(codes) & rules["month"].isin(months)].copy()
    part["jrc_dry_flagged_km2"] = part["flagged_jrc_observed_km2"] - part["flagged_jrc_water_km2"]
    info = counties[["county_code", "month", "assessed_km2", "jrc_observed_km2"]]
    part = part.merge(info, on=["county_code", "month"], how="left")
    part["jrc_observed_pct"] = 100 * part["jrc_observed_km2"] / part["assessed_km2"]
    columns = [
        "county_code",
        "month",
        "rule",
        "radar_only_km2",
        "flagged_km2",
        "jrc_dry_flagged_km2",
        "flagged_jrc_water_km2",
        "jrc_observed_pct",
    ]
    return part[columns].reset_index(drop=True)


def false_positive_causes(counties: pd.DataFrame, wet_months: tuple[int, ...]) -> pd.DataFrame:
    """Where each sensor's stored flags disagree with JRC (JRC saw the pixel, no water), by likely cause.

    next_to_jrc_water: JRC water within one pixel (edges, and 30 m against 100 m pixels);
    jrc_water_other_month: JRC saw water there at another time that year (timing);
    other: neither (clouds or shadows, wet soil, dark dry land, or water JRC missed).
    """
    counties = counties.dropna(subset=["jrc_water_km2"])
    causes = ("next_to_jrc_water", "jrc_water_other_month", "other")
    rows = []
    for layer in ("optical", "radar_only", "both_sensors"):
        values = [f"{layer}_jrc_observed_km2", *[f"{layer}_jrc_dry_{c}_km2" for c in causes]]
        sums = _by_season(counties, [], values, wet_months)
        for _, row in sums.iterrows():
            dry = sum(row[f"{layer}_jrc_dry_{c}_km2"] for c in causes)
            flagged = row[f"{layer}_jrc_observed_km2"]
            out = {
                "layer": layer,
                "season": row["season"],
                "flagged_jrc_observed_km2": flagged,
                "jrc_dry_km2": dry,
            }
            out["precision"] = 1 - dry / flagged if flagged else float("nan")
            for c in causes:
                out[f"{c}_km2"] = row[f"{layer}_jrc_dry_{c}_km2"]
                out[f"{c}_share"] = row[f"{layer}_jrc_dry_{c}_km2"] / dry if dry else float("nan")
            rows.append(out)
    return pd.DataFrame(rows)


# --- Reproduction check: does the local route give what Earth Engine gives? ----------------------


def compare_stored_sums(counties: pd.DataFrame, flood_raw: pd.DataFrame, month: str) -> pd.DataFrame:
    """Local county sums of the stored flags against the Earth Engine county table (outputs/raw/flood.csv)."""
    pairs = {
        "flooded_km2": "stored_flooded_km2",
        "flooded_sar_km2": "stored_sar_km2",
        "flooded_optical_km2": "stored_optical_km2",
        "observed_km2": "stored_observed_km2",
    }
    ee = flood_raw[flood_raw["month"] == month].set_index("county_code")
    local = counties[counties["month"] == month].set_index("county_code")
    rows = []
    for ee_col, local_col in pairs.items():
        diff = local[local_col] - ee[ee_col]
        rows.append(
            {
                "quantity": ee_col,
                "earth_engine_km2": ee[ee_col].sum(),
                "local_km2": local[local_col].sum(),
                "difference_pct": 100 * (local[local_col].sum() / ee[ee_col].sum() - 1),
                "largest_county_difference_km2": diff.abs().max(),
            }
        )
    return pd.DataFrame(rows)


def compare_classes(local: pd.DataFrame, ee: pd.DataFrame, bits: dict[str, int]) -> pd.DataFrame:
    """km2 of each pixel class nationally, Earth Engine against local (both decoded, excluded pixels out)."""
    a, b = decode(ee, bits), decode(local, bits)
    rows = []
    for name in bits:
        if name == "excluded":
            continue
        e, loc = a["km2"][a[name]].sum(), b["km2"][b[name]].sum()
        rows.append(
            {
                "class": name,
                "earth_engine_km2": e,
                "local_km2": loc,
                "difference_pct": 100 * (loc / e - 1) if e else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def radar_only_by_condition(
    classes: pd.DataFrame, bits: dict[str, int], codes: list[str], conditions: dict[str, tuple[str, ...]]
) -> pd.DataFrame:
    """Radar-only km2 per county left after each single condition, before the patch filter (from codes)."""
    d = decode(classes, bits)
    d = d[d["county_code"].isin(codes)]
    radar_only = d["sar"] & ~d["optical"]
    out = {}
    for name, cond in conditions.items():
        drop = d[list(cond)].any(axis=1) if cond else pd.Series(False, index=d.index)
        out[name] = d["km2"].where(radar_only & ~drop, 0.0).groupby(d["county_code"]).sum()
    return pd.DataFrame(out).T
