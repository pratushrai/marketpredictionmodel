#!/usr/bin/env python3
"""Build market-data.json for the real-estate growth dashboard.

Pulls public, keyless data sources:
  * Zillow Research ZHVI (smoothed, seasonally-adjusted home value index, metro)
  * Zillow Research ZORI (observed rent index, metro)
  * US Census ACS 5-year (income, population, rent, unemployment) for 2023 and
    2018 vintages, so 5-year population/income growth can be computed
  * US Census gazetteer (CBSA centroid coordinates for the map)

Then computes, per metro, a transparent momentum + fundamentals composite:
12-month appreciation forecast, growth score, affordability score, and a
buy-and-hold score. Output: real-estate/data/market-data.json

Only ZHVI is a hard requirement; every other source degrades gracefully and
its status is recorded in the output so the dashboard can show provenance.

Offline testing: set LOCAL_FIXTURE_DIR to a directory containing files named
by the FIXTURE_NAMES mapping below and no network access is attempted.
"""

import csv
import io
import json
import math
import os
import re
import sys
import time
import zipfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "data" / "market-data.json"

USER_AGENT = "real-estate-dashboard/1.0 (github.com/pratushrai/marketpredictionmodel)"

ZHVI_URLS = [
    "https://files.zillowstatic.com/research/public_csvs/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
    "https://files.zillowstatic.com/research/public_csvs/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_month.csv",
]
ZORI_URLS = [
    "https://files.zillowstatic.com/research/public_csvs/zori/Metro_zori_uc_sfrcondomfr_sm_sa_month.csv",
    "https://files.zillowstatic.com/research/public_csvs/zori/Metro_zori_uc_sfrcondomfr_sm_month.csv",
]
INVENTORY_URLS = [
    "https://files.zillowstatic.com/research/public_csvs/invt_fs/Metro_invt_fs_uc_sfrcondo_sm_month.csv",
]
CENSUS_VARS_NOW = "NAME,B19013_001E,B01003_001E,B25064_001E,B25077_001E,B23025_003E,B23025_005E"
CENSUS_VARS_PAST = "NAME,B01003_001E,B19013_001E"
MSA_FOR = "metropolitan%20statistical%20area/micropolitan%20statistical%20area:*"
CENSUS_NOW_URLS = [
    f"https://api.census.gov/data/2023/acs/acs5?get={CENSUS_VARS_NOW}&for={MSA_FOR}",
    f"https://api.census.gov/data/2022/acs/acs5?get={CENSUS_VARS_NOW}&for={MSA_FOR}",
]
CENSUS_PAST_URLS = [
    f"https://api.census.gov/data/2018/acs/acs5?get={CENSUS_VARS_PAST}&for={MSA_FOR}",
    f"https://api.census.gov/data/2017/acs/acs5?get={CENSUS_VARS_PAST}&for={MSA_FOR}",
]
POPEST_URLS = [
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/metro/totals/cbsa-est2024-alldata.csv",
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-2023/metro/totals/cbsa-est2023-alldata.csv",
]
GAZETTEER_URLS = [
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_cbsa_national.zip",
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_cbsa_national.zip",
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2022_Gazetteer/2022_Gaz_cbsa_national.zip",
]

FIXTURE_NAMES = {
    "zhvi": "zhvi.csv",
    "zori": "zori.csv",
    "inventory": "inventory.csv",
    "census_now": "census_now.json",
    "census_past": "census_past.json",
    "gazetteer": "gazetteer.zip",
    "popest": "popest.csv",
}

FIXTURE_DIR = os.environ.get("LOCAL_FIXTURE_DIR")


CENSUS_KEY = os.environ.get("CENSUS_API_KEY", "").strip()


def with_census_key(url):
    if CENSUS_KEY and "api.census.gov" in url:
        return url + "&key=" + CENSUS_KEY
    return url


