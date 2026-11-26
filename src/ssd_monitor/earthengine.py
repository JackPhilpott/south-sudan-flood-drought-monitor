"""Earth Engine helpers shared by the indicator modules.

These only build Earth Engine expressions. Nothing is sent to Earth Engine until exports.py starts
an export or reads a result.
"""

import json

import ee
import geopandas as gpd
import shapely


def planar_geometry(geojson: dict) -> ee.Geometry:
    """A GeoJSON geometry in longitude/latitude, with straight (not geodesic) edges like the source file."""
    return ee.Geometry(geojson, "EPSG:4326", False)


def counties_to_ee(counties: gpd.GeoDataFrame) -> ee.FeatureCollection:
    """The county polygons as an Earth Engine FeatureCollection, keyed by county_code."""
    geojson = json.loads(counties[["county_code", "geometry"]].to_json(drop_id=True))
    return ee.FeatureCollection(
        [
            ee.Feature(planar_geometry(f["geometry"]), {"county_code": f["properties"]["county_code"]})
            for f in geojson["features"]
        ]
    )


def country_to_ee(counties: gpd.GeoDataFrame) -> ee.Geometry:
    """The outline of all counties together, used to clip the exported images."""
    return planar_geometry(json.loads(shapely.to_geojson(counties.union_all())))


def search_area_to_ee(counties: gpd.GeoDataFrame, margin_deg: float = 0.05) -> ee.Geometry:
    """A simple outline slightly larger than the country, for finding the scenes that touch it.

    Scene searches (filterBounds) with the detailed outline run out of memory in Earth Engine. A
    buffered, simplified outline has a few dozen vertices and still catches every scene at the edges.
    """
    outline = counties.union_all().buffer(margin_deg).simplify(margin_deg)
    return planar_geometry(json.loads(shapely.to_geojson(outline)))


def empty_image(bands: list[str]) -> ee.Image:
    """A float image with the given bands and every pixel masked."""
    return ee.Image.constant([0] * len(bands)).toFloat().rename(bands).updateMask(ee.Image(0))


def with_fallback(images: ee.ImageCollection, bands: list[str]) -> ee.ImageCollection:
    """Add one fully masked image to a collection before reducing it.

    Reducing an empty collection gives an image with no bands, and the next band operation fails.
    With the masked image added, a month with no data gives masked pixels instead, which become a
    missing value for each county. Masked pixels are ignored by sum, mean, median and max, so the
    added image changes nothing when there is data. Every image is cast to float first, because
    Earth Engine won't reduce a collection that mixes band types (MODIS NDVI is stored as integers).
    """
    as_float = images.map(lambda image: ee.Image(image).toFloat())
    return as_float.merge(ee.ImageCollection([empty_image(bands)]))


def county_stats(
    images: list[ee.Image],
    months: list[str],
    counties: ee.FeatureCollection,
    reducer: ee.Reducer,
    scale: float,
    tile_scale: int = 2,
) -> ee.FeatureCollection:
    """Reduce one image per month over every county: one feature per county-month.

    Each feature keeps county_code, gets a "month" property and the reducer's outputs, and has its
    polygon replaced by the centroid so the exported table stays small.
    """
    monthly = ee.ImageCollection(
        [image.set("month", month) for image, month in zip(images, months, strict=True)]
    )

    def reduce(image):
        image = ee.Image(image)
        month = image.get("month")
        stats = image.reduceRegions(
            collection=counties, reducer=reducer, scale=scale, crs="EPSG:4326", tileScale=tile_scale
        )
        return stats.map(
            lambda f: ee.Feature(f).setGeometry(ee.Feature(f).geometry().centroid(100)).set("month", month)
        )

    return ee.FeatureCollection(monthly.map(reduce)).flatten()
