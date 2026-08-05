"""Price / rent / listing sources.

Zillow is the broad baseline, but it is a *modeled* index. Two independent,
non-Zillow signals are layered on top:

  * FHFA HPI  - repeat-sales index built from actual Fannie/Freddie mortgage
    transactions. Transaction-based, published quarterly, keyless.
  * HUD FMR   - administratively-set rents by bedroom count, an independent
    check on Zillow's rent index.

MLS is the source the user really wants for listing-level truth (days on
market, list-to-sale, true inventory). It is *not* publicly fetchable: every
MLS licenses its data through RESO Web API under a broker/agent agreement.
`fetch_mls` implements that standard interface and activates only when
credentials are supplied; without them the pipeline records the source as
"inactive (credentials required)" rather than silently pretending.
"""

import json
import re
from datetime import datetime, timezone

from .common import (KEYS, SourceError, cagr, fetch, read_csv_rows, read_xlsx,
                     to_float)

# --------------------------------------------------------------- Zillow ----

ZILLOW_FILES = {
    "zhvi": ([
        "https://files.zillowstatic.com/research/public_csvs/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv",
        "https://files.zillowstatic.com/research/public_csvs/zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_month.csv",
    ], "zhvi.csv"),
    "zhvi_sfr": ([
        "https://files.zillowstatic.com/research/public_csvs/zhvi/Metro_zhvi_uc_sfr_tier_0.33_0.67_sm_sa_month.csv",
    ], "zhvi_sfr.csv"),
    "zhvi_condo": ([
        "https://files.zillowstatic.com/research/public_csvs/zhvi/Metro_zhvi_uc_condo_tier_0.33_0.67_sm_sa_month.csv",
    ], "zhvi_condo.csv"),
    "zori": ([
        "https://files.zillowstatic.com/research/public_csvs/zori/Metro_zori_uc_sfrcondomfr_sm_sa_month.csv",
        "https://files.zillowstatic.com/research/public_csvs/zori/Metro_zori_uc_sfrcondomfr_sm_month.csv",
    ], "zori.csv"),
    "inventory": ([
        "https://files.zillowstatic.com/research/public_csvs/invt_fs/Metro_invt_fs_uc_sfrcondo_sm_month.csv",
    ], "inventory.csv"),
    "days_on_market": ([
        "https://files.zillowstatic.com/research/public_csvs/mean_doz_pending/Metro_mean_doz_pending_uc_sfrcondo_sm_month.csv",
    ], "days_on_market.csv"),
    "new_listings": ([
        "https://files.zillowstatic.com/research/public_csvs/new_listings/Metro_new_listings_uc_sfrcondo_sm_month.csv",
    ], "new_listings.csv"),
}

_DATE_COL = re.compile(r"\d{4}-\d{2}-\d{2}")


def fetch_zillow(kind):
    """Parse a wide Zillow metro CSV -> list of metro dicts with monthly series."""
    urls, fixture = ZILLOW_FILES[kind]
    text, url = fetch(urls, fixture=fixture, want="text")
    rows, header = [], None
    for row in read_csv_rows(text):
        if header is None:
            header = list(row.keys())
            date_cols = [h for h in header if h and _DATE_COL.fullmatch(h)]
        if row.get("RegionType") not in (None, "", "msa"):
            continue
        series = []
        for h in date_cols:
            v = to_float(row.get(h))
            if v is not None:
                series.append((h[:7], v))
        if not series:
            continue
        region = (row.get("RegionName") or "").strip()
        rows.append({
            "id": (row.get("RegionID") or "").strip(),
            "name": region,
            "city": region.rsplit(",", 1)[0].strip(),
            "state": (row.get("StateName") or "").strip(),
            "sizeRank": int(to_float(row.get("SizeRank")) or 10**9),
            "series": series,
        })
    if not rows:
        raise SourceError(f"no msa rows parsed from {kind}")
    return rows, url


# ----------------------------------------------------------- FHFA HPI ------

FHFA_URLS = [
    "https://www.fhfa.gov/hpi/download/quarterly_datasets/hpi_at_metro.csv",
    "https://www.fhfa.gov/DataTools/Downloads/Documents/HPI/HPI_AT_metro.csv",
]


