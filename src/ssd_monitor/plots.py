"""The four figures, drawn from the cached outputs. No Earth Engine here.

Each function returns a matplotlib Figure; save() writes it to a PNG.
"""

from pathlib import Path

import geopandas as gpd
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from matplotlib import patheffects
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.figure import Figure
from matplotlib.patches import Patch
from matplotlib.ticker import StrMethodFormatter

from ssd_monitor.rasters import FrequencyRaster

INK, SECONDARY, MUTED = "#0b0b0b", "#52514e", "#898781"
SURFACE, LAND, GRID, BASELINE = "#fcfcfb", "#f0efec", "#e1e0d9", "#c3c2b7"
SERIES = "#2a78d6"
# One hue, light to dark, for magnitude (flood frequency and flooded share).
BLUES = ["#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#184f95", "#0d366b"]
# Brown (drier than usual) to teal (greener than usual) with a neutral grey midpoint. Brown-teal
# rather than brown-green so the two sides stay distinct with red-green colour blindness.
BROWN_TEAL = ["#8c510a", "#bf812d", "#dfc27d", "#f0efec", "#80cdc1", "#35978f", "#01665e"]

FREQUENCY_BINS = [0.5, 3, 6, 12, 24, np.inf]
FREQUENCY_LABELS = ["0.5-3", "3-6", "6-12", "12-24", "24 or more"]
FLOODED_PCT_BINS = [0, 1, 5, 10, 25, 50, 100.001]
FLOODED_PCT_LABELS = ["under 1%", "1-5%", "5-10%", "10-25%", "25-50%", "50% or more"]
ANOMALY_BINS = [-np.inf, -2, -1, -0.5, 0.5, 1, 2, np.inf]
ANOMALY_LABELS = ["below -2", "-2 to -1", "-1 to -0.5", "-0.5 to 0.5", "0.5 to 1", "1 to 2", "above 2"]

BOUNDARY_NOTE = (
    "Boundaries: OCHA COD-AB (CC BY-IGO), simplified; they do not imply official endorsement or "
    "acceptance by the United Nations. Final status of the Abyei area is not yet determined."
)


def month_label(month: str) -> str:
    return pd.Period(month, freq="M").strftime("%B %Y")


def _figure(width: float, height: float, layout: str | None = None) -> Figure:
    return Figure(figsize=(width, height), facecolor=SURFACE, layout=layout)


def _titles(fig: Figure, title: str, subtitle: str, note: str) -> None:
    fig.suptitle(title, x=0.02, y=0.975, ha="left", va="top", color=INK, fontsize=13, fontweight="bold")
    fig.text(0.02, 0.925, subtitle, ha="left", va="top", color=SECONDARY, fontsize=9.5)
    fig.text(0.02, 0.012, note, ha="left", va="bottom", color=MUTED, fontsize=7.5, wrap=True)


def _map_axes(fig: Figure, counties: gpd.GeoDataFrame):
    ax = fig.add_axes([0.01, 0.08, 0.72, 0.8])
    ax.set_axis_off()
    minx, miny, maxx, maxy = counties.total_bounds
    pad = 0.15
    ax.set_xlim(minx - pad, maxx + pad)
    ax.set_ylim(miny - pad, maxy + pad)
    ax.set_aspect(1 / np.cos(np.radians((miny + maxy) / 2)))
    return ax


def _outlines(ax, counties: gpd.GeoDataFrame, county_colour: str = "#ffffff") -> None:
    counties.boundary.plot(ax=ax, color=county_colour, linewidth=0.35, aspect=None)
    states = counties.dissolve(by="state_code")
    states.boundary.plot(ax=ax, color=SECONDARY, linewidth=0.6, aspect=None)


def _legend(ax, handles: list, title: str) -> None:
    legend = ax.legend(
        handles=handles,
        title=title,
        loc="upper left",
        bbox_to_anchor=(1.0, 0.9),
        frameon=False,
        fontsize=8.5,
        title_fontsize=9,
        alignment="left",
    )
    legend.get_title().set_color(INK)
    for text in legend.get_texts():
        text.set_color(SECONDARY)


def _label_counties(ax, counties: gpd.GeoDataFrame, codes: list[str]) -> None:
    """Name a few counties on the map: selective labels, not one on every polygon."""
    halo = [patheffects.withStroke(linewidth=2.5, foreground="#ffffff")]
    for _, county in counties[counties["county_code"].isin(codes)].iterrows():
        point = county.geometry.representative_point()
        ax.annotate(
            county["county_name"],
            (point.x, point.y),
            ha="center",
            va="center",
            fontsize=7.5,
            color=INK,
            path_effects=halo,
        )


