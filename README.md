# US Real Estate Capital Allocation Dashboard

A self-refreshing dashboard that scores every US metro on growth, affordability
and eleven separate **asset classes**, to surface where capital is most
effectively deployed. Live page:
`https://pratushrai.github.io/marketpredictionmodel/`

## What it answers

- Which metros are growing — in population, jobs, income and approved construction?
- Which are still affordable relative to local income?
- For a given metro, **which asset class** is the strongest use of capital —
  apartments, industrial, data centers, retail, hotels, land, or something else?
- What is the political, regulatory and physical-hazard risk of owning there?

## Data sources

All keyless unless noted. Every source degrades independently: a failure is
recorded with a diagnostic excerpt and the run continues.

| Source | What it contributes |
|---|---|
| Zillow ZHVI / ZORI / inventory / days-to-pending | Home values, rents, liquidity |
| **FHFA HPI** | Repeat-sales price index from actual mortgage transactions — a non-Zillow, non-modeled check |
| **Census Building Permits Survey** | Permits by structure type (1-unit / 2 / 3-4 / 5+) — what local government actually approved |
| **BLS QCEW** | Employment, establishments and wages by NAICS sector per metro — the demand driver for every commercial class |
| Census Population Estimates | Population level and growth |
| Census ACS 5-year *(free key)* | Income, tenure, units-in-structure, unemployment |
| BEA Regional *(free key)* | Metro GDP and growth |
| HUD Fair Market Rents *(optional key)* | Administratively-set rents by bedroom count |
| **FEMA National Risk Index** | Natural-hazard expected annual loss by county, rolled to metro |
| **Regional / municipal portals** | Sub-metro permit velocity from MPO and city open-data feeds (Maricopa Association of Governments, Phoenix, Austin, NYC, Chicago, LA, Seattle, SF, Denver, Nashville) |
| State policy table *(curated)* | Property tax burden, rent-control regime, landlord-tenant posture, land-use reform, insurance stress |
| MLS via RESO Web API *(licensed)* | Listing-level price, days on market, sale-to-list — **inactive without credentials** |

### About MLS

There is no public MLS feed. Every MLS licenses data through a broker/agent
agreement, exposed as a RESO Web API endpoint. `pipeline/sources/market.py`
implements that standard interface; set `MLS_RESO_BASE_URL` and
`MLS_RESO_TOKEN` (optionally `MLS_RESO_FILTER`) as repository secrets to
activate it. Listing data is aggregated to metro medians and never republished
row-by-row. Until then the dashboard uses transaction-based FHFA data, Census
permits and Zillow inventory/days-on-market as the closest public equivalents,
and says so on the page.

## Asset classes

Residential — single-family, townhomes & attached, duplex & 2-4 unit,
apartments (5+). Commercial — industrial & warehouse, data centers, retail,
hotels, restaurants, office. Plus land & development.

Two ideas drive the scoring:

**Net absorption, not raw demand.** Income-producing classes are scored on
demand growth *minus* supply pressure, where supply pressure is permits in that
structure type against the existing stock of the same type. Booming jobs plus a
flood of deliveries is not the same investment as booming jobs with an empty
pipeline.

**Land flips the sign.** For a developer, heavy permitting and a permissive
entitlement climate are the product, not the risk — so the land class rewards
the exact signal that penalises apartment owners.

Every input is percentile-ranked across all metros before combining, so
measures in different units (percent job growth, permits per capita, cents per
kilowatt-hour) contribute on one comparable scale. A score of 80 means the
metro sits in the 80th percentile for that class.

## Repository layout

```
index.html                     the dashboard (static, no build step)
data/market-data.json          model output, committed by CI
pipeline/build_data.py         orchestrator: joins sources, scores, writes JSON
pipeline/model.py              forecast + per-asset-class scoring
pipeline/make_fixtures.py      synthetic fixtures for offline testing
pipeline/sources/
  common.py                    fetching, CSV/XLSX/ZIP parsing, CBSA matching
  market.py                    Zillow, FHFA, HUD, MLS
  econ.py                      QCEW, population, ACS, BEA
  development.py               building permits, FEMA, regional portals
  policy.py                    curated state policy & political risk table
.github/workflows/             daily refresh
```

## Running it

```bash
# offline, against synthetic fixtures (no network needed)
python pipeline/make_fixtures.py /tmp/fx
LOCAL_FIXTURE_DIR=/tmp/fx python pipeline/build_data.py

# live
python pipeline/build_data.py
```

Standard library only — no dependencies to install.

## Optional repository secrets

Each is optional; a missing key disables exactly one source and is reported on
the dashboard rather than failing the run.

| Secret | Enables | Get one |
|---|---|---|
| `CENSUS_API_KEY` | ACS income, tenure, structure type, unemployment | https://api.census.gov/data/key_signup.html |
| `BEA_API_KEY` | Metro GDP | https://apps.bea.gov/API/signup/ |
| `HUD_API_TOKEN` | Fair Market Rents via API | https://www.huduser.gov/portal/dataset/fmr-api.html |
| `SOCRATA_APP_TOKEN` | Higher rate limits on municipal portals | https://evergreen.data.socrata.com/signup |
| `MLS_RESO_BASE_URL`, `MLS_RESO_TOKEN` | Licensed MLS listing data | your MLS / broker IDX agreement |

## Refresh

`.github/workflows/refresh-real-estate-data.yml` runs daily at 10:23 UTC (and
on demand via *Run workflow*), rebuilds `data/market-data.json` and commits it.
The page itself re-checks for a newer model run every 15 minutes.

## Caveats

Commercial classes have no public price or cap-rate index, so they are scored
on demand fundamentals rather than returns — a ranking of market conditions,
not a yield estimate. Metro-level indexes hide neighborhood variation, ACS
figures are 5-year survey estimates, and QCEW publishes with roughly a
nine-month lag. Research tool, not investment advice.
