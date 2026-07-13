# US Real Estate Growth Dashboard

A CoStar-style, self-refreshing dashboard that scores every US metro on
**growth potential** and **affordability** to surface the best buy-and-hold
markets. Live page (once GitHub Pages is enabled for this repo):
`https://pratushrai.github.io/marketpredictionmodel/`

## How it works

- **`pipeline/build_data.py`** — stdlib-only Python. Pulls Zillow Research
  ZHVI (home values), ZORI (rents), and for-sale inventory; US Census ACS
  5-year data (income, population, rent, unemployment — current and 5-years
  prior vintages); and Census CBSA centroid coordinates. Computes, per metro,
  a transparent momentum + fundamentals composite: a 12-month appreciation
  forecast plus growth, affordability, and buy-and-hold scores (0–100).
  Output: `data/market-data.json`.
- **`.github/workflows/refresh-real-estate-data.yml`** — reruns the pipeline
  daily at 10:23 UTC (and on demand via *Run workflow*) and commits the fresh
  JSON. This is what keeps a static GitHub Pages site "live."
- **`index.html`** — the dashboard. Interactive US metro map, growth-vs-
  affordability quadrant, forecast rankings, per-metro detail with ~10 years
  of price history, a fully sortable data table, filters, and dark mode. The
  page re-checks for a fresh model run every 15 minutes.

## Model (v1.0, documented in-page)

Forecast 12-mo growth = damped blend of 12-mo (×0.40), annualized 6-mo
(×0.25), 3-yr (×0.15), and 5-yr (×0.10) appreciation, tilted up for
above-national population growth and rent yield, dragged down for stretched
price-to-income and above-national unemployment, clamped to −10…+15%.
Buy & hold score = 50% growth percentile + 35% affordability + 15% rent-yield
percentile. A heuristic composite, not a trained ML model — and not
investment advice.

## Data sources & the Census API key

The Census *data API* (income, price-to-income affordability, unemployment)
now requires a free API key. Without one the pipeline still works — home
values, rents, forecasts, coordinates, and population/growth (from the
keyless Census Population Estimates file) all populate — but income-based
affordability metrics stay empty.

To unlock them: request a key at
https://api.census.gov/data/key_signup.html (instant, free), then add it to
this repo as an Actions secret named `CENSUS_API_KEY`
(Settings → Secrets and variables → Actions → New repository secret).
The next scheduled run picks it up automatically.

## Setup

1. Push this repo, then enable **Settings → Pages → Deploy from branch →
   `main` / root**.
2. The workflow runs on the first push and commits `data/market-data.json`;
   after that it refreshes daily.
