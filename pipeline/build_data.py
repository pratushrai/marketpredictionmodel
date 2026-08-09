#!/usr/bin/env python3
"""Build data/market-data.json for the real-estate growth dashboard.

Joins a dozen public sources onto one metro spine (Zillow's CBSA list), scores
every metro for eleven asset classes, and writes a single JSON the static
dashboard reads. Standard library only — the refresh workflow installs nothing.

Every source is optional except ZHVI. Failures are recorded per source with a
diagnostic excerpt and the run continues, so one agency's outage degrades the
dashboard rather than breaking it.

    LOCAL_FIXTURE_DIR=... python pipeline/build_data.py    # offline test
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import model  # noqa: E402
from pipeline.sources import development, econ, market, policy  # noqa: E402
from pipeline.sources.common import (CbsaIndex, SourceError, cagr, fetch,  # noqa: E402
                                     load_county_to_cbsa, median,
                                     percentile_ranks, read_csv_rows,
                                     read_zip_member, to_float, write_json)

OUT_PATH = Path(__file__).resolve().parents[1] / "data" / "market-data.json"
# Per-source last-known-good values. Kept separate from the published payload
# because the payload itself gets overwritten by a bad run, and an outage that
# lasts two days would otherwise destroy the very data needed to ride it out.
LAST_GOOD_PATH = Path(__file__).resolve().parents[1] / "data" / "last-good.json"

GAZETTEER_URLS = [
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_cbsa_national.zip",
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2023_Gazetteer/2023_Gaz_cbsa_national.zip",
]

SOURCE_LABELS = {
    "zhvi": "Zillow ZHVI (home values)",
    "zori": "Zillow ZORI (rents)",
    "inventory": "Zillow for-sale inventory",
    "days_on_market": "Zillow days-to-pending",
    "fhfa": "FHFA repeat-sales HPI",
    "hud_fmr": "HUD Fair Market Rents",
    "mls": "MLS (RESO Web API)",
    "population": "Census population estimates",
    "acs": "Census ACS 5-year",
    "qcew": "BLS QCEW employment by sector",
    "bea_gdp": "BEA metro GDP",
    "permits": "Census building permits by structure type",
    "hazard": "FEMA National Risk Index",
    "gazetteer": "Census CBSA coordinates",
    "delineation": "Census county-CBSA delineation",
    "local_portals": "Regional & municipal permit portals",
    "policy": "State policy & political risk (curated)",
}


# When a source fails, the fields it owns are carried forward from the last
# good run rather than left empty. A single upstream outage would otherwise
# blank most of the dashboard — losing BLS, for example, drops the demand
# driver behind ten of the eleven asset classes.
CARRY_FORWARD = {
    "qcew": ["sectors", "empGrowth", "empGrowth3a", "employment", "avgWage"],
    "permits": ["permitYear", "permitUnits", "permits_unit1", "permits_unit2",
                "permits_unit34", "permits_unit5p", "permitGrowth1", "permitGrowth3a"],
    "fhfa": ["fhfaIndex", "fhfaG1", "fhfaG5a", "fhfaAsof"],
    "acs": ["income", "medianGrossRent", "censusHomeValue", "medianAge",
            "unemployment", "renterShare", "households", "housingUnits",
            "stockSingleFamily", "stockTownhome", "stockSmallMulti",
            "stockApartment", "incomeGrowth"],
    "bea_gdp": ["gdp", "gdpGrowth"],
    "hazard": ["hazardRisk", "hazardEAL", "communityResilience"],
    "hud_fmr": ["fmr1br", "fmr2br", "fmr3br"],
    "population": ["pop", "popGrowth", "popGrowth1"],
    "gazetteer": ["lat", "lon"],
}
# Beyond this the values are too old to stand in for live data.
MAX_CARRY_DAYS = 45


def load_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return None


def update_last_good(store, metros, status):
    """Snapshot the fields of every source that succeeded on this run."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for name, fields in CARRY_FORWARD.items():
        if not (status.get(name) or {}).get("ok"):
            continue
        snapshot = {}
        for m in metros:
            vals = {f: m[f] for f in fields if m.get(f) is not None}
            if vals:
                snapshot[m["id"]] = vals
        if snapshot:
            store[name] = {"generatedAt": stamp, "metros": snapshot}
    return store