def _binned(values: pd.Series, bins: list[float], colours: list[str]) -> pd.Series:
    """The colour of each value's bin, or None for missing values."""
    index = pd.cut(values, bins=bins, labels=False, right=False)
    return index.map(lambda i: colours[int(i)] if pd.notna(i) else None)


# --- 1. Flood frequency map --------------------------------------------------------------


def plot_flood_frequency(
    raster: FrequencyRaster, counties: gpd.GeoDataFrame, period: str, analysis_scale_m: float
) -> Figure:
    """Mean number of flooded months per cell, with permanent water and steep slopes greyed out."""
    cell = raster.grid.cell
    fig = _figure(9, 6.4)
    ax = _map_axes(fig, counties)
    counties.plot(ax=ax, color=LAND, linewidth=0, aspect=None)

    extent = raster.grid.extent
    excluded = np.where(raster.excluded >= 0.5, 1.0, np.nan)
    ax.imshow(excluded, extent=extent, cmap=ListedColormap([BASELINE]), interpolation="nearest", zorder=2)
    months = np.where((raster.months >= FREQUENCY_BINS[0]) & ~(raster.excluded >= 0.5), raster.months, np.nan)
    colours = BLUES[1:]
    norm = BoundaryNorm(FREQUENCY_BINS, ncolors=len(colours))
    ax.imshow(
        months, extent=extent, cmap=ListedColormap(colours), norm=norm, interpolation="nearest", zorder=3
    )
    ax.set_aspect(1 / np.cos(np.radians((extent[2] + extent[3]) / 2)))
    _outlines(ax, counties)

    handles = [Patch(facecolor=c, label=lab) for c, lab in zip(colours, FREQUENCY_LABELS, strict=True)]
    handles += [
        Patch(facecolor=LAND, edgecolor=BASELINE, label="under 0.5"),
        Patch(facecolor=BASELINE, label="Permanent water or\nsteep slope (not assessed)"),
    ]
    _legend(ax, handles, "Months flooded")
    _titles(
        fig,
        f"Flood frequency, {period}",
        "Number of months each area was flooded, from Sentinel-1 radar and Sentinel-2 optical imagery",
        f"Mean over each {cell:g}° (about {cell * 111_320:,.0f} m) cell of a {analysis_scale_m:g} m "
        "analysis. Contains modified Copernicus Sentinel data. "
        "Permanent water: EC JRC/Google Global Surface Water. Slope: HydroSHEDS. " + BOUNDARY_NOTE,
    )
    return fig


# --- 2. National monthly chart -----------------------------------------------------------


def plot_national(national: pd.DataFrame, rain_threshold_pct: float, min_normal_mm: float) -> Figure:
    """Two panels sharing the time axis: rainfall % of normal (bars) and flooded area (line)."""
    fig = _figure(10, 6.2, layout="constrained")
    top, bottom = fig.subplots(2, 1, sharex=True)
    dates = pd.PeriodIndex(national["month"], freq="M").to_timestamp(how="start") + pd.Timedelta(days=14)

    top.bar(dates, national["rain_pct_normal"], width=24, color=SERIES, zorder=3)
    top.axhline(100, color=BASELINE, linewidth=1, zorder=2)
    top.axhline(rain_threshold_pct, color=SECONDARY, linewidth=1, linestyle=(0, (4, 3)), zorder=4)
    # Name the two reference lines in the right margin, clear of the bars.
    for y, text in ((100, "normal"), (rain_threshold_pct, "county\nalert")):
        top.annotate(
            text,
            (1, y),
            xycoords=("axes fraction", "data"),
            xytext=(6, 0),
            textcoords="offset points",
            va="center",
            fontsize=8,
            color=SECONDARY,
        )
    top.set_ylabel("Rainfall, % of 1991-2020 normal", color=SECONDARY, fontsize=9)

    bottom.plot(dates, national["flooded_km2"], color=SERIES, linewidth=2, zorder=3)
    bottom.set_ylabel("Flooded area, km²", color=SECONDARY, fontsize=9)
    bottom.set_ylim(bottom=0)
    bottom.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    peak = national.loc[national["flooded_km2"].idxmax()] if national["flooded_km2"].notna().any() else None
    if peak is not None:
        peak_date = dates[national.index.get_loc(peak.name)]
        bottom.scatter(
            [peak_date], [peak["flooded_km2"]], s=40, color=SERIES, edgecolor=SURFACE, linewidth=2, zorder=4
        )
        bottom.annotate(
            f"{month_label(peak['month'])}: {peak['flooded_km2']:,.0f} km²",
            (peak_date, peak["flooded_km2"]),
            xytext=(8, 0),
            textcoords="offset points",
            va="center",
            fontsize=8.5,
            color=INK,
        )

    for ax in (top, bottom):
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(colors=MUTED, labelcolor=MUTED, length=0, labelsize=8.5)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(BASELINE)
    bottom.xaxis.set_major_locator(mdates.YearLocator())
    bottom.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    fig.get_layout_engine().set(rect=(0, 0.05, 1, 0.86))
    _titles(
        fig,
        "Rainfall and flooding across South Sudan, by month",
        "Rainfall as % of the 1991-2020 normal (all counties, area-weighted) and total flooded area",
        f"Blank rainfall months: dry months with a normal below {min_normal_mm:g} mm. Rainfall: CHIRPS. "
        "Flooded area: modified Copernicus Sentinel-1 and Sentinel-2 data, "
        "counties with observations that month.",
    )
    return fig


