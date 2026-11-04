# Data

`ssd_admin2_simplified.geojson`: South Sudan's 79 counties (admin 2), simplified from the
[OCHA Common Operational Dataset of administrative boundaries (COD-AB) for South Sudan](https://data.humdata.org/dataset/cod-ab-ssd)
on HDX. Sources: South Sudan Information Management Working Group (IMWG), National Bureau of
Statistics (NBS), International Organization for Migration (IOM) and OCHA.

- **Licence:** [Creative Commons Attribution for Intergovernmental Organisations (CC BY-IGO)](https://creativecommons.org/licenses/by/3.0/igo/legalcode).
- **Changes:** only the codes and names (`adm2_pcode`, `adm2_name`, `adm1_pcode`, `adm1_name`) are kept,
  renamed to `county_code`, `county_name`, `state_code`, `state_name`. Shapes are simplified with a
  0.002° tolerance using coverage simplification, so neighbouring counties still share their
  borders. Coordinates are rounded to 5 decimals. `area_km2` is measured on the simplified shapes
  in an equal-area projection (Africa Albers, ESRI:102022).
- **Rebuild:** `python scripts/monitor.py boundaries` downloads the original into `data/raw/`
  (not committed) and writes this file again.

The boundaries and names shown and the designations used do not imply official endorsement or
acceptance by the United Nations. Final status of the Abyei area is not yet determined.
