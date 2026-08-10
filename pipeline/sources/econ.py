"""Employment, population, and output — the demand side of every asset class.

BLS QCEW is the workhorse: it reports employment, establishment counts, and
wages by NAICS sector for every metro, from unemployment-insurance filings
(a near-census of covered jobs, not a survey sample). Sector-level detail is
what makes per-asset-class scoring possible — warehouse demand tracks NAICS
48-49, hotel demand tracks 72, data-centre demand tracks 22/51, and so on.
"""

import json
import sys
import time
from datetime import datetime, timezone

from .common import KEYS, SourceError, cagr, fetch, read_csv_rows, to_float

# NAICS sectors pulled from QCEW. Keys are the pipeline's short names; values
# are (QCEW industry_code, human label).
# NAICS sectors pulled from QCEW. Each entry lists candidate industry codes
# tried in order until one actually yields metro rows.
#
# Raw NAICS sector codes do not work on this endpoint: hyphenated ones (31-33,
# 44-45, 48-49) return 404, and plain ones (23, 42) download but contain no
# MSA-level rows. BLS's own aggregation codes (10, 1012, 1013, ...) are the
# ones published at metro level. The specific NAICS code is still tried first
# so real sector detail is preserved wherever BLS offers it; the broader
# aggregate is only a fallback, and when several sectors land on the same
# fallback code their demand signals stop being independent — which the run
# reports via sectorCodes/sharedCodes rather than hiding.
SECTORS = {
    "total":         (["10"], "Total, all industries"),
    "construction":  (["23", "1012"], "Construction"),
    "manufacturing": (["31-33", "31", "1013"], "Manufacturing"),
    "wholesale":     (["42", "1021"], "Wholesale trade"),
    "retail":        (["44-45", "44", "1021"], "Retail trade"),
    "transport":     (["48-49", "48", "1021"], "Transportation and warehousing"),
    "utilities":     (["22", "1021"], "Utilities"),
    "information":   (["51", "1022"], "Information"),
    "finance":       (["52", "1023"], "Finance and insurance"),
    "professional":  (["54", "1024"], "Professional and business services"),
    "health":        (["62", "1025"], "Education and health services"),
    "leisure":       (["71", "1026"], "Leisure and hospitality"),
    "accommodation": (["72", "1026"], "Accommodation and food services"),
}

QCEW_ANNUAL = "https://data.bls.gov/cew/data/api/{year}/a/industry/{code}.csv"
# Seconds between BLS requests. Thirteen sectors across three years is ~39
# calls; fired back-to-back, everything after the first sector was being
# rejected, which left the commercial asset classes unscored.
REQUEST_SPACING = 1.5


def _qcew_cbsa(area_fips):
    """QCEW MSA codes look like 'C1242' for CBSA 12420."""
    a = (area_fips or "").strip().upper()
    if len(a) == 5 and a.startswith("C") and a[1:].isdigit():
        return a[1:] + "0"
    return None