# --- 3 and 4. County maps for one month --------------------------------------------------


def _county_map(
    counties: gpd.GeoDataFrame,
    values: pd.DataFrame,
    column: str,
    bins: list[float],
    colours: list[str],
    labels: list[str],
    flagged: pd.Series,
    label_codes: list[str],
) -> tuple[Figure, object, list]:
    data = counties.merge(values[["county_code", column]], on="county_code", how="left")
    data["colour"] = _binned(data[column], bins, colours)
    missing = data["colour"].isna()

    fig = _figure(9, 6.4)
    ax = _map_axes(fig, counties)
    data[~missing].plot(ax=ax, color=data.loc[~missing, "colour"], linewidth=0, aspect=None)
    if missing.any():
        data[missing].plot(ax=ax, facecolor=LAND, edgecolor=BASELINE, hatch="////", linewidth=0, aspect=None)
    _outlines(ax, counties)
    flagged_codes = values.loc[flagged.fillna(False).to_numpy(), "county_code"]
    if not flagged_codes.empty:
        outlined = counties[counties["county_code"].isin(flagged_codes)]
        outlined.boundary.plot(ax=ax, color=INK, linewidth=0.9, aspect=None)
    _label_counties(ax, counties, label_codes)

    handles = [Patch(facecolor=c, label=lab) for c, lab in zip(colours, labels, strict=True)]
    if missing.any():
        handles.append(Patch(facecolor=LAND, edgecolor=BASELINE, hatch="////", label="No data"))
    return fig, ax, handles


def plot_county_flood(
    counties: gpd.GeoDataFrame, county_monthly: pd.DataFrame, month: str, threshold_pct: float
) -> Figure:
    """Share of each county flooded in one month, with flagged counties outlined."""
    values = county_monthly[county_monthly["month"] == month]
    top3 = values.dropna(subset=["flooded_pct"]).nlargest(3, "flooded_pct")["county_code"].tolist()
    fig, ax, handles = _county_map(
        counties,
        values,
        "flooded_pct",
        FLOODED_PCT_BINS,
        BLUES,
        FLOODED_PCT_LABELS,
        values["flag_flood"],
        top3,
    )
    handles.append(
        Patch(facecolor="none", edgecolor=INK, linewidth=0.9, label=f"Flagged: over {threshold_pct:g}%")
    )
    _legend(ax, handles, "Share of county flooded")
    _titles(
        fig,
        f"Flooded share of each county, {month_label(month)}",
        "The month with the largest flooded area in the series. Labels: the three most flooded counties.",
        "Flood extent from modified Copernicus Sentinel-1 and Sentinel-2 data; permanent water "
        "(EC JRC/Google) and steep slopes excluded. No data: no usable radar or optical observation "
        "that month. " + BOUNDARY_NOTE,
    )
    return fig


def plot_county_ndvi(
    counties: gpd.GeoDataFrame, county_monthly: pd.DataFrame, month: str, threshold_sd: float
) -> Figure:
    """NDVI anomaly of each county in one month, with flagged counties outlined."""
    values = county_monthly[county_monthly["month"] == month]
    lowest3 = values.dropna(subset=["ndvi_anomaly"]).nsmallest(3, "ndvi_anomaly")["county_code"].tolist()
    fig, ax, handles = _county_map(
        counties,
        values,
        "ndvi_anomaly",
        ANOMALY_BINS,
        BROWN_TEAL,
        ANOMALY_LABELS,
        values["flag_ndvi_low"],
        lowest3,
    )
    handles.append(
        Patch(facecolor="none", edgecolor=INK, linewidth=0.9, label=f"Flagged: below {threshold_sd:g}")
    )
    _legend(ax, handles, "NDVI anomaly (SD)")
    _titles(
        fig,
        f"Vegetation anomaly by county, {month_label(month)}",
        "NDVI against the 2001-2020 mean for the same month, in standard deviations. "
        "Labels: the three lowest counties.",
        "The month with the most counties flagged for low NDVI. NDVI: MODIS MOD13Q1 (NASA LP DAAC); water, "
        "built-up and bare land masked with ESA WorldCover 2021. " + BOUNDARY_NOTE,
    )
    return fig


def save(fig: Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, facecolor=SURFACE)
