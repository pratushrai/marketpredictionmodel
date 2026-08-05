"""Supply, entitlement, and physical-risk signals.

Three layers, coarse to fine:

  1. Census Building Permits Survey (BPS) — every permit-issuing jurisdiction
     in the country, by structure type (1-unit / 2 / 3-4 / 5+). This is the
     pro-development signal: it measures what local government actually
     *approved*, split the same way the asset classes are.
  2. FEMA National Risk Index — natural-hazard expected annual loss by county,
     rolled up to metro. Physical risk feeds insurance cost and long-run value.
  3. Regional / municipal open-data portals (the "MAG-type" layer) — MPO and
     city permit feeds for sub-metro detail. Each portal is independent; a dead
     endpoint marks only itself unavailable.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from .common import (SourceError, cagr, fetch, read_csv_rows, read_zip_member,
                     to_float)

# --------------------------------------------- Census Building Permits -----

BPS_STRUCTURE_GROUPS = ["unit1", "unit2", "unit34", "unit5p"]


def _bps_urls(year):
    """Candidate BPS metro files for one year.

    Census has used several naming conventions; the December year-to-date file
    is the most reliably present and equals the annual total, so it is tried
    alongside the explicit annual files.
    """
    yy, base = str(year)[2:], "https://www2.census.gov/econ/bps/Metro"
    return [
        f"{base}/ma{year}a.txt",      # 4-digit annual
        f"{base}/ma{yy}a.txt",        # 2-digit annual
        f"{base}/ma{yy}12y.txt",      # December year-to-date == annual total
        f"{base}/ma{yy}12c.txt",      # December cumulative
    ]


def fetch_permits(years_back=6):
    """Building permits by metro and structure type.

    Returns ({cbsa: {units/bldgs by group, growth, per-capita inputs}}, meta).
    Annual files are used for the latest complete year plus lags, so permit
    *velocity* (not just level) can be scored.
    """
    now = datetime.now(timezone.utc).year
    base_year, by_year, used = None, {}, []

    for year in range(now, now - years_back, -1):
        try:
            text, url = fetch(_bps_urls(year), fixture=f"bps_{'cur' if base_year is None else year}.txt",
                              want="text", attempts=2)
        except SourceError:
            continue
        parsed = _parse_bps(text)
        if not parsed:
            continue
        if base_year is None:
            base_year, = (year,)
            used.append(url)
        by_year[year] = parsed
        if len(by_year) >= 5:
            break

    if not by_year:
        raise SourceError(f"BPS unavailable for {now-years_back+1}..{now}")

    years = sorted(by_year, reverse=True)
    latest, lag1 = years[0], years[1] if len(years) > 1 else None
    lag3 = years[3] if len(years) > 3 else years[-1] if len(years) > 1 else None

    out = {}
    for cbsa, rec in by_year[latest].items():
        total = sum(rec.get(g, {}).get("units", 0) or 0 for g in BPS_STRUCTURE_GROUPS)
        entry = {"permitYear": latest, "permitUnits": total}
        for g in BPS_STRUCTURE_GROUPS:
            entry[f"permits_{g}"] = rec.get(g, {}).get("units")
        prev = by_year.get(lag1, {}).get(cbsa) if lag1 else None
        if prev:
            prev_total = sum(prev.get(g, {}).get("units", 0) or 0 for g in BPS_STRUCTURE_GROUPS)
            entry["permitGrowth1"] = (total / prev_total - 1) if prev_total else None
        older = by_year.get(lag3, {}).get(cbsa) if lag3 else None
        if older and lag3 and lag3 != latest:
            old_total = sum(older.get(g, {}).get("units", 0) or 0 for g in BPS_STRUCTURE_GROUPS)
            entry["permitGrowth3a"] = cagr(old_total, total, latest - lag3)
        out[cbsa] = entry
    return out, {"year": latest, "years": years[:5], "urls": used}


def _parse_bps(text):
    """Parse a BPS metro file (two-row header, then CSV) -> {cbsa: groups}."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 3:
        return {}
    header_row = next((i for i, ln in enumerate(lines[:8])
                       if "CBSA" in ln.upper() and "NAME" in ln.upper()), None)
    if header_row is None:
        return {}
    cols = [c.strip().strip('"') for c in lines[header_row].split(",")]
    upper = [c.upper() for c in cols]
    try:
        cbsa_i = next(i for i, c in enumerate(upper) if c == "CBSA")
        name_i = next(i for i, c in enumerate(upper) if c == "NAME")
    except StopIteration:
        return {}

    # After Name, the file repeats (Bldgs, Units, Value) for 1-unit, 2-units,
    # 3-4 units and 5+ units. Data rows begin after the second header line.
    start = header_row + 1
    while start < len(lines) and not _looks_numeric(lines[start], cbsa_i):
        start += 1

    out = {}
    for ln in lines[start:]:
        parts = [p.strip().strip('"') for p in ln.split(",")]
        if len(parts) <= name_i + 12:
            continue
        code = parts[cbsa_i].strip()
        if not code.isdigit():
            continue
        code = code.zfill(5)
        vals = parts[name_i + 1:]
        groups = {}
        for gi, g in enumerate(BPS_STRUCTURE_GROUPS):
            base = gi * 3
            if base + 1 >= len(vals):
                break
            groups[g] = {"bldgs": to_float(vals[base]), "units": to_float(vals[base + 1])}
        if groups:
            out[code] = groups
    return out


