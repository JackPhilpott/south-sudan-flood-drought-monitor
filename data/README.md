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

`ocha_flood_counties.csv`: the counties reported as flood-affected each year, for checking the flood
flag. One row per year and county: `year`, `county_code`, `county_name`, `state_name`, `people`
(people affected; for 2019, people displaced), `partial` (true for 2019) and `sources` (the file it
came from, with its licence and date).

- **Sources:** [South Sudan: Flood Data](https://data.humdata.org/dataset/south-sudan-flood-data)
  (OCHA South Sudan, CC0) for 2021, 2022 (October and November snapshots), 2024 and 2025, and
  [South Sudan: Flood Locations](https://data.humdata.org/dataset/south-sudan-flood-locations)
  (IOM Displacement Tracking Matrix flood displacement locations, end of October 2019, posted by
  OCHA South Sudan; CC BY 4.0) for 2019. 2020 and 2023 have no county list: OCHA's final 2020
  flooding report gives 49 flood-affected counties without naming them.
- **Licence:** CC BY 4.0, attributing OCHA South Sudan and IOM DTM.
- **Changes:** a county counts when its reported number is above zero (2019: when it has a
  displacement location). Five 2021 names are matched to COD-AB spellings (`Bor` → Bor South, `Jur`
  → Jur River, `Nasir` → Luakpiny/Nasir, `Panyijar` → Panyijiar, `Pigi / Canal` → Canal/Pigi). In
  the 2019 file a few Nyirol locations carry Fangak's county code; their payam codes are Nyirol's,
  so the county comes from the payam code.
- **Rebuild:** `python scripts/monitor.py ocha` downloads the files into `data/raw/ocha/` (not
  committed) and writes this file again.
