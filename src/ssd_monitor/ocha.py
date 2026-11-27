"""Counties reported as flood-affected each year, from OCHA South Sudan's open data on HDX. No Earth Engine.

Used to check the flood flag (method update, item 3). All files are downloaded by script from HDX:

- 2021, 2022, 2024, 2025: "South Sudan: Flood Data" (OCHA South Sudan, CC0): reported numbers of
  flood-affected people by county. A county counts as affected when its number is above zero. 2022
  has two snapshots (October and November); a county counts if either reports it.
- 2019: "South Sudan: Flood Locations" (OCHA South Sudan, CC BY): IOM DTM's locations of people
  displaced by floods, end of October 2019. A county counts if it has at least one location. This is
  partial: displacement sites only, in 18 counties, while OCHA reported 32 flood-affected counties
  that season. Hits are counted for 2019, false alarms are not.
- 2020 and 2023: no county list. OCHA's final 2020 flooding report (1 July 2020 to 31 January
  2021) gives "49 flood-affected counties" and figures by state, but doesn't name the counties.
"""

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

HDX = "https://data.humdata.org/dataset"
FLOOD_DATA = f"{HDX}/70cefa0a-e4bd-481e-b773-20c2a462ea2e/resource"
FLOOD_LOCATIONS = f"{HDX}/a0a190d5-09af-49a8-87ba-83d91bfdab30/resource"

# County names in the 2021 file that are spelt differently in COD-AB.
NAME_ALIASES = {
    "Bor": "Bor South",
    "Jur": "Jur River",
    "Nasir": "Luakpiny/Nasir",
    "Panyijar": "Panyijiar",
    "Pigi / Canal": "Canal/Pigi",
}


@dataclass(frozen=True)
class Source:
    year: int
    as_of: str
    file: str
    url: str
    licence: str
    kind: str  # how to read it (see _read)
    partial: bool = False


SOURCES = (
    Source(
        2019,
        "2019-10-29",
        "ssd_dtm_flood_locations_20191029.xlsx",
        f"{FLOOD_LOCATIONS}/7c454f75-ea6e-4361-98ab-e12baa31cb20/download/20191029_dtm_floods_locations.xlsx",
        "CC BY 4.0",
        "dtm_locations",
        partial=True,
    ),
    Source(
        2021,
        "2021-12-13",
        "ssd_flood_affected_20211213.xlsx",
        f"{FLOOD_DATA}/1f91a97c-5c60-4375-8e5d-8967f764cbac/download/ss_floodsaffected_people_20211213.xlsx",
        "CC0",
        "names_2021",
    ),
    Source(
        2022,
        "2022-10-21",
        "ssd_flood_affected_20221021.xlsx",
        f"{FLOOD_DATA}/bfa47921-2a7b-43e4-b08d-fb4dfec8326e/download/ssd_flood_response_211022.xlsx",
        "CC0",
        "names_2022",
    ),
    Source(
        2022,
        "2022-11-30",
        "ssd_flood_affected_20221130.xlsx",
        f"{FLOOD_DATA}/918b0b51-30d3-4d46-b0c5-1688bdf27b8e/download/ssd_flood_response_301122.xlsx",
        "CC0",
        "pcodes_2022",
    ),
    Source(
        2024,
        "2024-12-20",
        "ssd_flood_affected_20241220.xlsx",
        f"{FLOOD_DATA}/eed79fad-e7d2-4890-881d-de8e0ec88a41/download/ssd_flood_response_20122024.xlsx",
        "CC0",
        "pcodes_2024",
    ),
    Source(
        2025,
        "2025-11-30",
        "ssd_flood_affected_20251130.xlsx",
        f"{FLOOD_DATA}/eb064156-f948-4520-8e2c-3b271badbd5c/download/ss_people_affected_and_displaced_by_floods_20251130.xlsx",
        "CC0",
        "pcodes_2025",
    ),
)