def carry_forward(store, metros, status):
    """Fill gaps left by failed sources from the last-known-good snapshot.

    Only fills gaps — never overwrites a value this run produced — and records
    the age so the dashboard can label the source stale rather than passing old
    numbers off as fresh. Snapshots older than MAX_CARRY_DAYS are dropped.
    """
    now = datetime.now(timezone.utc)
    by_id = {m["id"]: m for m in metros}
    for name, fields in CARRY_FORWARD.items():
        entry = status.get(name)
        if not entry or entry.get("ok"):
            continue
        snap = store.get(name)
        if not snap or not snap.get("metros"):
            continue
        try:
            stamp = datetime.strptime(snap["generatedAt"],
                                      "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        age = now - stamp
        if age > timedelta(days=MAX_CARRY_DAYS):
            entry["staleDropped"] = f"last good data is {age.days}d old (>{MAX_CARRY_DAYS}d)"
            continue
        filled = 0
        for mid, vals in snap["metros"].items():
            m = by_id.get(mid)
            if not m:
                continue
            touched = False
            for f, v in vals.items():
                if f in fields and m.get(f) is None:
                    m[f] = v
                    touched = True
            filled += 1 if touched else 0
        if filled:
            entry["state"] = "stale"
            entry["carriedFrom"] = snap["generatedAt"]
            entry["carriedAgeHours"] = round(age.total_seconds() / 3600, 1)
            entry["metrosCarried"] = filled
            print(f"note: {name} failed; carried {filled} metros forward from "
                  f"{snap['generatedAt']}", file=sys.stderr)


class Runner:
    """Runs each source, records status, never lets one failure end the run."""

    def __init__(self):
        self.status = {}

    def run(self, name, fn, required=False, **kw):
        try:
            result = fn(**kw)
        except SourceError as e:
            return self._fail(name, str(e), required)
        except Exception as e:  # noqa: BLE001
            return self._fail(name, f"{type(e).__name__}: {e}", required)
        value, meta = result if isinstance(result, tuple) else (result, None)
        entry = {"ok": True, "label": SOURCE_LABELS.get(name, name)}
        if isinstance(meta, dict):
            entry.update({k: v for k, v in meta.items() if k != "urls"})
        elif isinstance(meta, str):
            entry["url"] = meta
        self.status[name] = entry
        return value

    def _fail(self, name, message, required):
        inactive = message.startswith("inactive")
        self.status[name] = {
            "ok": False,
            "label": SOURCE_LABELS.get(name, name),
            "state": "needs credentials" if inactive else "failed",
            "error": message[:400],
        }
        print(f"{'note' if inactive else 'warning'}: {name}: {message[:200]}",
              file=sys.stderr)
        if required:
            print(f"FATAL: required source {name} unavailable", file=sys.stderr)
            sys.exit(1)
        return None


# ------------------------------------------------------------- helpers -----

def value_at(series, months_back):
    return series[-1 - months_back][1] if len(series) > months_back else None


def pct_change(series, months_back):
    v0, v1 = value_at(series, months_back), series[-1][1]
    return (v1 / v0 - 1) if v0 and v0 > 0 else None


def annualized(series, months_back):
    return cagr(value_at(series, months_back), series[-1][1], months_back / 12)


def fetch_gazetteer():
    """CBSA centroid coordinates, keyed by CBSA code."""
    raw, url = fetch(GAZETTEER_URLS, fixture="gazetteer.zip")
    text = read_zip_member(raw, suffix=(".txt",))
    out = {}
    for row in read_csv_rows(text, delimiter="\t"):
        clean = {(k or "").strip().upper(): (v or "").strip() for k, v in row.items()}
        code = clean.get("GEOID", "")
        lat, lon = to_float(clean.get("INTPTLAT")), to_float(clean.get("INTPTLONG"))
        if code and lat is not None and lon is not None:
            out[code.zfill(5)] = (round(lat, 4), round(lon, 4))
    if not out:
        raise SourceError("gazetteer produced no rows")
    return out, url


def r(v, places=4):
    return round(v, places) if isinstance(v, float) else v


def main():
    run = Runner()
    last_good = load_json(LAST_GOOD_PATH) or {}

    # --- spine: Zillow metro list ---------------------------------------
    zhvi = run.run("zhvi", market.fetch_zillow, required=True, kind="zhvi")
    zori = run.run("zori", market.fetch_zillow, kind="zori") or []
    inventory = run.run("inventory", market.fetch_zillow, kind="inventory") or []
    dom = run.run("days_on_market", market.fetch_zillow, kind="days_on_market") or []

    zori_by_id = {x["id"]: x for x in zori}
    inv_by_id = {x["id"]: x for x in inventory}
    dom_by_id = {x["id"]: x for x in dom}

    # --- CBSA code resolution -------------------------------------------
    population = run.run("population", econ.fetch_population) or {}
    index = CbsaIndex()
    # Metros before micros, so a shared city prefix resolves to the metro.
    for code, rec in sorted(population.items(),
                            key=lambda kv: "Micro" in (kv[1].get("name") or "")):
        index.add(code, code=code, name=rec.get("name"))

    metros = []
    for z in zhvi:
        s = z["series"]
        if len(s) < 24:
            continue
        m = {
            "id": z["id"], "name": z["name"], "state": z["state"],
            "cbsa": index.lookup(name=z["name"], state=z["state"]),
            "sizeRank": z["sizeRank"],
            "zhvi": round(s[-1][1]), "asof": s[-1][0],
            "g3m": pct_change(s, 3), "g6a": annualized(s, 6), "g1": pct_change(s, 12),
            "g3a": annualized(s, 36), "g5a": annualized(s, 60),
        }
        zr = zori_by_id.get(z["id"])
        if zr:
            rent = zr["series"][-1][1]
            m["rent"] = round(rent)
            m["rentYield"] = rent * 12 / s[-1][1]
            m["rentG1"] = pct_change(zr["series"], 12)
        iv = inv_by_id.get(z["id"])
        if iv:
            m["inventory"] = round(iv["series"][-1][1])
            m["inventoryG1"] = pct_change(iv["series"], 12)
        dm = dom_by_id.get(z["id"])
        if dm:
            m["daysOnMarket"] = round(dm["series"][-1][1], 1)
        hist = s[-121:]
        m["series"] = [[d, round(v)] for d, v in hist[::3]]
        if m["series"][-1][0] != hist[-1][0]:
            m["series"].append([hist[-1][0], round(hist[-1][1])])
        metros.append(m)

    by_cbsa = {}
    for m in metros:
        if m["cbsa"]:
            by_cbsa.setdefault(m["cbsa"], m)

    def apply(source, mapper):
        """Join a {cbsa: record} source onto the metro spine."""
        if not source:
            return 0
        hits = 0
        for code, rec in source.items():
            m = by_cbsa.get(str(code).zfill(5))
            if m is None:
                continue
            mapper(m, rec)
            hits += 1
        return hits

    # --- population, coordinates ----------------------------------------
    apply(population, lambda m, rec: m.update({
        "pop": round(rec["pop"]), "popGrowth": rec.get("popGrowth"),
        "popGrowth1": rec.get("popGrowth1")}))

    coords = run.run("gazetteer", fetch_gazetteer) or {}
    apply(coords, lambda m, rec: m.update({"lat": rec[0], "lon": rec[1]}))

    # --- independent price / rent signals -------------------------------
    fhfa = run.run("fhfa", market.fetch_fhfa_hpi) or {}
    apply(fhfa, lambda m, rec: m.update({
        "fhfaIndex": rec["index"], "fhfaG1": rec.get("g1"),
        "fhfaG5a": rec.get("g5a"), "fhfaAsof": rec.get("asof")}))

    fmr = run.run("hud_fmr", market.fetch_hud_fmr) or {}
    apply(fmr, lambda m, rec: m.update({
        "fmr2br": rec.get("fmr2"), "fmr1br": rec.get("fmr1"),
        "fmr3br": rec.get("fmr3")}))

    # MLS: licensed feed, active only when credentials are configured.
    mls_raw = run.run("mls", market.fetch_mls)
    if mls_raw:
        summary = market.summarize_mls(mls_raw)
        city_index = CbsaIndex()
        for m in metros:
            city_index.add(m["id"], name=m["name"], state=m["state"])
        by_id = {m["id"]: m for m in metros}
        matched = 0
        for (city, state), rec in summary.items():
            hit = city_index.lookup(name=f"{city}, {state}", state=state)
            if hit and hit in by_id:
                by_id[hit].update(rec)
                matched += 1
        run.status["mls"]["metrosMatched"] = matched

    # --- demographics & economy -----------------------------------------
    acs = run.run("acs", econ.fetch_acs) or {}
    apply(acs, lambda m, rec: m.update(econ.shape_acs(rec)))

    qcew = run.run("qcew", econ.fetch_qcew) or {}

    def add_sectors(m, rec):
        m["sectors"] = rec
        total = rec.get("total") or {}
        if total.get("g1") is not None:
            m["empGrowth"] = total["g1"]
        if total.get("g3a") is not None:
            m["empGrowth3a"] = total["g3a"]
        if total.get("emp"):
            m["employment"] = round(total["emp"])
        if total.get("wage"):
            m["avgWage"] = round(total["wage"])

    apply(qcew, add_sectors)

    bea = run.run("bea_gdp", econ.fetch_bea_gdp) or {}
    apply(bea, lambda m, rec: m.update({
        "gdp": round(rec["gdp"]), "gdpGrowth": rec.get("gdpGrowth")}))

    # --- development & risk ----------------------------------------------
    permits = run.run("permits", development.fetch_permits) or {}
    apply(permits, lambda m, rec: m.update(rec))

    county_map = run.run("delineation", load_county_to_cbsa) or {}
    if county_map:
        hazard = run.run("hazard", development.fetch_hazard_risk,
                         county_to_cbsa=county_map) or {}
        apply(hazard, lambda m, rec: m.update(rec))

    local = run.run("local_portals", development.fetch_local_portals) or {}
    apply(local, lambda m, rec: m.update({
        "localPermits": rec.get("localPermits"),
        "localPermitTrend": rec.get("localPermitTrend"),
        "localPortals": rec.get("localPortals")}))

    # --- state policy (curated, always available) ------------------------
    policy_hits = 0
    for m in metros:
        prof = policy.metro_policy(m["state"])
        if prof:
            m.update(prof)
            policy_hits += 1
    run.status["policy"] = {"ok": policy_hits > 0, "label": SOURCE_LABELS["policy"],
                            "metrosMatched": policy_hits, **policy.meta()}

    # --- last-known-good: snapshot what worked, backfill what did not -----
    update_last_good(last_good, metros, run.status)
    carry_forward(last_good, metros, run.status)

    # --- derived ratios ---------------------------------------------------
    for m in metros:
        if m.get("income"):
            m["pti"] = m["zhvi"] / m["income"]
        if m.get("permitUnits") is not None and m.get("pop"):
            m["permitIntensity"] = m["permitUnits"] / (m["pop"] / 1000.0)
        if m.get("rent") and m.get("fmr2br"):
            m["rentVsFmr"] = m["rent"] / m["fmr2br"]
        # The population-growth threshold called out in the brief.
        m["highGrowth"] = bool(m.get("popGrowth") is not None and m["popGrowth"] >= 0.02)

    # --- scoring ----------------------------------------------------------
    # Carried-forward sectors arrive already trimmed to growth rates, which is
    # exactly what the scorer reads, so no re-flattening is needed here.
    nat = model.national_baselines(metros)
    for m in metros:
        m["pred"] = model.forecast_growth(m, nat)
    model.score_all(metros)

    growth_pct = percentile_ranks([(m["id"], m["pred"]) for m in metros])
    pti_pct = percentile_ranks([(m["id"], m.get("pti")) for m in metros])
    price_pct = percentile_ranks([(m["id"], m["zhvi"]) for m in metros])
    yield_pct = percentile_ranks([(m["id"], m.get("rentYield")) for m in metros])

    for m in metros:
        gs = growth_pct.get(m["id"])
        m["growthScore"] = round(gs, 1) if gs is not None else None
        pti_r, price_r = pti_pct.get(m["id"]), price_pct.get(m["id"], 50)
        m["affordScore"] = round(100 - (0.6 * pti_r + 0.4 * price_r), 1) \
            if pti_r is not None else round(100 - price_r, 1)
        yr = yield_pct.get(m["id"], 50)
        m["buyHoldScore"] = round(0.50 * gs + 0.35 * m["affordScore"] + 0.15 * yr, 1) \
            if gs is not None else None

    metros.sort(key=lambda m: (-(m["buyHoldScore"] if m["buyHoldScore"] is not None else -1),
                               m["sizeRank"]))

    # --- trim & round for payload size ------------------------------------
    for m in metros:
        sectors = m.pop("sectors", None)
        if sectors:
            trimmed = {}
            for name, rec in sectors.items():
                keep = {k: r(rec[k]) for k in ("g1", "g3a") if rec.get(k) is not None}
                if keep:
                    trimmed[name] = keep
            if trimmed:
                m["sectors"] = trimmed
        for k, v in list(m.items()):
            if isinstance(v, float):
                m[k] = round(v, 5)

    nat["predMedian"] = median([m["pred"] for m in metros])
    coverage = {
        "metros": len(metros),
        "withCbsa": sum(1 for m in metros if m.get("cbsa")),
        "withPopulation": sum(1 for m in metros if m.get("pop")),
        "withCoords": sum(1 for m in metros if m.get("lat")),
        "withEmployment": sum(1 for m in metros if m.get("empGrowth") is not None),
        "withPermits": sum(1 for m in metros if m.get("permitUnits") is not None),
        "withIncome": sum(1 for m in metros if m.get("income")),
        "withHazard": sum(1 for m in metros if m.get("hazardRisk") is not None),
        "withFhfa": sum(1 for m in metros if m.get("fhfaIndex") is not None),
        "withPolicy": sum(1 for m in metros if m.get("politicalRisk") is not None),
        "withLocalPortal": sum(1 for m in metros if m.get("localPermits")),
        "staleSources": sorted(k for k, v in run.status.items()
                               if v.get("state") == "stale"),
        "highGrowth2pct": sum(1 for m in metros if m.get("highGrowth")),
    }

    out = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "refreshCadence": "daily",
        "model": model.model_meta(),
        "sources": run.status,
        "coverage": coverage,
        "national": {k: r(v, 5) for k, v in nat.items() if v is not None},
        "metros": metros,
    }
    write_json(LAST_GOOD_PATH, last_good)
    size = write_json(OUT_PATH, out)
    print(f"wrote {OUT_PATH} ({size/1024:.0f} KB)")
    print(f"  metros={coverage['metros']} employment={coverage['withEmployment']} "
          f"permits={coverage['withPermits']} income={coverage['withIncome']} "
          f"hazard={coverage['withHazard']} policy={coverage['withPolicy']} "
          f"highGrowth(2%+)={coverage['highGrowth2pct']}")
    ok = [k for k, v in run.status.items() if v.get("ok")]
    bad = [f"{k}({v.get('state')})" for k, v in run.status.items() if not v.get("ok")]
    print(f"  sources ok: {', '.join(sorted(ok))}")
    if bad:
        print(f"  sources unavailable: {', '.join(sorted(bad))}")


if __name__ == "__main__":
    main()
