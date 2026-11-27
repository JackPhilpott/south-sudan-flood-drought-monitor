"""Flood flags on new water, and their check against OCHA's flood-affected counties. No Earth Engine here.

The first run's flag (more than 10% of a county under open water) fired almost only in Unity, where
water that never drained counts as flooded every month. The method update bases the flag on new
water (rebuild.py) and tests two county-relative rules, with thresholds fixed before any comparison:

- R1, own history: new water above the county's own 80th percentile for that calendar month (over
  2019-2025) and at least 10 km2. It fires in roughly the wettest one or two years per county.
- R2, low-lying land: new water above 5% of the county's land within 5 m of the nearest drainage
  (MERIT Hydro HAND), the land that can flood.

Each rule is checked per county and year: flagged if it fires in any July-December month, against
OCHA's list of counties with people affected by floods that year (ocha.py). A random flag that fires
for the same number of counties would hit flagged x listed / counties of them on average; that is the
chance baseline. 2019's list is partial (IOM DTM displacement sites only), so 2019 counts hits and
misses but not false alarms.
"""

import numpy as np
import pandas as pd

FLAG_MONTHS = (7, 8, 9, 10, 11, 12)
RULES = {
    "R1": "new water above the county's own 80th percentile for the month (2019-2025) and at least 10 km²",
    "R2": "new water above 5% of the county's low-lying land (HAND 5 m or less)",
    "old": "open water (all) above 10% of the county",
}


def percentile_flag(df: pd.DataFrame, percentile: float, min_km2: float) -> pd.Series:
    """R1: new water above the county's own percentile for the calendar month, and at least min_km2."""
    calendar = df["month"].str[5:7]
    threshold = df.groupby([df["county_code"], calendar])["new_km2"].transform(
        lambda s: np.nanpercentile(s, percentile) if s.notna().any() else np.nan
    )
    flag = (df["new_km2"] > threshold) & (df["new_km2"] >= min_km2)
    return flag.where(df["new_km2"].notna())


def low_lying_flag(df: pd.DataFrame, share_pct: float) -> pd.Series:
    """R2: new water above share_pct of the county's low-lying land (column low_lying_km2)."""
    flag = df["new_km2"] > share_pct / 100 * df["low_lying_km2"]
    return flag.where(df["new_km2"].notna())


def add_rule_flags(df: pd.DataFrame, percentile: float, min_km2: float, share_pct: float, old_pct: float):
    """Columns flag_R1, flag_R2 and flag_old (the first run's rule, on all open water)."""
    out = df.copy()
    out["flag_R1"] = percentile_flag(out, percentile, min_km2)
    out["flag_R2"] = low_lying_flag(out, share_pct)
    out["flag_old"] = (out["flooded_pct"] > old_pct).where(out["flooded_pct"].notna())
    return out


def county_years(df: pd.DataFrame, rules=("R1", "R2", "old")) -> pd.DataFrame:
    """One row per county and year: whether each rule fired in any July-December month."""
    part = df[df["month"].str[5:7].astype(int).isin(FLAG_MONTHS)].assign(
        year=lambda d: d["month"].str[:4].astype(int)
    )
    flags = {r: part[f"flag_{r}"].fillna(False).astype(bool) for r in rules}
    return (
        pd.DataFrame({"county_code": part["county_code"], "year": part["year"], **flags})
        .groupby(["county_code", "year"], as_index=False)
        .max()
    )


def score(
    years: pd.DataFrame, reference: pd.DataFrame, n_counties: int, rules=("R1", "R2", "old")
) -> pd.DataFrame:
    """Hits, misses, false alarms and chance hits per rule and year (reference: ocha.reference_table)."""
    rows = []
    for year, ref in reference.groupby("year"):
        listed = set(ref["county_code"])
        partial = bool(ref["partial"].any())
        flagged_year = years[years["year"] == year]
        for rule in rules:
            flagged = set(flagged_year.loc[flagged_year[rule], "county_code"])
            hits = len(flagged & listed)
            rows.append(
                {
                    "rule": rule,
                    "year": year,
                    "listed": len(listed),
                    "flagged": len(flagged),
                    "hits": hits,
                    "misses": len(listed) - hits,
                    "false_alarms": np.nan if partial else len(flagged - listed),
                    "chance_hits": len(flagged) * len(listed) / n_counties,
                    "partial": partial,
                }
            )
    return pd.DataFrame(rows)


def best_rule(scores: pd.DataFrame, candidates=("R1", "R2")) -> str:
    """The rule adopted for flag_flood, by a criterion fixed before the comparison: most hits above
    chance, summed over the reference years; fewer false alarms breaks a tie."""
    part = scores[scores["rule"].isin(candidates)]
    total = (
        part.assign(above=part["hits"] - part["chance_hits"]).groupby("rule")[["above", "false_alarms"]].sum()
    )
    return total.sort_values(["above", "false_alarms"], ascending=[False, True]).index[0]