def fetch_fhfa_hpi():
    """Repeat-sales house price index by CBSA.

    Returns {cbsa_code: {"index": latest, "g1": 1yr, "g5a": 5yr annualized,
    "asof": "YYYYQn", "name": title}} — a transaction-based cross-check on
    Zillow's modeled index.
    """
    text, url = fetch(FHFA_URLS, fixture="fhfa_hpi.csv", want="text")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise SourceError("empty FHFA file")
    # The published file is sometimes headerless; detect by trying the header.
    first = lines[0].lower()
    has_header = "cbsa" in first or "metro" in first or "yr" in first
    series = {}
    reader = read_csv_rows(text) if has_header else None
    if has_header:
        for row in reader:
            keys = {k.lower().strip(): k for k in row if k}
            code = row.get(keys.get("cbsa", ""), "")
            yr = to_float(row.get(keys.get("yr", ""), ""))
            qtr = to_float(row.get(keys.get("qtr", ""), ""))
            val = None
            for cand in ("index_nsa", "index_sa", "index"):
                if cand in keys:
                    val = to_float(row.get(keys[cand]))
                    if val is not None:
                        break
            name = row.get(keys.get("metro_name", ""), "") or row.get(keys.get("metro", ""), "")
            if code and yr and qtr and val:
                series.setdefault(str(code).strip(), {"name": name, "pts": []})["pts"].append(
                    (int(yr) * 4 + int(qtr), val))
    else:
        for ln in lines:
            parts = next(read_csv_rows("a,b,c,d,e,f\n" + ln), None)
            if not parts:
                continue
            vals = list(parts.values())
            name, code, yr, qtr = vals[0], vals[1], to_float(vals[2]), to_float(vals[3])
            val = to_float(vals[4]) if len(vals) > 4 else None
            if code and yr and qtr and val:
                series.setdefault(str(code).strip(), {"name": name, "pts": []})["pts"].append(
                    (int(yr) * 4 + int(qtr), val))
    out = {}
    for code, rec in series.items():
        pts = sorted(rec["pts"])
        if len(pts) < 5:
            continue
        latest_t, latest_v = pts[-1]
        by_t = dict(pts)
        out[code] = {
            "index": round(latest_v, 2),
            "name": rec["name"],
            "asof": f"{latest_t // 4}Q{latest_t % 4 or 4}",
            "g1": (latest_v / by_t[latest_t - 4] - 1) if by_t.get(latest_t - 4) else None,
            "g5a": cagr(by_t.get(latest_t - 20), latest_v, 5),
        }
    if not out:
        raise SourceError("FHFA parsed but produced no metros")
    return out, url


# ------------------------------------------------------------- HUD FMR -----

def fetch_hud_fmr():
    """HUD Fair Market Rents by metro and bedroom count.

    Prefers the authenticated API (HUD_API_TOKEN); falls back to the published
    workbook, whose filename changes each fiscal year, so several are tried.
    """
    year = datetime.now(timezone.utc).year
    if KEYS["hud"]:
        text, url = fetch(
            f"https://www.huduser.gov/hudapi/public/fmr/data/{year}",
            fixture="hud_fmr.json", want="text",
            headers={"Authorization": f"Bearer {KEYS['hud']}"})
        payload = json.loads(text)
        rows = payload.get("data", {}).get("basicdata", payload.get("data", []))
        out = {}
        for r in rows if isinstance(rows, list) else []:
            code = str(r.get("cbsa_code") or r.get("code") or "").strip()
            if not code:
                continue
            out[code.lstrip("METRO")[:5]] = {
                "fmr0": to_float(r.get("Efficiency")), "fmr1": to_float(r.get("One-Bedroom")),
                "fmr2": to_float(r.get("Two-Bedroom")), "fmr3": to_float(r.get("Three-Bedroom")),
                "fmr4": to_float(r.get("Four-Bedroom")),
            }
        if out:
            return out, url
        raise SourceError("HUD API returned no rows")

    urls = [f"https://www.huduser.gov/portal/datasets/fmr/fmr{y}/FY{str(y)[2:]}_FMRs.xlsx"
            for y in (year, year - 1)]
    urls += [f"https://www.huduser.gov/portal/datasets/fmr/fmr{y}/FY{y}_4050_FMRs_rev.xlsx"
             for y in (year, year - 1)]
    raw, url = fetch(urls, fixture="hud_fmr.xlsx")
    rows = read_xlsx(raw)
    if not rows:
        raise SourceError("empty HUD workbook")
    header = [(c or "").strip().lower() for c in rows[0]]

    def col(*names):
        for n in names:
            for i, h in enumerate(header):
                if h == n or h.startswith(n):
                    return i
        return None

    ci = {k: col(*v) for k, v in {
        "code": ("cbsa", "metro_code", "hud_area_code"),
        "fmr0": ("fmr_0", "fmr0"), "fmr1": ("fmr_1", "fmr1"),
        "fmr2": ("fmr_2", "fmr2"), "fmr3": ("fmr_3", "fmr3"),
        "fmr4": ("fmr_4", "fmr4"),
    }.items()}
    if ci["code"] is None or ci["fmr2"] is None:
        raise SourceError(f"unexpected HUD columns: {header[:10]}")
    out = {}
    for r in rows[1:]:
        if len(r) <= ci["code"]:
            continue
        raw_code = re.sub(r"\D", "", str(r[ci["code"]] or ""))
        if len(raw_code) < 5:
            continue
        code = raw_code[:5]
        rec = {k: (to_float(r[ci[k]]) if ci[k] is not None and len(r) > ci[k] else None)
               for k in ("fmr0", "fmr1", "fmr2", "fmr3", "fmr4")}
        if rec["fmr2"] is not None:
            out.setdefault(code, rec)
    if not out:
        raise SourceError("HUD workbook produced no metros")
    return out, url