def fetch_bytes(urls, fixture_key, attempts=3):
    """Return (bytes, url_used). Tries each URL with retries and backoff."""
    if FIXTURE_DIR:
        p = Path(FIXTURE_DIR) / FIXTURE_NAMES[fixture_key]
        return p.read_bytes(), str(p)
    last_err = None
    for url in urls:
        url = with_census_key(url)
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/csv, */*",
                })
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return resp.read(), resp.geturl()
            except Exception as e:  # noqa: BLE001 - record and try next
                last_err = e
                time.sleep(2 ** attempt)
    raise RuntimeError(f"all URLs failed for {fixture_key}: {last_err}")


DASHES = str.maketrans({"–": "-", "—": "-", "−": "-"})


def norm_city(city):
    city = city.translate(DASHES).lower()
    city = re.sub(r"[^a-z\- ]", "", city)
    return re.sub(r"\s+", " ", city).strip()


def census_name_parts(name):
    """'Austin-Round Rock-San Marcos, TX Metro Area' -> (city candidates, state candidates, kind)."""
    name = name.translate(DASHES)
    kind = "micro" if "Micro Area" in name else "metro"
    base = re.sub(r"\s+(Metro|Micro) Area$", "", name).strip()
    city_part, _, state_part = base.rpartition(",")
    if not city_part:
        return [], [], kind
    city_part = city_part.split("/")[0].strip()
    pieces = [p.strip() for p in re.split(r"-+", city_part) if p.strip()]
    # progressive prefixes handle both hyphenated names (Winston-Salem) and
    # multi-city lists (Austin-Round Rock)
    candidates = ["-".join(pieces[: i + 1]) for i in range(len(pieces))]
    candidates += [pieces[0]]
    states = [s.strip() for s in state_part.strip().split("-") if s.strip()]
    return candidates, states, kind


def parse_zillow_csv(raw):
    """Zillow wide CSV -> list of {id, name, city, state, sizeRank, dates, values}."""
    text = raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    idx = {h: i for i, h in enumerate(header)}
    date_cols = [(i, h) for i, h in enumerate(header) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", h)]
    rows = []
    for row in reader:
        if idx.get("RegionType") is not None and row[idx["RegionType"]] != "msa":
            continue
        region = row[idx["RegionName"]]
        state = row[idx["StateName"]].strip() if "StateName" in idx else ""
        city = region.rsplit(",", 1)[0].strip()
        series = []
        for i, h in date_cols:
            v = row[i].strip()
            if v:
                try:
                    series.append((h[:7], float(v)))
                except ValueError:
                    pass
        if not series:
            continue
        rows.append({
            "id": row[idx["RegionID"]],
            "name": region,
            "city": city,
            "state": state,
            "sizeRank": int(row[idx["SizeRank"]] or 10**9),
            "series": series,
        })
    return rows


def parse_census(raw):
    data = json.loads(raw.decode("utf-8"))
    header, rows = data[0], data[1:]
    out = []
    for r in rows:
        rec = dict(zip(header, r))
        clean = {}
        for k, v in rec.items():
            if k == "NAME" or v is None:
                clean[k] = v
                continue
            try:
                f = float(v)
                clean[k] = None if f <= -666666 else f
            except (TypeError, ValueError):
                clean[k] = v
        out.append(clean)
    return out


def parse_popest(raw):
    """PEP CBSA totals CSV -> {census-style name: (pop_latest, pop_growth_annualized)}."""
    text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    out = {}
    for row in reader:
        lsad = (row.get("LSAD") or "").strip()
        if lsad not in ("Metropolitan Statistical Area", "Micropolitan Statistical Area"):
            continue
        name = (row.get("NAME") or "").strip()
        years = sorted(int(k[-4:]) for k in row if k.startswith("POPESTIMATE") and k[-4:].isdigit())
        if not name or len(years) < 2:
            continue
        try:
            p0 = float(row[f"POPESTIMATE{years[0]}"])
            p1 = float(row[f"POPESTIMATE{years[-1]}"])
        except (TypeError, ValueError):
            continue
        if p0 <= 0 or p1 <= 0:
            continue
        growth = (p1 / p0) ** (1 / (years[-1] - years[0])) - 1
        suffix = " Metro Area" if lsad.startswith("Metropolitan") else " Micro Area"
        out[name + suffix] = (p1, growth)
    return out


def parse_gazetteer(raw):
    """CBSA gazetteer zip -> {normalized name: (lat, lon)} keyed like census names."""
    coords = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        txt_name = next(n for n in zf.namelist() if n.lower().endswith(".txt"))
        text = zf.read(txt_name).decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = [h.strip() for h in next(reader)]
    idx = {h.upper(): i for i, h in enumerate(header)}
    for row in reader:
        try:
            name = row[idx["NAME"]].strip()
            lat = float(row[idx["INTPTLAT"]].strip())
            lon = float(row[idx["INTPTLONG"]].strip())
        except (KeyError, IndexError, ValueError):
            continue
        coords[name] = (lat, lon)
    return coords


def build_lookup(census_style_names):
    """Map (norm city candidate, state) -> original name, for census/gazetteer names."""
    lookup = {}
    for name in census_style_names:
        cands, states, kind = census_name_parts(name)
        for c in cands:
            for s in states:
                key = (norm_city(c), s.upper())
                # metros win over micros when keys collide
                if key not in lookup or (kind == "metro" and lookup[key][1] == "micro"):
                    lookup[key] = (name, kind)
    return lookup


def match_metro(city, state, lookup):
    key = (norm_city(city), state.upper())
    hit = lookup.get(key)
    return hit[0] if hit else None


def value_at_offset(series, months_back):
    """series is [(YYYY-MM, value)] ascending; return value ~months_back before latest."""
    if len(series) <= months_back:
        return None
    return series[-1 - months_back][1]


def pct_change(series, months_back):
    v0 = value_at_offset(series, months_back)
    v1 = series[-1][1]
    if not v0 or v0 <= 0:
        return None
    return v1 / v0 - 1


def annualized(series, months_back):
    total = pct_change(series, months_back)
    if total is None:
        return None
    years = months_back / 12
    base = 1 + total
    if base <= 0:
        return None
    return base ** (1 / years) - 1


def median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def percentile_ranks(pairs):
    """pairs: [(key, value)] -> {key: 0..100 percentile of value}."""
    valid = sorted((v, k) for k, v in pairs if v is not None)
    n = len(valid)
    ranks = {}
    for i, (_, k) in enumerate(valid):
        ranks[k] = 100 * i / (n - 1) if n > 1 else 50
    return ranks


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


MODEL_VERSION = "1.0"
MODEL_WEIGHTS = {
    "momentum_1y": 0.40,
    "momentum_6m_annualized": 0.25,
    "trend_3y_annualized": 0.15,
    "trend_5y_annualized": 0.10,
    "population_growth_tilt": 1.2,
    "rent_yield_tilt": 0.5,
    "overvaluation_drag_per_pti_unit": 0.008,
    "unemployment_drag": 0.20,
}


def predict_growth(m, nat):
    """12-month ZHVI appreciation forecast: damped momentum blend + fundamentals tilts."""
    comps = [
        (MODEL_WEIGHTS["momentum_1y"], m.get("g1")),
        (MODEL_WEIGHTS["momentum_6m_annualized"], m.get("g6a")),
        (MODEL_WEIGHTS["trend_3y_annualized"], m.get("g3a")),
        (MODEL_WEIGHTS["trend_5y_annualized"], m.get("g5a")),
    ]
    used = [(w, v) for w, v in comps if v is not None]
    if not used:
        return None
    # missing components shrink the estimate toward zero on purpose (damping)
    pred = sum(w * v for w, v in used)
    if m.get("popGrowth") is not None and nat.get("popGrowth") is not None:
        pred += MODEL_WEIGHTS["population_growth_tilt"] * (m["popGrowth"] - nat["popGrowth"])
    if m.get("rentYield") is not None and nat.get("rentYield") is not None:
        pred += MODEL_WEIGHTS["rent_yield_tilt"] * clamp(m["rentYield"] - nat["rentYield"], -0.03, 0.03)
    if m.get("pti") is not None and nat.get("pti") is not None and m["pti"] > nat["pti"]:
        pred -= MODEL_WEIGHTS["overvaluation_drag_per_pti_unit"] * (m["pti"] - nat["pti"])
    if m.get("unemployment") is not None and nat.get("unemployment") is not None:
        pred -= MODEL_WEIGHTS["unemployment_drag"] * (m["unemployment"] - nat["unemployment"])
    return clamp(pred, -0.10, 0.15)


def main():
    sources = {}

    def load(key, urls, parser, required=False):
        raw = None
        try:
            raw, used = fetch_bytes(urls, key)
            parsed = parser(raw)
            sources[key] = {"ok": True, "url": used}
            return parsed
        except Exception as e:  # noqa: BLE001
            err = str(e)[:300]
            if raw is not None:
                err += " | body head: " + raw[:220].decode("utf-8", errors="replace")
            sources[key] = {"ok": False, "error": err}
            e = RuntimeError(err)
            if required:
                print(f"FATAL: required source {key} failed: {e}", file=sys.stderr)
                sys.exit(1)
            print(f"warning: optional source {key} failed: {e}", file=sys.stderr)
            return None

    zhvi = load("zhvi", ZHVI_URLS, parse_zillow_csv, required=True)
    zori = load("zori", ZORI_URLS, parse_zillow_csv) or []
    inventory = load("inventory", INVENTORY_URLS, parse_zillow_csv) or []
    census_now = load("census_now", CENSUS_NOW_URLS, parse_census) or []
    census_past = load("census_past", CENSUS_PAST_URLS, parse_census) or []
    gaz = load("gazetteer", GAZETTEER_URLS, parse_gazetteer) or {}
    popest = load("popest", POPEST_URLS, parse_popest) or {}

    sources["zhvi"]["asof"] = zhvi[0]["series"][-1][0] if zhvi else None

    zori_by_id = {r["id"]: r for r in zori}
    inv_by_id = {r["id"]: r for r in inventory}
    census_now_by_name = {r["NAME"]: r for r in census_now if r.get("NAME")}
    census_past_by_name = {r["NAME"]: r for r in census_past if r.get("NAME")}
    now_lookup = build_lookup(census_now_by_name.keys())
    past_lookup = build_lookup(census_past_by_name.keys())
    gaz_lookup = build_lookup(gaz.keys())
    popest_lookup = build_lookup(popest.keys())

    metros = []
    for z in zhvi:
        s = z["series"]
        if len(s) < 24:
            continue
        m = {
            "id": z["id"],
            "name": z["name"],
            "state": z["state"],
            "sizeRank": z["sizeRank"],
            "zhvi": round(s[-1][1]),
            "asof": s[-1][0],
            "g3m": pct_change(s, 3),
            "g6a": annualized(s, 6),
            "g1": pct_change(s, 12),
            "g3a": annualized(s, 36),
            "g5a": annualized(s, 60),
        }

        zr = zori_by_id.get(z["id"])
        if zr and zr["series"]:
            rent = zr["series"][-1][1]
            m["rent"] = round(rent)
            m["rentYield"] = rent * 12 / s[-1][1]
            m["rentG1"] = pct_change(zr["series"], 12)

        iv = inv_by_id.get(z["id"])
        if iv and iv["series"]:
            m["inventory"] = round(iv["series"][-1][1])
            m["inventoryG1"] = pct_change(iv["series"], 12)

        now_name = match_metro(z["city"], z["state"], now_lookup)
        if now_name:
            c = census_now_by_name[now_name]
            m["income"] = c.get("B19013_001E")
            m["pop"] = c.get("B01003_001E")
            m["medianGrossRent"] = c.get("B25064_001E")
            m["censusHomeValue"] = c.get("B25077_001E")
            lf, un = c.get("B23025_003E"), c.get("B23025_005E")
            if lf and un is not None and lf > 0:
                m["unemployment"] = un / lf
            if m.get("income"):
                m["pti"] = s[-1][1] / m["income"]
            past_name = match_metro(z["city"], z["state"], past_lookup)
            if past_name:
                p = census_past_by_name[past_name]
                pop0, pop1 = p.get("B01003_001E"), m.get("pop")
                if pop0 and pop1 and pop0 > 0:
                    m["popGrowth"] = (pop1 / pop0) ** (1 / 5) - 1
                inc0, inc1 = p.get("B19013_001E"), m.get("income")
                if inc0 and inc1 and inc0 > 0:
                    m["incomeGrowth"] = (inc1 / inc0) ** (1 / 5) - 1

        if m.get("pop") is None:
            pe_name = match_metro(z["city"], z["state"], popest_lookup)
            if pe_name:
                m["pop"], m["popGrowth"] = round(popest[pe_name][0]), popest[pe_name][1]

        gaz_name = match_metro(z["city"], z["state"], gaz_lookup)
        if gaz_name:
            lat, lon = gaz[gaz_name]
            m["lat"], m["lon"] = round(lat, 4), round(lon, 4)

        # quarterly ZHVI history, last ~10 years, for the detail sparkline
        hist = s[-121:]
        m["series"] = [[d, round(v)] for d, v in hist[::3]]
        if hist[-1][0] != m["series"][-1][0]:
            m["series"].append([hist[-1][0], round(hist[-1][1])])

        metros.append(m)

    nat = {
        "zhvi": median([m["zhvi"] for m in metros]),
        "g1": median([m["g1"] for m in metros]),
        "g3a": median([m["g3a"] for m in metros]),
        "rentYield": median([m.get("rentYield") for m in metros]),
        "pti": median([m.get("pti") for m in metros]),
        "popGrowth": median([m.get("popGrowth") for m in metros]),
        "unemployment": median([m.get("unemployment") for m in metros]),
    }

    for m in metros:
        m["pred"] = predict_growth(m, nat)

    growth_pct = percentile_ranks([(m["id"], m["pred"]) for m in metros])
    pti_pct = percentile_ranks([(m["id"], m.get("pti")) for m in metros])
    price_pct = percentile_ranks([(m["id"], m["zhvi"]) for m in metros])
    yield_pct = percentile_ranks([(m["id"], m.get("rentYield")) for m in metros])

    for m in metros:
        gs = growth_pct.get(m["id"])
        m["growthScore"] = round(gs, 1) if gs is not None else None
        pti_r = pti_pct.get(m["id"])
        price_r = price_pct.get(m["id"], 50)
        if pti_r is not None:
            m["affordScore"] = round(100 - (0.6 * pti_r + 0.4 * price_r), 1)
        else:
            m["affordScore"] = round(100 - price_r, 1)
        yr = yield_pct.get(m["id"], 50)
        if gs is not None:
            m["buyHoldScore"] = round(0.50 * gs + 0.35 * m["affordScore"] + 0.15 * yr, 1)
        else:
            m["buyHoldScore"] = None

    metros.sort(key=lambda m: (-(m["buyHoldScore"] or -1), m["sizeRank"]))

    nat["predMedian"] = median([m["pred"] for m in metros])
    matched_census = sum(1 for m in metros if m.get("income"))
    matched_pop = sum(1 for m in metros if m.get("pop"))
    matched_coords = sum(1 for m in metros if m.get("lat"))

    out = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "refreshCadence": "daily",
        "model": {
            "version": MODEL_VERSION,
            "type": "momentum + fundamentals composite (heuristic, not a trained ML model)",
            "weights": MODEL_WEIGHTS,
        },
        "sources": sources,
        "coverage": {
            "metros": len(metros),
            "withCensus": matched_census,
            "withPopulation": matched_pop,
            "withCoords": matched_coords,
        },
        "national": {k: v for k, v in nat.items() if v is not None},
        "metros": metros,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, separators=(",", ":")) + "\n")
    size_kb = OUT_PATH.stat().st_size / 1024
    print(f"wrote {OUT_PATH} ({size_kb:.0f} KB): {len(metros)} metros, "
          f"{matched_census} with census data, {matched_coords} with coordinates")


if __name__ == "__main__":
    main()