def fetch_qcew(years_back=4):
    """Employment/wages by sector by metro, for the latest available year and
    a 1- and 3-year lag.

    Returns ({cbsa: {sector: {...}}}, meta). QCEW annual data publishes with
    roughly a nine-month lag, so the latest complete year is discovered by
    probing backwards rather than assumed.
    """
    now_year = datetime.now(timezone.utc).year
    base_year = None
    data = {}
    used_urls = []
    failures = []
    loaded = []
    chosen = {}

    for sector, (codes, _label) in SECTORS.items():
        # Establish which year is available using the first sector, then reuse.
        candidate_years = ([base_year] if base_year
                           else list(range(now_year - 1, now_year - years_back - 2, -1)))
        got = False
        for year, code in ((y, c) for y in candidate_years if y for c in codes):
            try:
                text, url = fetch(QCEW_ANNUAL.format(year=year, code=code),
                                  fixture=f"qcew_{sector}_{'cur' if year == base_year or base_year is None else year}.csv",
                                  want="text", attempts=3)
            except SourceError as e:
                failures.append(f"{sector}[{code}]/{year}: {str(e)[:60]}")
                continue
            rows = _parse_qcew(text)
            if not rows:
                failures.append(f"{sector}[{code}]/{year}: 0 MSA rows; "
                                + _describe_qcew(text))
                continue
            chosen[sector] = code
            base_year = base_year or year
            used_urls.append(url)
            for cbsa, rec in rows.items():
                data.setdefault(cbsa, {}).setdefault(sector, {}).update(
                    {"emp": rec["emp"], "estabs": rec["estabs"], "wage": rec["wage"]})
            got = True
            break
        if got:
            loaded.append(sector)
        elif base_year is None:
            raise SourceError(
                f"QCEW unavailable for any year {now_year-1}..{now_year-years_back-1}. "
                + " | ".join(failures[:4]))
        # BLS throttles rapid sequential requests; pace them so sectors after
        # the first are not silently dropped.
        time.sleep(REQUEST_SPACING)

    # Lagged years for growth rates.
    for lag, tag in ((1, "p1"), (3, "p3")):
        year = base_year - lag
        for sector, (codes, _label) in SECTORS.items():
            code = chosen.get(sector, codes[0])
            try:
                text, _ = fetch(QCEW_ANNUAL.format(year=year, code=code),
                                fixture=f"qcew_{sector}_{tag}.csv", want="text", attempts=3)
            except SourceError as e:
                failures.append(f"{sector}/{tag}: {str(e)[:60]}")
                continue
            finally:
                time.sleep(REQUEST_SPACING)
            for cbsa, rec in _parse_qcew(text).items():
                if cbsa in data and sector in data[cbsa]:
                    data[cbsa][sector][f"emp_{tag}"] = rec["emp"]

    if not data:
        raise SourceError("QCEW produced no metro rows")

    # Derive growth rates per sector.
    for cbsa, sectors in data.items():
        for sector, rec in sectors.items():
            rec["g1"] = (rec["emp"] / rec["emp_p1"] - 1) if rec.get("emp_p1") and rec.get("emp") else None
            rec["g3a"] = cagr(rec.get("emp_p3"), rec.get("emp"), 3)
    missing = [x for x in SECTORS if x not in loaded]
    if missing:
        print(f"warning: QCEW loaded {len(loaded)}/{len(SECTORS)} sectors; "
              f"missing {', '.join(missing)} -> {'; '.join(failures[:3])}",
              file=sys.stderr)
    import collections
    dupes = {c: sorted(k for k, v in chosen.items() if v == c)
             for c, n in collections.Counter(chosen.values()).items() if n > 1}
    if dupes:
        print(f"warning: QCEW fell back to shared aggregate codes {dupes}; "
              "those sectors are no longer independent signals", file=sys.stderr)
    return data, {"year": base_year, "urls": used_urls[:3],
                  "sectorsLoaded": len(loaded), "sectorsTotal": len(SECTORS),
                  "sectorsMissing": missing[:8],
                  "sectorCodes": chosen,
                  "sharedCodes": dupes,
                  "sectorErrors": failures[:6]}


def _describe_qcew(text, limit=600):
    """Summarise a QCEW file that yielded no metro rows, for diagnosis."""
    import collections
    kinds, aggs, n = collections.Counter(), collections.Counter(), 0
    for row in read_csv_rows(text):
        n += 1
        area = (row.get("area_fips") or "").strip().upper()
        kinds[area[:1] if area else "?"] += 1
        aggs[(row.get("agglvl_code") or "").strip()] += 1
        if n >= limit:
            break
    return (f"rows={n} areaPrefixes={dict(kinds.most_common(4))} "
            f"agglvl={dict(aggs.most_common(4))}")


def _parse_qcew(text):
    """Aggregate a QCEW industry file down to {cbsa: totals} for MSA areas."""
    out = {}
    for row in read_csv_rows(text):
        cbsa = _qcew_cbsa(row.get("area_fips"))
        if not cbsa:
            continue
        # own_code 0 = total covered employment; 5 = private only (fallback).
        own = (row.get("own_code") or "").strip()
        if own not in ("0", "5"):
            continue
        emp = to_float(row.get("annual_avg_emplvl") or row.get("month3_emplvl"))
        if emp is None:
            continue
        estabs = to_float(row.get("annual_avg_estabs") or row.get("qtrly_estabs"))
        wage = to_float(row.get("avg_annual_pay") or row.get("annual_avg_wkly_wage"))
        prev = out.get(cbsa)
        # Prefer own_code 0 (all ownerships) when both appear.
        if prev is None or (own == "0" and prev.get("own") != "0"):
            out[cbsa] = {"emp": emp, "estabs": estabs, "wage": wage, "own": own}
    for rec in out.values():
        rec.pop("own", None)
    return out