# ------------------------------------------------------- MLS (RESO API) ----

MLS_ENV = ("MLS_RESO_BASE_URL", "MLS_RESO_TOKEN")


def mls_configured():
    import os
    return all(os.environ.get(k, "").strip() for k in MLS_ENV)


def fetch_mls(limit=5000):
    """Pull listing-level data from a RESO Web API feed, if credentialed.

    MLS data is licensed per-market: you need an IDX/broker agreement and the
    resulting OData endpoint + bearer token. Set MLS_RESO_BASE_URL and
    MLS_RESO_TOKEN (optionally MLS_RESO_FILTER) to activate. Aggregates to
    metro-level medians so no listing-level data is ever republished.
    """
    import os
    if not mls_configured():
        raise SourceError(
            "inactive: MLS requires a licensed RESO Web API feed. Set "
            "MLS_RESO_BASE_URL and MLS_RESO_TOKEN repository secrets to enable. "
            "Public alternatives (FHFA repeat-sales HPI, Census permits, Zillow "
            "inventory/days-on-market) are used meanwhile.")
    base = os.environ["MLS_RESO_BASE_URL"].strip().rstrip("/")
    token = os.environ["MLS_RESO_TOKEN"].strip()
    user_filter = os.environ.get("MLS_RESO_FILTER", "").strip()

    select = ",".join(["ListPrice", "ClosePrice", "DaysOnMarket", "PostalCode",
                       "City", "StateOrProvince", "PropertyType",
                       "PropertySubType", "LivingArea", "StandardStatus",
                       "CloseDate"])
    listings, skip = [], 0
    while len(listings) < limit:
        q = (f"{base}/Property?$top=1000&$skip={skip}&$select={select}"
             + (f"&$filter={user_filter}" if user_filter else ""))
        text, _ = fetch(q, fixture="mls.json", want="text",
                        headers={"Authorization": f"Bearer {token}",
                                 "Accept": "application/json"})
        page = json.loads(text).get("value", [])
        listings.extend(page)
        if len(page) < 1000:
            break
        skip += 1000
    if not listings:
        raise SourceError("RESO feed returned no listings")
    return listings, base


def summarize_mls(listings):
    """Aggregate raw listings to {(city, state): metrics} medians."""
    from .common import median
    buckets = {}
    for lst in listings:
        city = (lst.get("City") or "").strip()
        state = (lst.get("StateOrProvince") or "").strip().upper()
        if not city or not state:
            continue
        b = buckets.setdefault((city, state), {"list": [], "close": [], "dom": [], "ppsf": [], "n": 0})
        b["n"] += 1
        lp, cp = to_float(lst.get("ListPrice")), to_float(lst.get("ClosePrice"))
        dom, area = to_float(lst.get("DaysOnMarket")), to_float(lst.get("LivingArea"))
        if lp:
            b["list"].append(lp)
        if cp:
            b["close"].append(cp)
        if dom is not None:
            b["dom"].append(dom)
        if cp and area and area > 100:
            b["ppsf"].append(cp / area)
    out = {}
    for key, b in buckets.items():
        if b["n"] < 5:
            continue
        ml, mc = median(b["list"]), median(b["close"])
        out[key] = {
            "mlsListings": b["n"],
            "mlsMedianList": round(ml) if ml else None,
            "mlsMedianClose": round(mc) if mc else None,
            "mlsDaysOnMarket": median(b["dom"]),
            "mlsPricePerSqft": round(median(b["ppsf"]), 1) if b["ppsf"] else None,
            "mlsSaleToList": round(mc / ml, 4) if ml and mc else None,
        }
    return out
