# south-sudan-flood-drought-monitor

[![CI](https://github.com/JackPhilpott/south-sudan-flood-drought-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/JackPhilpott/south-sudan-flood-drought-monitor/actions/workflows/ci.yml)

Monthly flood, rainfall and vegetation indicators for each of South Sudan's 79 counties, from open
satellite data, using the Google Earth Engine Python API.

From 2019 to 2022 I designed and ran a flood and drought monitoring system for IMPACT Initiatives
(REACH) in South Sudan. It ran every month for over two years in Google Earth Engine (Python):
satellite flood extent from radar and optical imagery, CHIRPS rainfall and NDVI, reported by
county. It produced monthly data, quarterly factsheets and a dashboard used by OCHA, the clusters
and partners, and we checked it on the ground in three post-flood field assessments. Its outputs
also fed two separate REACH workflows, the Shocks Monitoring Index (SMI) and Integrated Needs
Tracking (INT). Two published maps from that work show the kind of output it produced:
[flood frequency 2019-2021](https://repository.impact-initiatives.org/document/reach/c24a7620/REACH_SSD_Map_SMI_FloodFrequency2019_20_21_31JAN2022_A4-2.pdf)
and [drought in Eastern Equatoria, January 2022](https://repository.impact-initiatives.org/document/reach/7deca445/REACH_SSD_Map_SMI_Eastern_Equatoria_Drought_JAN2022_A4.pdf).

This repo is an open-source rebuild of the monitoring part, written from scratch from my own
knowledge of the method and using open data only. It is not REACH's code. It reports each
indicator on its own, with a simple, documented alert threshold. It does not combine them into
an index like the SMI.

**Flood extent here means open water only.** Flooded grassland and swamp, which make up much of
the flooding in Jonglei, Unity and the Sudd, turn brighter on radar rather than darker and are not
detected. Flooded areas in this repo therefore sit well below the figures in humanitarian reports
on the 2019-2022 floods. See [Limitations](#limitations).

## Figures and results

![Flood frequency map of South Sudan, 2019-2025](outputs/figures/flood_frequency.png)

*Number of months each area had open water outside permanent water, 2019-2025. The persistent
water around Rubkona and Guit in Unity stands out, along with the Nile corridor through Jonglei,
the Bahr el Ghazal and Bahr el Arab system and the Upper Nile floodplains.*

![National monthly rainfall (% of normal) and flooded area, 2019-2025](outputs/figures/national_monthly.png)

*Top: national rainfall as % of the 1991-2020 normal. Bottom: total open water outside permanent
water. The rise from 2022 is mostly persistent water in Unity; the high early-2019 values are
largely false detections on burnt land (see [Limitations](#limitations)).*

| | |
| :- | :- |
| ![Flooded share of each county, August 2025](outputs/figures/county_flood_peak.png) | ![NDVI anomaly by county, January 2019](outputs/figures/county_ndvi_drought.png) |
| *Flooded share of each county in August 2025, the month with the most open water.* | *NDVI anomaly in January 2019, the month with the most counties flagged.* |

What the first full run (January 2019 to December 2025, 79 counties) shows:

- **Coverage.** At least one sensor saw 91-97% of the country every month, so very few
  county-months lack a flood value.
- **Rainfall.** The July-October rains were above normal in 2019 in Jonglei (116%), Eastern
  Equatoria (117%) and Upper Nile (111%), and close to or below normal across the country in 2021
  (87-99%), the year of the Unity floods, consistent with floods driven by high river levels from
  upstream rather than by local rain. 622 of the 4,340 county-months with a rainfall normal of at least 20 mm were flagged below
  75% of normal, including 33 in Eastern Equatoria in 2021.
- **Open water.** The largest monthly total was 6,021 km² in August 2025. In Unity the July-November
  average rose from 81-207 km² in 2019-2021 to 1,031-2,154 km² in 2022-2025, as floodwater around
  Rubkona, Guit and Mayendit stayed and became open water. Rubkona was more than 10% flooded in 39
  of the 84 months. The flood flag was raised for 53 county-months in all, every one in Unity apart
  from one false detection in Warrap in April 2019, and for none in 2020 or 2021 (see
  [Limitations](#limitations)).
- **Vegetation.** 222 county-months (3.3%) were flagged for low NDVI, most of them in January 2019
  (21 counties) and the 2022 rainy season.

## How it works

For every county and every month from January 2019 to December 2025:

| Indicator | Source | Reported | Alert flag |
| :- | :- | :- | :- |
| Rainfall | CHIRPS pentads, 0.05° | monthly total (mm), 1991-2020 normal, % of normal | below 75% of normal |
| Vegetation | MODIS MOD13Q1 NDVI, 250 m | monthly mean NDVI, 2001-2020 mean and standard deviation for that month, standardised anomaly | anomaly below -1 |
| Flood extent | Sentinel-1 radar and Sentinel-2 optical, analysed at 100 m | flooded km², % of county area, km² flagged by each sensor, % of county observed | more than 10% of the county flooded |

It also maps flood frequency: the number of months each place was flooded over 2019-2025, the
equivalent of the REACH flood frequency map.

### Pipeline

[`scripts/monitor.py`](scripts/monitor.py) runs each step:

1. **`boundaries`** downloads the county boundaries (OCHA COD-AB) from HDX and writes the
   simplified copy committed in [`data/`](data/README.md).
2. **`submit`** starts the Earth Engine work as batch exports to assets in your Earth Engine
   project, split into chunks of years (see [Scales and quotas](#scales-and-quotas)). It skips
   exports that are done or running, so you run it again until everything has finished.
   `--flood-years 2019-2022` limits the flood exports to some years. **`cost-test`** runs one
   month of every export first, to measure the cost before committing to the full run.
3. **`status`** lists each export as done, running, ready, waiting or failed, with the EECU it used.
4. **`download`** reads the finished exports into `outputs/raw/` and writes the flood-frequency
   GeoTIFF.
5. **`build`** adds the normals, anomalies, flooded shares and alert flags, and writes
   `outputs/county_monthly.csv` and `outputs/national_monthly.csv`. No Earth Engine.
6. **`plots`** draws the four figures from those files. No Earth Engine.
7. **`benchmark`** compares the flood layers with JRC's monthly water maps (see
   [Agreement with JRC monthly water](#agreement-with-jrc-monthly-water)).

All Earth Engine code is in six modules ([`earthengine`](src/ssd_monitor/earthengine.py),
[`rainfall`](src/ssd_monitor/rainfall.py), [`vegetation`](src/ssd_monitor/vegetation.py),
[`flood`](src/ssd_monitor/flood.py), [`validation`](src/ssd_monitor/validation.py) and
[`exports`](src/ssd_monitor/exports.py)). Everything
else is plain Python and is tested without an Earth Engine account. Settings are in
[`config.yml`](config.yml); the Earth Engine project ID comes from the `EE_PROJECT` environment
variable.

### Rainfall

CHIRPS pentads split each month into six periods, so their sum is the month's total. The script
takes the county mean of that total for every month from 1991 to 2025. The **normal** is the mean
of the county's totals for the same calendar month over 1991-2020, and **% of normal** is the
month's total divided by the normal. In dry months the normal is close to zero, and a few mm of
rain would show as several hundred per cent, so % of normal is left blank when the normal is
below 20 mm.

### Vegetation

MOD13Q1 is a 16-day NDVI composite at 250 m. A month's NDVI is the mean of the composites whose
16-day period starts in that month, using only pixels rated good or marginal. Water, built-up and
bare land are masked with ESA WorldCover, because their NDVI says nothing about vegetation
condition. The **standardised anomaly** compares each county-month with the same calendar month
in 2001-2020: (NDVI − baseline mean) / baseline standard deviation. An anomaly of −1 means one
standard deviation below the usual value for that time of year.

Normals and baselines are computed from the county values in `outputs/raw/`, not per pixel in
Earth Engine, so anyone can recompute them from the CSVs. They are left blank where fewer than
75% of the baseline years have a value.

### Flood extent, step by step

The structure follows the
[UN-SPIDER recommended practice for flood mapping with Sentinel-1 in Earth Engine](https://www.un-spider.org/advisory-support/recommended-practices/recommended-practice-google-earth-engine-flood-mapping):
find water on radar, then remove permanent water, steep slopes and isolated pixels. UN-SPIDER maps
a single event; this repo runs every month and adds Sentinel-2 optical data, as the original system
did. The code is in [`src/ssd_monitor/flood.py`](src/ssd_monitor/flood.py) and every threshold is
in `config.yml`.

1. **Radar flag.** Calm open water reflects the radar signal away from the satellite, so it looks
   dark. For each orbit direction (ascending and descending, which see the ground at different
   angles), the month's median VV backscatter is taken, and a pixel is flagged when it is at or
   below −18 dB. It is flagged if either direction flags it.
2. **Optical flag.** Sentinel-2 surface reflectance scenes from the month, skipping scenes that are
   more than 80% cloud. Cloudy pixels are masked with Cloud Score+ (clear score below 0.6). For
   the rest, MNDWI = (green − SWIR1) / (green + SWIR1), and a pixel is flagged when the month's
   median MNDWI of its clear observations is above 0.
3. **Combine.** A pixel is flooded where either sensor flags it, except:
   - permanent water: water in at least 80% of observations in JRC Global Surface Water (1984-2021);
   - steep slopes above 5° (HydroSHEDS), where radar shadow looks like water and water doesn't stand;
   - isolated patches of fewer than 8 connected pixels (8 ha at 100 m), which are mostly noise.
4. **Store each month once.** The month's four flags (flooded, flagged by radar, flagged by optical,
   observed by either) are packed into one byte per pixel, and each year's twelve months are
   exported as one image asset. Everything below reads those assets, so the expensive part runs
   once per month.
5. **County areas.** Earth Engine sums the pixel areas per county: flooded km², the km² each
   sensor flagged (often both), and the km² that at least one sensor observed that month. If no
   sensor saw any of a county that month, its flood values are left blank rather than set to 0.
6. **Flood frequency.** The number of months each 100 m pixel was flooded over 2019-2025, averaged
   into 0.005° cells (about 550 m) for the GeoTIFF and the map.

**Why a darkness rule and not change detection.** The first version of this repo followed
UN-SPIDER more closely: a pixel was water when it had become at least 3 dB darker than a
dry-season reference (the median of January-March 2017-2019) and VV was below −15 dB. Checked
against JRC's Landsat water maps (next section), it found less than half of the water Landsat saw.
The reason is the ground: the clay plains of Jonglei and Unity are already dark on radar in the
dry season, so water there is dark but not much darker than the reference. A plain darkness
threshold agreed better with Landsat on both counts, so it replaced the change rule. The change
rule is kept in [`validation.py`](src/ssd_monitor/validation.py) only for that comparison.

Other differences from UN-SPIDER: the month's median rather than a single mosaic; speckle reduced
by reading the imagery at 100 m instead of a 50 m smoothing filter; permanent water from Global
Surface Water *occurrence* rather than *seasonality*.

### Agreement with JRC monthly water

[JRC's Global Surface Water monthly history](https://developers.google.com/earth-engine/datasets/catalog/JRC_GSW1_4_MonthlyHistory)
maps open water month by month from Landsat, independently of this project. `benchmark` compares
the flood layers with it pixel by pixel at 100 m, where Landsat had a view, leaving out permanent
water and steep slopes, in six flood-prone counties (Bor South, Fangak and Twic East in Jonglei;
Panyijiar and Rubkona in Unity; Luakpiny/Nasir in Upper Nile) in three months (September 2020 and
October 2021, both flood seasons, and February 2021, dry season). Recall is the share of JRC's water
that a layer also flags; precision is the share of a layer's flagged area that JRC also calls water.
Both are pooled over the 18 county-months by area (1,093 km² of JRC water in total):

| Layer | Flagged | Recall | Precision |
| :- | -: | -: | -: |
| Flood layer used here (radar or optical, after the patch filter) | 1,135 km² | 0.65 | 0.62 |
| Radar, darkness rule (used) | 880 km² | 0.61 | 0.76 |
| Radar, dry-season change rule (first design, dropped) | 962 km² | 0.39 | 0.44 |
| Optical, MNDWI | 859 km² | 0.45 | 0.58 |

These figures measure **agreement with JRC monthly water, not accuracy**. JRC has its own errors,
it only covers open water that Landsat saw (so flooding under vegetation is missing from both),
and it stops at the end of 2021. The per county-month figures are in `outputs/benchmark_jrc.csv`.

### Alert flags

| Column | Flag raised when | Setting in `config.yml` |
| :- | :- | :- |
| `flag_rain_low` | rainfall below 75% of the 1991-2020 normal | `alerts.rain_pct_normal_below` |
| `flag_ndvi_low` | NDVI anomaly below −1 standard deviation | `alerts.ndvi_anomaly_below` |
| `flag_flood` | more than 10% of the county flooded | `alerts.flooded_pct_above` |

Each flag is true, false, or blank when its indicator is missing, so "no data" is never read as
"no problem". The thresholds are strict (exactly 75% is not flagged). They are starting values,
not yet checked against known events. There is no combined score.

### Scales and quotas

| Indicator | Native resolution | Processing scale | Why |
| :- | :- | :- | :- |
| Rainfall | 0.05° (~5.5 km) | 5,566 m | native |
| Vegetation | 250 m | 250 m | native |
| Flood | 10 m (radar), 10-20 m (optical) | 100 m | see below |

A national monthly analysis at 10 m would read 100 times as many pixels as at 100 m. At 100 m
each pixel is one hectare, still fine enough for floodplains and county totals, and reading the
imagery from Earth Engine's averaged pyramid also smooths radar speckle. The cost is that floods
narrower than about 100 m, along small channels for example, are missed or diluted.

The work runs as 20 batch exports rather than interactive requests, so no single request hits
Earth Engine's time limits and a failed export can be rerun on its own:

| Export | Count | Covers |
| :- | :- | :- |
| `rainfall_1991_2025` | 1 | all years, as the data is small |
| `ndvi_<years>` | 5 | five years each, 2001-2025 |
| `flood_layers_<year>` | 7 | one year each, 2019-2025: the expensive part |
| `flood_table_<year>` | 7 | county areas read from that year's layers |

**Measured cost.** By Earth Engine's task records, the flood layers for all 84 months used 61.3
EECU-hours. The yearly cost follows the number of Sentinel-1 scenes: 9.9, 10.7 and 8.2 EECU-hours
for 2019-2021, 7.3, 7.0 and 8.0 for 2022-2024 (Sentinel-1A alone, about half the scenes) and 10.3
for 2025 (Sentinel-1C joined, and more Sentinel-2 scenes). The task records of table exports
under-report their cost, so those were profiled instead: one month of county tables cost about
205 EECU-seconds for flood, 36-76 for NDVI and under 1 for rainfall. Before the full run,
`cost-test` measured one month and a profiler run on single counties showed where the time went;
this led to the single-pass design and to a cheaper patch filter that gives identical results.

### Outputs

| File | Contents |
| :- | :- |
| `outputs/county_monthly.csv` | one row per county and month, with every indicator and flag |
| `outputs/national_monthly.csv` | national monthly totals and means, and the number of counties flagged |
| `outputs/raw/rainfall.csv`, `ndvi.csv`, `flood.csv` | what Earth Engine returned, so everything else rebuilds without it |
| `outputs/flood_frequency_2019_2025.tif` | GeoTIFF: band 1 months flooded × 10, band 2 % of the cell excluded (permanent water or steep slope) |
| `outputs/figures/*.png` | the four figures |
| `outputs/benchmark_jrc.csv`, `benchmark_jrc_summary.csv` | the agreement check with JRC monthly water, per county-month and pooled |

Columns of `county_monthly.csv`: `county_code`, `county_name`, `state_code`, `state_name`,
`month`, `area_km2`; `rain_mm`, `rain_normal_mm`, `rain_pct_normal`; `ndvi`,
`ndvi_baseline_mean`, `ndvi_baseline_sd`, `ndvi_anomaly`; `flooded_km2`, `flooded_pct`,
`flooded_sar_km2`, `flooded_optical_km2`, `flood_observed_pct`; `flag_rain_low`,
`flag_ndvi_low`, `flag_flood`.

## Quick start

### With pip

Python 3.12 (as in CI and Docker); the tests also pass on 3.13.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

pytest                             # unit tests, no Earth Engine needed
ruff check . && ruff format --check .
```

To run the monitor you need a Google Cloud project registered for Earth Engine (free for
non-commercial use, see [Earth Engine access](https://developers.google.com/earth-engine/guides/access)):

```bash
earthengine authenticate            # once per machine; opens a browser
export EE_PROJECT=your-project-id   # PowerShell: $env:EE_PROJECT = "your-project-id"

python scripts/monitor.py cost-test  # one month of everything; run again for the estimate
python scripts/monitor.py submit     # starts rainfall, NDVI and the yearly flood layers
python scripts/monitor.py status     # progress and EECU used (the Code Editor's Tasks tab too)
python scripts/monitor.py submit     # as layers finish: starts each year's flood table
python scripts/monitor.py download   # once everything is done
python scripts/monitor.py build      # county_monthly.csv and national_monthly.csv
python scripts/monitor.py plots      # the four figures
python scripts/monitor.py benchmark  # agreement with JRC monthly water (interactive)
```

`submit --flood-years 2019-2022` runs part of the flood period first, to check the cost before
committing to the rest.

`build` and `plots` only read `outputs/`, so once the outputs are committed you can rebuild the
tables and figures without Earth Engine.

### With Docker

The image installs `requirements.txt` on `python:3.12-slim` and runs the tests by default:

```bash
docker build -t south-sudan-flood-drought-monitor .
docker run --rm south-sudan-flood-drought-monitor
```

To run the monitor in the container, pass in your project ID and your existing Earth Engine
credentials (mounted read-only), and mount `outputs/` so the results land on your machine:

```bash
docker run --rm \
  -e EE_PROJECT=your-project-id \
  -v ~/.config/earthengine:/root/.config/earthengine:ro \
  -v "$(pwd)/outputs:/app/outputs" \
  south-sudan-flood-drought-monitor python scripts/monitor.py submit
```

## Tests and CI

The [tests](tests/) cover the parts that don't need Earth Engine, using four invented counties
and invented values: the settings, month and year windows, planning the exports and the cost
test, how the monthly flood flags are packed into one byte, turning Earth Engine's output into
tables (including months with no data), the normals and anomalies, flooded shares, the alert
thresholds, the national summary, the JRC agreement summary, the boundary simplification, the
GeoTIFF and its download tiles, the four figures, and the `build` and `plots` commands end to end. One test also checks that the
offline modules never import Earth Engine.

[GitHub Actions](.github/workflows/ci.yml) runs on every push and pull request: `ruff check`,
`ruff format --check` and `pytest` on Python 3.12, then it builds the Docker image and runs the
tests inside it. CI makes no Earth Engine calls and holds no credentials.

## Data and licences

| Data | Used for | Licence and attribution |
| :- | :- | :- |
| [CHIRPS](https://developers.google.com/earth-engine/datasets/catalog/UCSB-CHG_CHIRPS_PENTAD) pentads (UCSB Climate Hazards Center) | rainfall | public domain: the authors have waived all copyright. Funk et al. (2015) |
| [MODIS MOD13Q1 v6.1](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD13Q1) (NASA LP DAAC) | NDVI | no restrictions on use or redistribution. Didan (2021) |
| [Sentinel-1 GRD](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S1_GRD) and [Sentinel-2 surface reflectance](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED) (Copernicus, ESA) | flood extent | [Copernicus Sentinel data terms](https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice): free and open, with attribution. The outputs contain modified Copernicus Sentinel data 2019-2025 |
| [Cloud Score+](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_CLOUD_SCORE_PLUS_V1_S2_HARMONIZED) (Google) | Sentinel-2 cloud mask | CC BY 4.0. Pasquarella et al. (2023) |
| [JRC Global Surface Water v1.4](https://developers.google.com/earth-engine/datasets/catalog/JRC_GSW1_4_GlobalSurfaceWater) and [monthly history](https://developers.google.com/earth-engine/datasets/catalog/JRC_GSW1_4_MonthlyHistory) | permanent water mask; agreement check | free, without restriction of use, under the Copernicus programme. Source: EC JRC/Google. Pekel et al. (2016) |
| [ESA WorldCover 2021 v200](https://developers.google.com/earth-engine/datasets/catalog/ESA_WorldCover_v200) | NDVI land mask | CC BY 4.0. Zanaga et al. (2022) |
| [HydroSHEDS void-filled DEM](https://developers.google.com/earth-engine/datasets/catalog/WWF_HydroSHEDS_03VFDEM) (WWF) | slope mask | free for non-commercial and commercial use under the [HydroSHEDS licence](https://www.hydrosheds.org/page/license). Lehner et al. (2008) |
| [OCHA COD-AB South Sudan](https://data.humdata.org/dataset/cod-ab-ssd) (South Sudan IMWG, NBS, IOM and OCHA, via HDX) | county boundaries | [CC BY-IGO](https://creativecommons.org/licenses/by/3.0/igo/legalcode). A simplified copy is committed in [`data/`](data/README.md) |

The satellite data are read through the Earth Engine API when the exports run, so there is nothing
to download in advance; `scripts/monitor.py boundaries` downloads the boundaries. The boundaries
and names shown and the designations used do not imply official endorsement or acceptance by the
United Nations. Final status of the Abyei area is not yet determined.

## Limitations

**Flood extent is open water only.** The radar flag looks for dark surfaces. Where water stands
under grass, papyrus or trees, as across much of Jonglei, Unity and the Sudd, the radar signal
bounces between the water and the stems and the pixel gets *brighter*. Normal wet-season vegetation
and wet soil brighten too, so brightening can't be used as a flood signal on its own, and optical
imagery can't see through the canopy either. In Twic East (Jonglei) in September 2020, a month of
severe flooding, most of the county turned brighter on radar than in the dry season, yet JRC's
Landsat maps show only 80 km² of open water outside permanent water, about 1.3% of the county, and
this repo's flood layer flags 102 km² (`outputs/benchmark_jrc.csv`). Flooded areas here therefore
sit well below the figures in humanitarian reports on the 2019-2022 floods, which count flooded
land, crops and homes.

**The NDVI baseline doesn't account for the long-term greening trend.** NDVI over South Sudan has
risen across 2001-2025, so against a fixed 2001-2020 baseline the recent years read as greener than
normal, and the vegetation flag fires less and less: on 22.9% of county-months in 2001-2010, 8.0%
in 2011-2020 and 3.3% in 2019-2025 (from `outputs/raw/ndvi.csv`). As a result, the January 2022
drought in Eastern Equatoria that the original system mapped doesn't show up as an NDVI flag here:
Eastern Equatoria was the only state close to its normal that month, while the rest of the country
was well above it. The rainfall indicator does pick it up: the 2021 rains in Eastern Equatoria were
below 75% of normal in March, June and October. A detrended or rolling baseline would fix this, but
that is not done here.

**Dark dry land is flagged as water in the dry season.** Burnt grassland and some dry soils are as
dark on radar as water. In March 2019 the radar flagged 320-410 km² of "water" in each of Gogrial
East, Tonj North and Tonj South (Warrap) that optical imagery did not see; 38-51% of it lay on land
that MODIS mapped as burnt that dry season, while burnt land covered only 16-22% of those counties.
In Kapoeta East (Eastern Equatoria) similar radar-only detections fall on unburnt, semi-arid land.
These false detections push up the dry-season totals, explain the high values in early 2019, and
are why the 2019 flood season does not stand out in the national chart. The radar rule needs a
dry-season safeguard (for example using radar only where optical imagery was cloudy), which is not
done yet.

**Open water that stays is counted as flooded every month.** The permanent-water mask comes from
Landsat observations up to 2021, so water bodies that formed during the 2019-2022 floods and never
drained, as around Rubkona, Guit and Mayendit in Unity, count as flooded in every later month. This
drives most of the rise in the national total from 2022. More Sentinel-1 and Sentinel-2 scenes in
2025 probably add detections too, so totals for different years are not strictly comparable.

**The flood flag rarely fires.** Open water seldom covers more than 10% of a county, so the flag was
raised for only 53 county-months, almost all in Unity after 2022, and not at all in 2020 or 2021.
For flooding as humanitarian reports describe it, the flooded km² and the flood-frequency map are
more informative than the flag; a lower or county-specific threshold would be a better test.

**Scene edges leave some straight lines.** A few thin straight lines in the flood-frequency map, in
the Equatorias and along the south-east border, follow satellite scene edges rather than water.

**Clouds limit the optical data in the rainy season.** When floods peak, Sentinel-2 often has few
clear views, so the radar carries most of the detection. `flood_observed_pct` shows how much of
each county either sensor actually saw that month.

**Sentinel-1 coverage is thinner from 2022.** Sentinel-1B failed on 23 December 2021, which left
Sentinel-1A alone until Sentinel-1C, launched in December 2024, joined it. With fewer passes, some
months have no radar pass over part of the country in one or both directions. Those areas count
as unobserved, not as dry.

**County averages hide local extremes.** A county can pass every threshold while one part of it
is badly flooded or very dry. The flood-frequency map shows where within a county flooding
happens.

**No field verification in this version.** The original system was checked on the ground in three
post-flood field assessments. This rebuild has not been validated in the field.

**Seasonal wetlands count as flooded.** Open water that is not permanent (in at least 80% of
Landsat observations since 1984) counts as flooded whenever it appears, so wetlands and floodplains
that fill every rainy season are flagged every year, as they were in the original flood frequency
maps. A county with large seasonal wetlands can be flagged most years; the flag compares with a
fixed share of the county, not with that county's usual extent.

**The thresholds are not fitted to this data.** The radar threshold was chosen by comparison with
JRC's Landsat water in six counties and three months; the optical and alert thresholds are
documented starting values in `config.yml`.

**Smaller points.** Floods narrower than about 100 m are missed (see
[Scales and quotas](#scales-and-quotas)). The simplified boundaries change county areas by less
than 0.03% compared with COD-AB.

## References

- Didan, K. (2021). MODIS/Terra Vegetation Indices 16-Day L3 Global 250m SIN Grid V061. NASA EOSDIS
  Land Processes DAAC. [doi:10.5067/MODIS/MOD13Q1.061](https://doi.org/10.5067/MODIS/MOD13Q1.061)
- Funk, C., Peterson, P., Landsfeld, M., et al. (2015). The climate hazards infrared precipitation
  with stations: a new environmental record for monitoring extremes. *Scientific Data*, 2, 150066.
  [doi:10.1038/sdata.2015.66](https://doi.org/10.1038/sdata.2015.66)
- Lehner, B., Verdin, K. and Jarvis, A. (2008). New global hydrography derived from spaceborne
  elevation data. *Eos, Transactions, AGU*, 89(10), 93-94.
- Pasquarella, V. J., Brown, C. F., Czerwinski, W. and Rucklidge, W. J. (2023). Comprehensive
  quality assessment of optical satellite imagery using weakly supervised video learning.
  *CVPR Workshops*, 2125-2135. [doi:10.1109/CVPRW59228.2023.00206](https://doi.org/10.1109/CVPRW59228.2023.00206)
- Pekel, J.-F., Cottam, A., Gorelick, N. and Belward, A. S. (2016). High-resolution mapping of
  global surface water and its long-term changes. *Nature*, 540, 418-422.
  [doi:10.1038/nature20584](https://doi.org/10.1038/nature20584)
- Zanaga, D., Van De Kerchove, R., Daems, D., et al. (2022). ESA WorldCover 10 m 2021 v200.
  [doi:10.5281/zenodo.7254221](https://doi.org/10.5281/zenodo.7254221)

## Related repos

- [methane-s5p-explorer](https://github.com/JackPhilpott/methane-s5p-explorer): Sentinel-5P
  methane with the Earth Engine Python API.
- [south-sudan-aok-pipeline](https://github.com/JackPhilpott/south-sudan-aok-pipeline): REACH's
  Area of Knowledge pipeline for South Sudan (R), monthly key-informant data on hard-to-reach
  settlements.

## Licence

Code: [MIT](LICENSE). Data: see [Data and licences](#data-and-licences).

Built with AI-assisted development, reviewed and tested by me.