def _looks_numeric(line, idx):
    parts = [p.strip().strip('"') for p in line.split(",")]
    return len(parts) > idx and parts[idx].isdigit()


# ------------------------------------------------------------- FEMA NRI ---

NRI_URLS = [
    "https://hazards.fema.gov/nri/Content/StaticDocuments/DataDownload/NRI_Table_Counties/NRI_Table_Counties.zip",
    "https://hazards.fema.gov/nri/Content/StaticDocuments/DataDownload/NRI_Table_CensusTracts/NRI_Table_CensusTracts.zip",
]
# FEMA's CDN rejects non-browser user agents with a 403.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, "
              "like Gecko) Chrome/126.0 Safari/537.36")


def fetch_hazard_risk(county_to_cbsa):
    """FEMA National Risk Index by county, population-weighted to metro.

    Returns {cbsa: {hazardRisk 0-100, hazardEAL $/yr, communityResilience}}.
    """
    raw, url = fetch(NRI_URLS, fixture="fema_nri.zip",
                     headers={"User-Agent": BROWSER_UA})
    text = read_zip_member(raw, suffix=(".csv",))
    agg = {}
    for row in read_csv_rows(text):
        fips = (row.get("STCOFIPS") or row.get("STCOFIPS_") or "").strip().zfill(5)
        hit = county_to_cbsa.get(fips)
        if not hit:
            continue
        cbsa = hit[0]
        pop = to_float(row.get("POPULATION")) or 0
        risk = to_float(row.get("RISK_SCORE"))
        eal = to_float(row.get("EAL_VALT")) or to_float(row.get("EAL_VALB"))
        resl = to_float(row.get("RESL_SCORE"))
        if risk is None:
            continue
        a = agg.setdefault(cbsa, {"w": 0.0, "risk": 0.0, "eal": 0.0, "resl": 0.0, "rw": 0.0})
        w = max(pop, 1.0)
        a["w"] += w
        a["risk"] += risk * w
        a["eal"] += eal or 0
        if resl is not None:
            a["resl"] += resl * w
            a["rw"] += w
    out = {}
    for cbsa, a in agg.items():
        if a["w"] <= 0:
            continue
        out[cbsa] = {
            "hazardRisk": round(a["risk"] / a["w"], 1),
            "hazardEAL": round(a["eal"]),
            "communityResilience": round(a["resl"] / a["rw"], 1) if a["rw"] else None,
        }
    if not out:
        raise SourceError("FEMA NRI produced no metro rows")
    return out, url


# ----------------------------------- Regional / municipal portals (MAG) ----

# Each entry describes one regional planning organisation or municipal open
# data portal. `kind` selects the query dialect:
#   socrata - SODA endpoint, aggregate via $select=count(1) and $where
#   arcgis  - ArcGIS FeatureServer, aggregate via returnCountOnly
# Endpoints move; a failing portal reports itself and nothing else breaks.
LOCAL_PORTALS = [
    {"id": "mag-maricopa", "org": "Maricopa Association of Governments",
     "cbsa": "38060", "kind": "arcgis",
     "url": "https://geo.azmag.gov/arcgis/rest/services/Regional_Data/MapServer/0/query",
     "date_field": None,
     "note": "MPO regional data layer (socioeconomic projections)"},
    {"id": "phoenix-permits", "org": "City of Phoenix", "cbsa": "38060",
     "kind": "arcgis",
     "url": "https://services6.arcgis.com/AmvIQTdvHRPnGwEA/arcgis/rest/services/Building_Permits/FeatureServer/0/query",
     "date_field": "ISSUEDDATE"},
    {"id": "austin-permits", "org": "City of Austin", "cbsa": "12420",
     "kind": "socrata", "url": "https://data.austintexas.gov/resource/3syk-w9eu.json",
     "date_field": "issued_date"},
    {"id": "nyc-dob-permits", "org": "NYC Dept. of Buildings", "cbsa": "35620",
     "kind": "socrata", "url": "https://data.cityofnewyork.us/resource/ipu4-2q9a.json",
     "date_field": "issuance_date"},
    {"id": "chicago-permits", "org": "City of Chicago", "cbsa": "16980",
     "kind": "socrata", "url": "https://data.cityofchicago.org/resource/ydr8-5enu.json",
     "date_field": "issue_date"},
    {"id": "la-permits", "org": "City of Los Angeles", "cbsa": "31080",
     "kind": "socrata", "url": "https://data.lacity.org/resource/pi9x-tg5x.json",
     "date_field": "issue_date"},
    {"id": "seattle-permits", "org": "City of Seattle", "cbsa": "42660",
     "kind": "socrata", "url": "https://data.seattle.gov/resource/76t5-zqzr.json",
     "date_field": "issueddate"},
    {"id": "sf-permits", "org": "City of San Francisco", "cbsa": "41860",
     "kind": "socrata", "url": "https://data.sfgov.org/resource/i98e-djp9.json",
     "date_field": "issued_date"},
    {"id": "denver-permits", "org": "City of Denver", "cbsa": "19740",
     "kind": "socrata", "url": "https://data.colorado.gov/resource/qbnd-45ur.json",
     "date_field": "issue_date"},
    {"id": "nashville-permits", "org": "Metro Nashville", "cbsa": "34980",
     "kind": "socrata", "url": "https://data.nashville.gov/resource/kqff-rxj8.json",
     "date_field": "date_issued"},
]