def download(raw_dir: Path) -> list[Path]:
    """Fetch every source file into raw_dir (skipping files already there)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for source in SOURCES:
        path = raw_dir / source.file
        if not path.exists():
            response = requests.get(
                source.url, timeout=120, headers={"User-Agent": "ssd-flood-drought-monitor"}
            )
            response.raise_for_status()
            path.write_bytes(response.content)
        paths.append(path)
    return paths


def _number(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values, errors="coerce").fillna(0)


def _read(source: Source, path: Path, names: dict[str, str]) -> pd.DataFrame:
    """county_code and value (people affected; people displaced for the 2019 DTM file) per row."""
    if source.kind == "dtm_locations":
        df = pd.read_excel(path, sheet_name=0)
        # The county P-code is wrong in a few rows (Nyirol locations coded as Fangak, SS0306);
        # the payam P-code is right, and its first six characters are the county's.
        codes = df["Payam_Pcode"].astype(str).str[:6].where(df["Payam_Pcode"].notna(), df["county_pcode"])
        return pd.DataFrame({"county_code": codes, "value": _number(df["idp_inds"])}).assign(present=True)
    if source.kind == "names_2021":
        df = pd.read_excel(path, sheet_name=0).iloc[:, :6]
        county, value = df["County"], df["Affected People"]
    elif source.kind == "names_2022":
        df = pd.read_excel(path, sheet_name=0, header=0).iloc[1:]  # row 1 holds HXL tags
        county, value = df.iloc[:, 1], df.iloc[:, 4]
    elif source.kind == "pcodes_2022":
        df = pd.read_excel(path, sheet_name=0, header=0).iloc[1:]
        return pd.DataFrame({"county_code": df.iloc[:, 3], "value": _number(df.iloc[:, 4])}).assign(
            present=False
        )
    elif source.kind == "pcodes_2024":
        df = pd.read_excel(path, sheet_name=0)
        return pd.DataFrame(
            {"county_code": df["Admin2_Pcode"], "value": _number(df["People_Affected"])}
        ).assign(present=False)
    elif source.kind == "pcodes_2025":
        df = pd.read_excel(path, sheet_name=0)
        return pd.DataFrame({"county_code": df["Admin2_PCODE"], "value": _number(df.iloc[:, 4])}).assign(
            present=False
        )
    else:
        raise ValueError(f"unknown source kind {source.kind!r}")
    county = county.astype(str).str.strip().replace(NAME_ALIASES)
    return pd.DataFrame({"county_code": county.map(names), "value": _number(value)}).assign(present=False)


def reference_table(raw_dir: Path, counties: pd.DataFrame) -> pd.DataFrame:
    """One row per year and reported county: county_code, county_name, people, sources, partial."""
    names = dict(zip(counties["county_name"], counties["county_code"], strict=True))
    known = set(counties["county_code"])
    frames = []
    for source in SOURCES:
        rows = _read(source, raw_dir / source.file, names)
        rows = rows[rows["county_code"].notna() & (rows["present"] | (rows["value"] > 0))]
        unknown = sorted(set(rows["county_code"]) - known)
        if unknown:
            raise ValueError(f"{source.file}: counties not in the boundaries: {unknown}")
        # Within a file, add up the rows of each county (the DTM file lists several locations per county).
        per_county = rows.groupby("county_code", as_index=False)["value"].sum()
        frames.append(
            per_county.assign(
                year=source.year,
                source=f"{source.file} ({source.licence}, as of {source.as_of})",
                partial=source.partial,
            )
        )
    table = pd.concat(frames, ignore_index=True)
    # Across snapshots of the same year (2022), keep the larger number.
    out = table.groupby(["year", "county_code"], as_index=False).agg(
        people=("value", "max"),
        sources=("source", lambda s: "; ".join(sorted(set(s)))),
        partial=("partial", "max"),
    )
    out = out.merge(counties[["county_code", "county_name", "state_name"]], on="county_code", how="left")
    return out[["year", "county_code", "county_name", "state_name", "people", "partial", "sources"]]
