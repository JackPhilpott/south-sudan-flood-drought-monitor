"""County (admin 2) boundaries from the OCHA COD-AB for South Sudan, on HDX.

download_cod_ab() fetches the original; make_counties() simplifies it into the small copy committed
at data/ssd_admin2_simplified.geojson. Everything else reads that copy.
"""

import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests

HDX_DATASET = "cod-ab-ssd"
HDX_API = "https://data.humdata.org/api/3/action/package_show"
EQUAL_AREA_CRS = "ESRI:102022"  # Africa Albers Equal Area Conic, for county areas
SIMPLIFY_TOLERANCE_DEG = 0.002  # about 220 m; shared borders stay shared (coverage simplification)
COORDINATE_DECIMALS = 5  # about 1 m

COLUMNS = {
    "adm2_pcode": "county_code",
    "adm2_name": "county_name",
    "adm1_pcode": "state_code",
    "adm1_name": "state_name",
}


def download_cod_ab(raw_dir: Path) -> Path:
    """Download the COD-AB GeoJSON archive from HDX into raw_dir and return its path."""
    meta = requests.get(HDX_API, params={"id": HDX_DATASET}, timeout=60)
    meta.raise_for_status()
    resources = meta.json()["result"]["resources"]
    url = next(r["url"] for r in resources if r["name"].lower().endswith(".geojson.zip"))
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / url.rsplit("/", 1)[-1]
    with requests.get(url, stream=True, timeout=300) as response:
        response.raise_for_status()
        with open(target, "wb") as f:
            for chunk in response.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return target


def read_admin2(archive: Path) -> gpd.GeoDataFrame:
    """Read the admin 2 layer from the COD-AB GeoJSON archive."""
    with zipfile.ZipFile(archive) as z:
        member = next(n for n in z.namelist() if n.endswith("admin2.geojson"))
    return gpd.read_file(f"zip://{archive}!{member}")


def make_counties(admin2: gpd.GeoDataFrame, tolerance: float = SIMPLIFY_TOLERANCE_DEG) -> gpd.GeoDataFrame:
    """Keep the codes and names, simplify the shapes and add each county's area in km2."""
    missing = set(COLUMNS) - set(admin2.columns)
    if missing:
        raise ValueError(f"admin 2 layer is missing columns {sorted(missing)}")
    counties = admin2[[*COLUMNS, "geometry"]].rename(columns=COLUMNS).to_crs("EPSG:4326")
    counties["geometry"] = counties.geometry.simplify_coverage(tolerance)
    counties["area_km2"] = county_areas_km2(counties)
    return counties.sort_values("county_code").reset_index(drop=True)


def county_areas_km2(counties: gpd.GeoDataFrame) -> pd.Series:
    """Area of each polygon in km2, measured in an equal-area projection."""
    return (counties.to_crs(EQUAL_AREA_CRS).area / 1e6).round(1)


def save_counties(counties: gpd.GeoDataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    counties.to_file(path, driver="GeoJSON", layer_options={"COORDINATE_PRECISION": str(COORDINATE_DECIMALS)})


def load_counties(path: Path) -> gpd.GeoDataFrame:
    """The committed county boundaries, sorted by county code."""
    counties = gpd.read_file(path)
    return counties.sort_values("county_code").reset_index(drop=True)


def county_table(counties: gpd.GeoDataFrame) -> pd.DataFrame:
    """Codes, names and areas without the shapes."""
    return pd.DataFrame(counties.drop(columns="geometry"))