# ------------------------------------------------------- population (PEP) --

POPEST_URLS = [
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/metro/totals/cbsa-est2024-alldata.csv",
    "https://www2.census.gov/programs-surveys/popest/datasets/2020-2023/metro/totals/cbsa-est2023-alldata.csv",
]


def fetch_population():
    """Census Population Estimates by CBSA -> {cbsa: {pop, popGrowth, popGrowth1}}."""
    raw, url = fetch(POPEST_URLS, fixture="popest.csv")
    text = raw.decode("latin-1")
    out = {}
    for row in read_csv_rows(text):
        lsad = (row.get("LSAD") or "").strip()
        if not lsad.startswith(("Metropolitan", "Micropolitan")):
            continue
        code = (row.get("CBSA") or "").strip()
        years = sorted(int(k[-4:]) for k in row
                       if k and k.startswith("POPESTIMATE") and k[-4:].isdigit())
        if not code or len(years) < 2:
            continue
        p_first = to_float(row.get(f"POPESTIMATE{years[0]}"))
        p_last = to_float(row.get(f"POPESTIMATE{years[-1]}"))
        p_prev = to_float(row.get(f"POPESTIMATE{years[-2]}"))
        if not p_first or not p_last:
            continue
        out[code] = {
            "pop": p_last,
            "popGrowth": cagr(p_first, p_last, years[-1] - years[0]),
            "popGrowth1": (p_last / p_prev - 1) if p_prev else None,
            "name": (row.get("NAME") or "").strip(),
            "asof": years[-1],
        }
    if not out:
        raise SourceError("PEP produced no CBSA rows")
    return out, url


# ------------------------------------------------------------- ACS --------

ACS_VARS = ("NAME,B19013_001E,B01003_001E,B25064_001E,B25077_001E,"
            "B23025_003E,B23025_005E,B25003_002E,B25003_003E,B25024_002E,"
            "B25024_003E,B25024_004E,B25024_005E,B25024_006E,B25024_007E,"
            "B25024_008E,B25024_009E,B01002_001E")
ACS_FOR = "metropolitan%20statistical%20area/micropolitan%20statistical%20area:*"


def fetch_acs(year_offsets=(2, 3), lag_years=5):
    """ACS 5-year detail for the current and lagged vintage.

    Requires a free Census API key (the API began rejecting unauthenticated
    calls). Returns ({cbsa: {...}}, meta) or raises with a clear message.
    """
    if not KEYS["census"]:
        raise SourceError(
            "inactive: needs a free Census API key. Request one at "
            "https://api.census.gov/data/key_signup.html and add it as the "
            "repository secret CENSUS_API_KEY to enable income, tenure, "
            "structure-type and unemployment metrics.")
    now = datetime.now(timezone.utc).year
    out, meta = {}, {}
    for tag, offsets in (("now", year_offsets),
                         ("past", tuple(o + lag_years for o in year_offsets))):
        got = False
        for off in offsets:
            year = now - off
            url = (f"https://api.census.gov/data/{year}/acs/acs5?get={ACS_VARS}"
                   f"&for={ACS_FOR}&key={KEYS['census']}")
            try:
                text, _ = fetch(url, fixture=f"acs_{tag}.json", want="text", attempts=2)
                payload = json.loads(text)
            except (SourceError, ValueError):
                continue
            header, rows = payload[0], payload[1:]
            for r in rows:
                rec = dict(zip(header, r))
                code = (rec.get("metropolitan statistical area/micropolitan statistical area")
                        or "").strip()
                if not code:
                    continue
                vals = {k: to_float(v) for k, v in rec.items() if k != "NAME" and not k.startswith("metro")}
                vals["NAME"] = rec.get("NAME")
                out.setdefault(code, {})[tag] = vals
            meta[tag] = year
            got = True
            break
        if not got and tag == "now":
            raise SourceError(f"ACS unavailable for {now-offsets[0]}..{now-offsets[-1]}")
    if not out:
        raise SourceError("ACS produced no rows")
    return out, meta


