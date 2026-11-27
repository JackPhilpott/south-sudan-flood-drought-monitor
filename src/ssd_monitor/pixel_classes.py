"""Pixel classes and candidate rules for the radar-fix test. No Earth Engine here.

Each pixel of a month gets one integer code whose bits say which stored flood flags it has, what
JRC's Landsat water maps say there, and which candidate fix would remove it if it were radar-only
water (class_bits). Earth Engine builds the same code in radar_fixes.class_image; the local route
(local_fixes.py) builds it from the cache, and the two are compared to check the local route.

A rule is a set of conditions. It only removes radar-only pixels (flagged by radar, not by optical
imagery): a pixel stays flagged if optical imagery flags it, or if radar flags it and none of the
rule's conditions holds. local_fixes.py applies every rule and re-applies the small-patch filter.
"""

import json
from itertools import product

import pandas as pd

from ssd_monitor.config import RadarFixTestConfig

# Candidate conditions, by item: (a) burned, (b) high above drainage, (c) dark in the dry-season
# reference, (e) next to a Sentinel-1 scene outline. Each list starts with "no condition".
BURN_OPTIONS = {"": (), "a1": ("burn_recent",), "a2": ("burn_season",)}
REFERENCE_OPTIONS = {"": (), "c": ("dark_reference",)}
EDGE_OPTIONS = {"": (), "e": ("scene_edge",)}


def hand_bit(threshold_m: float) -> str:
    return f"hand_over_{threshold_m:g}"


def class_bits(rt: RadarFixTestConfig) -> dict[str, int]:
    """The bit of each pixel class in the code (the same order radar_fixes.py builds it in)."""
    names = [
        "sar",  # stored radar flag (after the patch filter)
        "optical",  # stored optical flag
        "observed",  # stored: at least one sensor saw the pixel
        "jrc_observed",  # JRC monthly: Landsat saw the pixel this month
        "jrc_water",  # JRC monthly: water this month
        "jrc_near",  # JRC water this month within jrc_near_m
        "jrc_year",  # JRC yearly: water at some time this year
        "burn_recent",  # (a1) MODIS burned area in the recent window
        "burn_season",  # (a2) MODIS burned area since the start of the burn season
        *[hand_bit(t) for t in rt.hand_thresholds_m],  # (b) HAND above each threshold
        "dark_reference",  # (c) dark in the dry-season radar reference
        "scene_edge",  # (e) within scene_edge_m of a Sentinel-1 scene outline this month
        "excluded",  # permanent water or steep slope: not assessed, dropped by decode()
    ]
    if len(names) > 15:
        raise ValueError("too many pixel classes for a 16-bit code")
    return {name: 1 << i for i, name in enumerate(names)}


def candidate_rules(rt: RadarFixTestConfig) -> dict[str, tuple[str, ...]]:
    """Every combination of the candidate conditions, by name ("current" removes nothing)."""
    hand = {"": ()} | {f"b{t:g}": (hand_bit(t),) for t in rt.hand_thresholds_m}
    rules = {}
    for parts in product(BURN_OPTIONS.items(), hand.items(), REFERENCE_OPTIONS.items(), EDGE_OPTIONS.items()):
        name = "+".join(label for label, _ in parts if label) or "current"
        rules[name] = tuple(bit for _, bits in parts for bit in bits)
    return rules


def classes_from_features(features: list[dict]) -> pd.DataFrame:
    """One row per county, month and pixel code with its km2, from the exported features."""
    rows = []
    for feature in features:
        props = feature["properties"]
        for group in json.loads(props["groups"]):
            rows.append((props["county_code"], props["month"], int(group["code"]), float(group["sum"])))
    return pd.DataFrame(rows, columns=["county_code", "month", "code", "km2"])


def decode(classes: pd.DataFrame, bits: dict[str, int]) -> pd.DataFrame:
    """Add one True/False column per pixel class, and drop the pixels that are not assessed.

    Permanent water and steep slopes are left out of every score, as in the flood layers.
    """
    out = classes.copy()
    for name, bit in bits.items():
        out[name] = (out["code"].astype(int) & bit) > 0
    return out[~out["excluded"]].reset_index(drop=True)
