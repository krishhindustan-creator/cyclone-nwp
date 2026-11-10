# cyclone-nwp

**Multi-model cyclone, district and subdivisional forecast guidance for India, with automatic verification.**

Live site: **https://krishhindustan-creator.github.io/cyclone-nwp/**
Companion site (upper air): **https://krishhindustan-creator.github.io/rsrw-tphigrams/** (code: [rsrw-tphigrams](https://github.com/krishhindustan-creator/rsrw-tphigrams))

The system downloads forecasts from global and Indian models, turns them into products a forecaster can use on a phone or desktop, verifies them against observations, and publishes everything as static web pages. It runs on free GitHub Actions and GitHub Pages: no server, no office computer, no cost.

> These are guidance products prepared at RWFC New Delhi, India Meteorological Department. They are **not official IMD forecasts**.

---

## Products (tabs)

| Tab | What it gives |
|---|---|
| ECMWF ENS | Cyclone tracks of the 51-member ECMWF ensemble: spaghetti, strike probability (120 km), MSW and MSLP plumes, landfall summary, last six runs compared. Basin selector (Bay of Bengal / Arabian Sea). |
| IMD MME | Track, MSW and MSLP of 7 models (ECMWF HRES, NCEP GFS, CMC GDPS, IMD GFS, NCUM-G, NCUM-R, NAVGEM) and their mean, written in IMD's MME text format. Basin selector. |
| A-Deck | Model tracks collected by JTWC (NAVGEM, UKMO, CMC; GEFS, Canadian and Navy ensemble means). |
| MME District Rainfall Forecast | 24-h rainfall for 742 districts, Day 1–10, from 10 models (ECMWF IFS and AIFS, NCEP GFS, NOAA AIGFS, DWD ICON, CMC GDPS, Météo-France ARPEGE, IMD GFS, NCUM-G, NCUM-R): each model, MME mean and median, share of models in each IMD category. |
| Ensemble District R/F Probability | Chance of each IMD rainfall category per district from 102 pooled members (ECMWF ENS 50, GEFS 31, GEPS 21), plus member percentiles. |
| Subdivisional Forecast | For IMD's 36 subdivisions: 24-h rainfall, maximum and minimum temperature, Day 1–10; each model and the MME, as a map and a table. |
| Verification | District forecasts against IMD gridded rainfall (0.25°) and DRMS/iRAINS gauge rainfall: hit/miss maps; POD, FAR, CSI and bias for ≥ 64.5, ≥ 115.6 and ≥ 204.5 mm; scores over any day, month, season or the whole archive. |
| Track verification | Landfall, track and intensity errors of cyclone forecasts against JTWC and RSMC New Delhi best tracks. |
| About & technical notes | Full description of data, grids, methods, update times and archives ([docs.html](docs.html)). |

### Key methods

- **District / subdivision value:** the highest grid-point value inside the area (rainfall, Tmax) or the lowest (Tmin), so a localised heavy spell is not averaged away.
- **MME:** simple equal-weight mean (and median) of the models available for the same 24-h window. Only 00 and 12 UTC runs are used for rainfall.
- **Late runs:** a model whose new run is not yet published is taken from its previous run for the same window and labelled with that run, e.g. `NCUM-R (28/12Z)`.
- **Probabilities:** raw member frequencies from the pooled ensembles; no calibration.
- **Cyclone tracker (gridded models):** minimum of smoothed MSLP with 10 m wind; ECMWF tracks from ECMWF's own tracker.
- **IMD rainfall categories:** heavy 64.5–115.5 mm, very heavy 115.6–204.4 mm, extremely heavy ≥ 204.5 mm per 24 h (08:30–08:30 IST for verification).

---

## Update times (IST)

| Product | Updated | Kept |
|---|---|---|
| IMD MME, A-Deck | 10:30, 13:30, 14:30, 22:30, 01:30, 02:30 | 15 days |
| ECMWF ENS cyclone | ~13:30 (00 UTC run), ~01:30 (12 UTC run) | 15 days |
| District rainfall, probability, subdivisional | Every 30 min, 08:40–16:10 and 20:40–04:10, as models arrive | 7 days (14 runs) |
| Verification | With each update | Permanent (`verif-archive` branch) |
| T-phi grams (companion repo) | 07:30, 09:00, 19:30, 21:00 | Rolling |

---

## Repository layout

| Path | Purpose |
|---|---|
| `.github/workflows/nwp.yaml` | Main schedule: runs all scripts below and publishes to `gh-pages` |
| `nwp_runner.py` | ECMWF ENS cyclone products and the 7-model cyclone MME |
| `district_runner.py` | District rainfall from 10 models, MME mean/median/chance |
| `ens_district_runner.py` | District category probabilities from ECMWF ENS, GEFS and GEPS |
| `subdiv_temp_runner.py` | Subdivisional maximum and minimum temperature |
| `verif_runner.py` | Daily rainfall verification and the permanent archive |
| `track_verif.py` | Cyclone track, landfall and intensity verification |
| `*.html` | Web pages for each tab; `index.html` is the tab container |
| `india_districts.geojson`, `imd_subdivisions.json` | District boundaries (IMD DRMS, 742 districts) and subdivision membership (36) |
| `ECMWF_ensemble_colab.py`, `MME_7models_colab.py` | Earlier Google Colab versions, kept for reference |

Branches: `main` (code), `gh-pages` (published site, rebuilt each run), `verif-archive` (observed and forecast district values for every verified day).

---

## Running your own copy

1. Fork the repository and enable GitHub Actions and GitHub Pages (source: `gh-pages` branch).
2. Public data (ECMWF, NOAA, ECCC, DWD, Météo-France, JTWC/UCAR, IMD gridded rainfall) need no login.
3. IMD model fields (IMD GFS, NCUM-G, NCUM-R) come from IMD's NWP SFTP server and need the repository secret `IMD_PWD`. Without it those models are simply left out.
4. Python 3.11 with: `eccodes cartopy paramiko h5py scipy shapely pandas matplotlib ecmwf-opendata imdlib`.

---

## Data sources and acknowledgements

India Meteorological Department (NWP Division SFTP server; IMD Pune gridded rainfall; iRAINS DRMS district rainfall; RSMC New Delhi best tracks); NCMRWF (NCUM-G, NCUM-R); ECMWF open data (CC BY 4.0); NOAA/NCEP (GFS, AIGFS, GEFS); Environment and Climate Change Canada (GDPS, GEPS); DWD (ICON); Météo-France (ARPEGE); JTWC and UCAR RAL (a-decks, best tracks). Restricted IMD data are used with institutional credentials; only derived products are published.

## How it was built

All code was written by a generative AI assistant (Claude, Anthropic) from requirements set, tested and validated meteorologically by the forecaster. The development process and an evaluation of the system are described in:

> Mishra, K., Kumar, V. *Building zero-cost operational weather decision support with a generative AI coding assistant: an Indian case study.* Manuscript in preparation.

## Licence and contact

Code: [MIT License](LICENSE). Data remain under their providers' terms.

Krishna Mishra, Scientist D, Regional Weather Forecasting Centre, New Delhi, India Meteorological Department · krishhindustan@gmail.com · ORCID [0000-0003-3028-7383](https://orcid.org/0000-0003-3028-7383)
