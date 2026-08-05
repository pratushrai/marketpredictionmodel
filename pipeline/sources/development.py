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

import csv
import io
import json
import os
import re
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

from .common import (SourceError, cagr, fetch, read_csv_rows, read_zip_member,
                     to_float)

# --------------------------------------------- Census Building Permits -----

BPS_STRUCTURE_GROUPS = ["unit1", "unit2", "unit34", "unit5p"]


BPS_ROOT = "https://www2.census.gov/econ/bps/"
BPS_INDEX_CANDIDATES = [
    BPS_ROOT + "Metro/", BPS_ROOT + "metro/", BPS_ROOT + "MSA/", BPS_ROOT + "msa/",
]
_HREF_DIR = re.compile(r'href="([^"?#/][^"?#]*/)"', re.I)

# Census renames both the directory and the file prefix over time (the metro
# files moved to "CBSA (beginning Jan 2024)/"), so the alphabetic prefix is not
# assumed — only the digits, which encode the period, are interpreted.
_BPS_TXT = re.compile(r'href="([A-Za-z]{1,4}(\d{2,6})([A-Za-z])?\.txt)"', re.I)
_BPS_RANK = {"annual4": 0, "annual2": 1, "y": 2, "c": 3}


def _yy_to_year(yy):
    yy = int(yy)
    return 2000 + yy if yy < 90 else 1900 + yy


def _classify_bps(digits, suffix):
    """Map a BPS filename's digits + suffix to (year, rank), or None.

    Handles ma2024a / ma23a / ma2212y / cb2412c and the 6-digit YYYYMM form.
    Only December (or an explicit annual file) represents a full year.
    """
    suffix = (suffix or "").lower()
    n = len(digits)
    if n == 4 and suffix == "a":
        return int(digits), _BPS_RANK["annual4"]
    if n == 2 and suffix == "a":
        return _yy_to_year(digits), _BPS_RANK["annual2"]
    if n == 4 and suffix in ("y", "c"):
        year, month = digits[:2], digits[2:]
        if month != "12":
            return None
        return _yy_to_year(year), _BPS_RANK[suffix]
    if n == 6 and suffix in ("y", "c", ""):
        year, month = digits[:4], digits[4:]
        if month != "12":
            return None
        return int(year), _BPS_RANK.get(suffix, 3)
    return None


def _find_bps_index():
    """Locate the BPS metro directory, returning (index_url, listing_text).

    Both the directory path and the filenames inside it have moved before, so
    nothing here is hardcoded beyond the survey root: try the known spellings,
    and if none answer, read the root listing and follow whichever
    subdirectory looks metro-level.
    """
    seen = []
    for url in BPS_INDEX_CANDIDATES:
        try:
            text, got = fetch(url, fixture="bps_index.html", want="text", attempts=1)
            return got if got.endswith("/") else url, text
        except SourceError as e:
            seen.append(f"{url} -> {str(e)[:50]}")

    root_text, _ = fetch(BPS_ROOT, fixture="bps_root.html", want="text", attempts=2)
    subdirs = [d for d in dict.fromkeys(_HREF_DIR.findall(root_text))
               if not d.startswith("..")]
    for d in subdirs:
        if any(tag in d.lower() for tag in ("metro", "msa", "cbsa")):
            url = BPS_ROOT + d
            try:
                text, _ = fetch(url, fixture="bps_index.html", want="text", attempts=1)
                return url, text
            except SourceError as e:
                seen.append(f"{url} -> {str(e)[:50]}")
    # Nothing matched: report what the server actually offers, so the next run
    # is fixed from evidence rather than another guess.
    raise SourceError("no metro directory under " + BPS_ROOT
                      + "; subdirectories present: " + ", ".join(subdirs[:12])
                      + " | tried: " + "; ".join(seen[:3]))


def discover_bps_files():
    """Return BPS metro files as [(year, url)], newest first, best per year.

    Guessing filenames is what broke this source in production, so both the
    directory and the files inside it are discovered from live listings.
    December year-to-date files count as annual totals when no explicit annual
    file exists.
    """
    index_url, text = _find_bps_index()
    best, names = {}, []
    for name, digits, suffix in _BPS_TXT.findall(text):
        names.append(name)
        hit = _classify_bps(digits, suffix)
        if not hit:
            continue
        year, rank = hit
        if not (1990 <= year <= 2100):
            continue
        prev = best.get(year)
        if prev is None or rank < prev[0]:
            best[year] = (rank, index_url + quote(name))
    if not best:
        raise SourceError(
            f"BPS index at {index_url} listed no usable period files. "
            f"Files present: {', '.join(names[:15]) or '(no .txt links found)'}")
    return [(y, url) for y, (_r, url) in sorted(best.items(), reverse=True)]