def shape_acs(rec):
    """Flatten one metro's ACS record into the pipeline's field names."""
    now, past = rec.get("now") or {}, rec.get("past") or {}
    out = {}
    income = now.get("B19013_001E")
    out["income"] = income
    out["medianGrossRent"] = now.get("B25064_001E")
    out["censusHomeValue"] = now.get("B25077_001E")
    out["medianAge"] = now.get("B01002_001E")
    lf, unemp = now.get("B23025_003E"), now.get("B23025_005E")
    if lf and unemp is not None and lf > 0:
        out["unemployment"] = unemp / lf
    owner, renter = now.get("B25003_002E"), now.get("B25003_003E")
    if owner is not None and renter is not None and (owner + renter) > 0:
        out["renterShare"] = renter / (owner + renter)
        out["households"] = owner + renter
    # Units-in-structure: 1-unit detached/attached, 2, 3-4, 5-9, 10-19, 20-49, 50+
    su = {k: now.get(v) for k, v in {
        "u1d": "B25024_002E", "u1a": "B25024_003E", "u2": "B25024_004E",
        "u34": "B25024_005E", "u59": "B25024_006E", "u1019": "B25024_007E",
        "u2049": "B25024_008E", "u50": "B25024_009E"}.items()}
    total_units = sum(v for v in su.values() if v)
    if total_units:
        out["stockSingleFamily"] = ((su["u1d"] or 0) + (su["u1a"] or 0)) / total_units
        out["stockTownhome"] = (su["u1a"] or 0) / total_units
        out["stockSmallMulti"] = ((su["u2"] or 0) + (su["u34"] or 0)) / total_units
        out["stockApartment"] = sum(su[k] or 0 for k in
                                    ("u59", "u1019", "u2049", "u50")) / total_units
        out["housingUnits"] = total_units
    if income and past.get("B19013_001E"):
        out["incomeGrowth"] = cagr(past["B19013_001E"], income, 5)
    return out


# ------------------------------------------------------------- BEA GDP ----

def fetch_bea_gdp():
    """Real GDP by metro (chained dollars) -> {cbsa: {gdp, gdpGrowth}}."""
    if not KEYS["bea"]:
        raise SourceError(
            "inactive: needs a free BEA API key (https://apps.bea.gov/API/signup/). "
            "Add it as the repository secret BEA_API_KEY to enable metro GDP.")
    url = ("https://apps.bea.gov/api/data/?UserID=" + KEYS["bea"] +
           "&method=GetData&datasetname=Regional&TableName=CAGDP9&LineCode=1"
           "&GeoFips=MSA&Year=ALL&ResultFormat=JSON")
    text, used = fetch(url, fixture="bea_gdp.json", want="text", attempts=2)
    payload = json.loads(text)
    results = payload.get("BEAAPI", {}).get("Results", {})
    if "Error" in results or not results.get("Data"):
        raise SourceError(f"BEA error: {str(results.get('Error'))[:150]}")
    series = {}
    for row in results["Data"]:
        code = (row.get("GeoFips") or "").strip()[:5]
        year = to_float(row.get("TimePeriod"))
        val = to_float(row.get("DataValue"))
        if code and year and val:
            series.setdefault(code, {})[int(year)] = val
    out = {}
    for code, by_year in series.items():
        years = sorted(by_year)
        if len(years) < 2:
            continue
        latest = by_year[years[-1]]
        out[code] = {
            "gdp": latest,
            "gdpGrowth": cagr(by_year[years[max(0, len(years) - 4)]], latest,
                              min(3, years[-1] - years[max(0, len(years) - 4)]) or 1),
            "gdpAsof": years[-1],
        }
    if not out:
        raise SourceError("BEA returned no metro GDP")
    return out, used