def fetch_local_portals(months=12, timeout=45):
    """Query each regional/municipal portal for recent permit volume.

    Returns ({cbsa: {localPermits, localPermitsPrior, localPortal}}, statuses).
    Aggregate queries only — no bulk records are downloaded or republished.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=30 * months)).strftime("%Y-%m-%d")
    prior = (datetime.now(timezone.utc) - timedelta(days=30 * months * 2)).strftime("%Y-%m-%d")
    token = os.environ.get("SOCRATA_APP_TOKEN", "").strip()
    out, statuses = {}, []

    for p in LOCAL_PORTALS:
        try:
            recent = _portal_count(p, since, None, token, timeout)
            previous = _portal_count(p, prior, since, token, timeout)
            rec = out.setdefault(p["cbsa"], {"localPermits": 0, "localPermitsPrior": 0,
                                             "localPortals": []})
            if recent is not None:
                rec["localPermits"] += recent
            if previous is not None:
                rec["localPermitsPrior"] += previous
            rec["localPortals"].append(p["org"])
            statuses.append({"id": p["id"], "org": p["org"], "cbsa": p["cbsa"],
                             "ok": True, "recent": recent, "prior": previous})
        except Exception as e:  # noqa: BLE001 - one portal must not break the rest
            statuses.append({"id": p["id"], "org": p["org"], "cbsa": p["cbsa"],
                             "ok": False, "error": str(e)[:160]})
    for rec in out.values():
        r, p0 = rec.get("localPermits"), rec.get("localPermitsPrior")
        rec["localPermitTrend"] = (r / p0 - 1) if r and p0 else None
    live = [s for s in statuses if s["ok"]]
    if not live:
        raise SourceError("no regional portal responded: "
                          + "; ".join(f"{s['id']}: {s.get('error', '')[:60]}"
                                      for s in statuses[:3]))
    return out, {"portals": statuses, "portalsLive": len(live),
                 "portalsTotal": len(statuses)}


def _portal_count(p, since, until, token, timeout):
    """Return a record count from one portal, or None when unsupported."""
    if p["kind"] == "socrata":
        if not p.get("date_field"):
            return None
        where = f"{p['date_field']} >= '{since}T00:00:00'"
        if until:
            where += f" AND {p['date_field']} < '{until}T00:00:00'"
        url = f"{p['url']}?$select=count(1)%20AS%20n&$where={where.replace(' ', '%20')}"
        headers = {"X-App-Token": token} if token else None
        text, _ = fetch(url, fixture=f"portal_{p['id']}.json", want="text",
                        attempts=1, timeout=timeout, headers=headers)
        rows = json.loads(text)
        return int(to_float(rows[0].get("n")) or 0) if rows else 0

    if p["kind"] == "arcgis":
        where = "1=1"
        if p.get("date_field"):
            where = f"{p['date_field']} >= DATE '{since}'"
            if until:
                where += f" AND {p['date_field']} < DATE '{until}'"
        url = (f"{p['url']}?where={where.replace(' ', '%20').replace('=', '%3D')}"
               f"&returnCountOnly=true&f=json")
        text, _ = fetch(url, fixture=f"portal_{p['id']}.json", want="text",
                        attempts=1, timeout=timeout)
        payload = json.loads(text)
        if "error" in payload:
            raise SourceError(str(payload["error"])[:150])
        return int(payload.get("count", 0))
    return None