def fetch_permits(years_back=6):
    """Building permits by metro and structure type.

    Returns ({cbsa: {units/bldgs by group, growth, per-capita inputs}}, meta).
    Annual files are used for the latest complete year plus lags, so permit
    *velocity* (not just level) can be scored.
    """
    candidates = discover_bps_files()
    by_year, used, tried = {}, [], []
    for year, url in candidates[:years_back]:
        try:
            text, got = fetch(url, fixture=f"bps_{'cur' if not by_year else year}.txt",
                              want="text", attempts=2)
        except SourceError as e:
            tried.append(f"{year}: {str(e)[:60]}")
            continue
        parsed = _parse_bps(text)
        if not parsed:
            head = " | ".join(ln[:90] for ln in text.splitlines()[:3])
            tried.append(f"{year}: parsed 0 rows from {url.rsplit('/', 1)[-1]}; "
                         f"first lines: {head}")
            continue
        if not by_year:
            used.append(got)
        by_year[year] = parsed
        if len(by_year) >= 5:
            break

    if not by_year:
        raise SourceError("BPS files listed but none parsed: " + "; ".join(tried[:4]))

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
    """Parse a BPS metro/CBSA file -> {cbsa: {group: {bldgs, units}}}.

    Header wording and column order have both changed across vintages, so the
    columns are located from the data rather than from the header: the CBSA
    column is whichever one holds 5-digit codes on most rows, the name column
    is the last non-numeric one, and the numeric triplets after it are
    (buildings, units, value) for 1-unit / 2-unit / 3-4 unit / 5+ unit, in that
    published order. A real CSV reader is required because metro names contain
    commas ("Akron, OH").
    """
    rows = [r for r in csv.reader(io.StringIO(text)) if len(r) >= 8]
    if not rows:
        return {}

    def is_cbsa(v):
        v = (v or "").strip()
        return len(v) == 5 and v.isdigit()

    def is_num(v):
        v = (v or "").strip().replace(",", "")
        if not v:
            return False
        try:
            float(v)
            return True
        except ValueError:
            return False

    # The CBSA column: the one holding 5-digit codes most often.
    width = max(len(r) for r in rows)
    hits = [sum(1 for r in rows if i < len(r) and is_cbsa(r[i])) for i in range(width)]
    cbsa_i = max(range(width), key=lambda i: hits[i])
    if hits[cbsa_i] < 3:
        return {}

    data = [r for r in rows if cbsa_i < len(r) and is_cbsa(r[cbsa_i])]
    if not data:
        return {}

    # The name column: the last column that is text on most data rows.
    text_cols = [i for i in range(width)
                 if sum(1 for r in data if i < len(r) and r[i].strip()
                        and not is_num(r[i])) > len(data) * 0.6]
    name_i = max([i for i in text_cols if i >= cbsa_i] or [cbsa_i])

    out = {}
    for r in data:
        vals = [v for v in r[name_i + 1:]]
        groups = {}
        for gi, g in enumerate(BPS_STRUCTURE_GROUPS):
            base = gi * 3
            if base + 1 >= len(vals):
                break
            bldgs, units = to_float(vals[base]), to_float(vals[base + 1])
            if units is None:
                continue
            groups[g] = {"bldgs": bldgs, "units": units}
        if groups:
            out[r[cbsa_i].strip().zfill(5)] = groups
    return out


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
    try:
        raw, url = fetch(NRI_URLS, fixture="fema_nri.zip",
                         headers={"User-Agent": BROWSER_UA,
                                  "Accept": "application/zip,*/*",
                                  "Referer": "https://hazards.fema.gov/nri/data-resources"})
    except SourceError as e:
        if "403" in str(e):
            raise SourceError(
                "blocked upstream: FEMA's CDN returns 403 to automated and "
                "datacenter requests, so hazard risk cannot be fetched from CI. "
                "Every other input is unaffected — the model simply drops the "
                "hazard term for this run.") from e
        raise
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
