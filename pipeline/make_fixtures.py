#!/usr/bin/env python3
"""Generate synthetic fixtures mirroring each upstream file format.

Test scaffolding only — the numbers are invented, but the *shapes* (headers,
encodings, sentinel values, zip/xlsx packaging, QCEW area codes) mirror the
real feeds so the pipeline's parsing and joining logic is exercised offline.

    python pipeline/make_fixtures.py /tmp/fx
    LOCAL_FIXTURE_DIR=/tmp/fx python pipeline/build_data.py
"""

import csv
import json
import random
import sys
import zipfile
from pathlib import Path

from sources.econ import SECTORS  # noqa: E402  (run from pipeline/)

random.seed(7)

# name, state, CBSA, official PEP title, lat, lon, price, drift, income, pop, rent
METROS = [
    ("Austin, TX", "TX", "12420", "Austin-Round Rock-San Marcos, TX", 30.3, -97.75, 420000, 0.030, 91000, 2450000, 1750),
    ("Dallas-Fort Worth, TX", "TX", "19100", "Dallas-Fort Worth-Arlington, TX", 32.8, -97.0, 380000, 0.045, 83000, 7900000, 1650),
    ("Phoenix, AZ", "AZ", "38060", "Phoenix-Mesa-Chandler, AZ", 33.45, -112.07, 450000, 0.035, 79000, 5070000, 1720),
    ("Winston-Salem, NC", "NC", "49180", "Winston-Salem, NC", 36.1, -80.25, 250000, 0.060, 61000, 695000, 1150),
    ("Nashville, TN", "TN", "34980", "Nashville-Davidson--Murfreesboro--Franklin, TN", 36.15, -86.8, 430000, 0.050, 78000, 2100000, 1700),
    ("Louisville-Jefferson County, KY", "KY", "31140", "Louisville/Jefferson County, KY-IN", 38.25, -85.75, 260000, 0.045, 64000, 1290000, 1100),
    ("New York, NY", "NY", "35620", "New York-Newark-Jersey City, NY-NJ", 40.7, -74.0, 660000, 0.035, 93000, 19500000, 3100),
    ("Urban Honolulu, HI", "HI", "46520", "Urban Honolulu, HI", 21.3, -157.85, 810000, 0.005, 99000, 995000, 2400),
    ("Cleveland, OH", "OH", "17460", "Cleveland-Elyria, OH", 41.5, -81.7, 215000, 0.070, 62000, 2060000, 1150),
    ("Boise City, ID", "ID", "14260", "Boise City, ID", 43.6, -116.2, 470000, -0.010, 76000, 795000, 1600),
    ("Tampa, FL", "FL", "45300", "Tampa-St. Petersburg-Clearwater, FL", 27.95, -82.45, 375000, 0.020, 68000, 3290000, 1800),
    ("Pittsburgh, PA", "PA", "38300", "Pittsburgh, PA", 40.44, -80.0, 220000, 0.040, 66000, 2420000, 1200),
    ("Scranton, PA", "PA", "42540", "Scranton--Wilkes-Barre, PA", 41.4, -75.66, 185000, 0.075, 56000, 567000, 1000),
    ("Fresno, CA", "CA", "23420", "Fresno, CA", 36.75, -119.77, 385000, 0.040, 65000, 1015000, 1450),
    ("Nowhere, MT", "MT", "99999", None, None, None, 300000, 0.030, None, None, None),
]

MONTHS = []
_y, _m = 2015, 7
for _ in range(132):
    MONTHS.append(f"{_y:04d}-{_m:02d}-28")
    _m += 1
    if _m == 13:
        _m, _y = 1, _y + 1


def series(base, drift, n=132):
    vals, v = [], base * 0.55
    for _ in range(n):
        v *= (1 + drift / 12 + random.uniform(-0.002, 0.004))
        vals.append(round(v, 1))
    return vals


def write_zillow(out, fname, value_fn, skip_none=False):
    header = ["RegionID", "SizeRank", "RegionName", "RegionType", "StateName"] + MONTHS
    rows = [["102001", "0", "United States", "country", ""] + [str(x) for x in series(350000, 0.04)]]
    for i, mrec in enumerate(METROS):
        name, st = mrec[0], mrec[1]
        base = value_fn(mrec)
        if base is None and skip_none:
            continue
        rows.append([str(394000 + i), str(i + 1), name, "msa", st]
                    + [str(x) for x in series(base, mrec[7])])
    with open(out / fname, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def write_popest(out):
    years = list(range(2020, 2025))
    cols = ["CBSA", "MDIV", "STCOU", "NAME", "LSAD"] + [f"POPESTIMATE{y}" for y in years]
    rows = []
    for mrec in METROS:
        cbsa, title, pop = mrec[2], mrec[3], mrec[9]
        if not title or not pop:
            continue
        start = pop * random.uniform(0.90, 0.99)
        vals = [round(start + (pop - start) * i / (len(years) - 1)) for i in range(len(years))]
        rows.append([cbsa, "", "", title, "Metropolitan Statistical Area"] + [str(v) for v in vals])
    rows.append(["99998", "", "", "Tinytown, ZZ", "Micropolitan Statistical Area"]
                + ["12000"] * len(years))
    with open(out / "popest.csv", "w", newline="", encoding="latin-1") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)


def write_gazetteer(out):
    lines = ["CSAFP\tCBSAFP\tGEOID\tNAME\tCBSA_TYPE\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG"]
    for mrec in METROS:
        cbsa, title, lat, lon = mrec[2], mrec[3], mrec[4], mrec[5]
        if not title or lat is None:
            continue
        lines.append(f"\t{cbsa}\t{cbsa}\t{title}\t1\t1\t1\t1\t1\t{lat}\t{lon}   ")
    with zipfile.ZipFile(out / "gazetteer.zip", "w") as zf:
        zf.writestr("2024_Gaz_cbsa_national.txt", "\n".join(lines))


def write_fhfa(out):
    rows = [["metro_name", "cbsa", "yr", "qtr", "index_nsa", "index_sa"]]
    for mrec in METROS:
        cbsa, title = mrec[2], mrec[3]
        if not title:
            continue
        idx = 180.0
        for yr in range(2019, 2027):
            for q in range(1, 5):
                if yr == 2026 and q > 2:
                    break
                idx *= 1 + mrec[7] / 4 + random.uniform(-0.004, 0.006)
                rows.append([title, cbsa, str(yr), str(q), f"{idx:.2f}", f"{idx:.2f}"])
    with open(out / "fhfa_hpi.csv", "w", newline="") as f:
        csv.writer(f).writerows(rows)


def write_hud(out):
    data = []
    for mrec in METROS:
        cbsa, rent = mrec[2], mrec[10]
        if not mrec[3] or not rent:
            continue
        data.append({"cbsa_code": cbsa, "Efficiency": round(rent * 0.75),
                     "One-Bedroom": round(rent * 0.85), "Two-Bedroom": rent,
                     "Three-Bedroom": round(rent * 1.35), "Four-Bedroom": round(rent * 1.6)})
    (out / "hud_fmr.json").write_text(json.dumps({"data": {"basicdata": data}}))


def write_acs(out):
    def payload(past):
        header = ["NAME", "B19013_001E", "B01003_001E", "B25064_001E", "B25077_001E",
                  "B23025_003E", "B23025_005E", "B25003_002E", "B25003_003E",
                  "B25024_002E", "B25024_003E", "B25024_004E", "B25024_005E",
                  "B25024_006E", "B25024_007E", "B25024_008E", "B25024_009E",
                  "B01002_001E",
                  "metropolitan statistical area/micropolitan statistical area"]
        rows = [header]
        for mrec in METROS:
            cbsa, title, income, pop, rent = mrec[2], mrec[3], mrec[8], mrec[9], mrec[10]
            if not title or not income:
                continue
            inc = round(income * 0.85) if past else income
            units = round(pop / 2.4)
            rows.append([title, str(inc), str(pop), str(rent - 100 if rent else 900),
                         str(round(mrec[6] * 0.9)), str(round(pop * 0.5)),
                         str(round(pop * 0.5 * random.uniform(0.03, 0.06))),
                         str(round(units * 0.62)), str(round(units * 0.38)),
                         str(round(units * 0.58)), str(round(units * 0.06)),
                         str(round(units * 0.04)), str(round(units * 0.05)),
                         str(round(units * 0.09)), str(round(units * 0.08)),
                         str(round(units * 0.07)), str(round(units * 0.03)),
                         "38.4", cbsa])
        # sentinel row: Census uses -666666666 for suppressed values
        rows.append(["Bad Data, ZZ", "-666666666", "1000", "800", "100000",
                     "500", "20", "300", "200", "250", "30", "20", "20", "40",
                     "30", "20", "10", "40.0", "99997"])
        return rows

    (out / "acs_now.json").write_text(json.dumps(payload(False)))
    (out / "acs_past.json").write_text(json.dumps(payload(True)))


def write_qcew(out):
    """QCEW industry files: all areas for one NAICS sector, MSA rows as C####."""
    header = ["area_fips", "own_code", "industry_code", "agglvl_code", "year",
              "qtr", "annual_avg_estabs", "annual_avg_emplvl", "avg_annual_pay"]
    for sector, (code, _label) in SECTORS.items():
        for tag, scale in (("cur", 1.0), ("p1", 0.975), ("p3", 0.93)):
            rows = [header]
            for mrec in METROS:
                cbsa, pop = mrec[2], mrec[9]
                if not mrec[3] or not pop:
                    continue
                share = {"total": 0.45, "construction": 0.03, "manufacturing": 0.05,
                         "wholesale": 0.02, "retail": 0.05, "transport": 0.035,
                         "utilities": 0.004, "information": 0.02, "finance": 0.03,
                         "professional": 0.05, "health": 0.07, "leisure": 0.012,
                         "accommodation": 0.055}[sector]
                emp = pop * share * scale * random.uniform(0.97, 1.03)
                rows.append([f"C{cbsa[:4]}", "0", code, "44", "2024", "A",
                             str(round(emp / 12)), str(round(emp)),
                             str(round(62000 * random.uniform(0.8, 1.3)))])
                # a private-only duplicate row, which the parser must not double count
                rows.append([f"C{cbsa[:4]}", "5", code, "44", "2024", "A",
                             str(round(emp / 13)), str(round(emp * 0.85)), "58000"])
            with open(out / f"qcew_{sector}_{tag}.csv", "w", newline="") as f:
                csv.writer(f).writerows(rows)


def write_bps_index(out):
    """Apache-style directory index, mirroring the mixed naming Census uses."""
    names = ["ma2024a.txt", "ma23a.txt", "ma2212y.txt", "ma2112y.txt",
             "ma2012c.txt", "ma2501c.txt", "readme.txt"]
    rows = "\n".join(
        f'<tr><td><a href="{n}">{n}</a></td><td>2025-03-01 08:14</td></tr>'
        for n in names)
    (out / "bps_index.html").write_text(
        f"<html><head><title>Index of /econ/bps/Metro</title></head>"
        f"<body><h1>Index of /econ/bps/Metro</h1><table>{rows}</table></body></html>")


def write_bps(out):
    """Census BPS metro file: two header rows, then CBSA rows."""
    for tag in ("cur", "2023", "2022", "2021", "2020"):
        head1 = ("Survey,CSA,CBSA,Name,1-unit,1-unit,1-unit,2-units,2-units,2-units,"
                 "3-4 units,3-4 units,3-4 units,5+ units,5+ units,5+ units")
        head2 = ",,,,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value,Bldgs,Units,Value"
        lines = [head1, head2]
        scale = {"cur": 1.0, "2023": 0.94, "2022": 0.88, "2021": 0.8, "2020": 0.74}[tag]
        for mrec in METROS:
            cbsa, title, pop = mrec[2], mrec[3], mrec[9]
            if not title or not pop:
                continue
            u1 = round(pop / 900 * scale * random.uniform(0.7, 1.4))
            u2 = round(pop / 40000 * scale)
            u34 = round(pop / 30000 * scale)
            u5 = round(pop / 1400 * scale * random.uniform(0.4, 2.2))
            lines.append(f"2024,,{cbsa},{title.replace(',', ' ')},"
                         f"{u1//3},{u1},{u1*250000},{u2//2},{u2},{u2*200000},"
                         f"{u34//3},{u34},{u34*190000},{u5//40},{u5},{u5*180000}")
        (out / f"bps_{tag}.txt").write_text("\n".join(lines))


def write_fema(out):
    header = ["STCOFIPS", "COUNTY", "POPULATION", "RISK_SCORE", "EAL_VALT", "RESL_SCORE"]
    rows = [header]
    for i, mrec in enumerate(METROS):
        if not mrec[3] or not mrec[9]:
            continue
        for c in range(2):  # two counties per metro, to exercise the roll-up
            rows.append([f"{(i+1)*2+c:05d}", f"County {c}", str(round(mrec[9] / 2)),
                         f"{random.uniform(5, 90):.1f}",
                         str(round(mrec[9] * random.uniform(2, 40))),
                         f"{random.uniform(30, 70):.1f}"])
    buf = []
    for row in rows:
        buf.append(",".join(str(x) for x in row))
    with zipfile.ZipFile(out / "fema_nri.zip", "w") as zf:
        zf.writestr("NRI_Table_Counties.csv", "\n".join(buf))


def write_xlsx(path, rows):
    """Minimal .xlsx writer using inline strings (matches read_xlsx)."""
    def ref(col, row):
        s, c = "", col
        while c > 0:
            c, rem = divmod(c - 1, 26)
            s = chr(65 + rem) + s
        return f"{s}{row}"

    body = []
    for ri, row in enumerate(rows, 1):
        cells = []
        for ci, val in enumerate(row, 1):
            v = "" if val is None else str(val)
            esc = v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            cells.append(f'<c r="{ref(ci, ri)}" t="inlineStr"><is><t>{esc}</t></is></c>')
        body.append(f'<row r="{ri}">{"".join(cells)}</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8"?><worksheet '
             'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'<sheetData>{"".join(body)}</sheetData></worksheet>')
    ctypes = ('<?xml version="1.0" encoding="UTF-8"?><Types '
              'xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
              '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
              '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8"?><workbook '
          'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
    wbrels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships '
              'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
              '</Relationships>')
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", ctypes)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", wb)
        zf.writestr("xl/_rels/workbook.xml.rels", wbrels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)


def write_delineation(out):
    rows = [["This file is a listing of..."], [], []]
    rows.append(["CBSA Code", "MDIV Code", "CSA Code", "CBSA Title",
                 "Metropolitan/Micropolitan Statistical Area", "State Name",
                 "County/County Equivalent", "FIPS State Code", "FIPS County Code"])
    for i, mrec in enumerate(METROS):
        if not mrec[3]:
            continue
        for c in range(2):
            fips = f"{(i+1)*2+c:05d}"
            rows.append([mrec[2], "", "", mrec[3], "Metropolitan Statistical Area",
                         mrec[1], f"County {c}", fips[:2], fips[2:]])
    write_xlsx(out / "delineation.xlsx", rows)


def write_portals(out):
    from sources.development import LOCAL_PORTALS
    for p in LOCAL_PORTALS:
        if p["kind"] == "socrata":
            (out / f"portal_{p['id']}.json").write_text(
                json.dumps([{"n": str(random.randint(400, 9000))}]))
        else:
            (out / f"portal_{p['id']}.json").write_text(
                json.dumps({"count": random.randint(400, 9000)}))


def write_mls(out):
    listings = []
    for mrec in METROS[:6]:
        city = mrec[0].split(",")[0]
        for _ in range(30):
            lp = mrec[6] * random.uniform(0.8, 1.3)
            listings.append({
                "ListPrice": round(lp), "ClosePrice": round(lp * random.uniform(0.95, 1.02)),
                "DaysOnMarket": random.randint(3, 90), "City": city,
                "StateOrProvince": mrec[1], "LivingArea": random.randint(1100, 3200),
                "PropertyType": "Residential", "StandardStatus": "Closed",
            })
    (out / "mls.json").write_text(json.dumps({"value": listings}))


def write_bea(out):
    data = []
    for mrec in METROS:
        if not mrec[3] or not mrec[9]:
            continue
        gdp = mrec[9] * 65
        for yr in range(2019, 2025):
            data.append({"GeoFips": mrec[2], "TimePeriod": str(yr),
                         "DataValue": str(round(gdp * (1.03 ** (yr - 2019))))})
    (out / "bea_gdp.json").write_text(json.dumps({"BEAAPI": {"Results": {"Data": data}}}))


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/fx")
    out.mkdir(parents=True, exist_ok=True)
    write_zillow(out, "zhvi.csv", lambda m: m[6])
    write_zillow(out, "zori.csv", lambda m: m[10], skip_none=True)
    write_zillow(out, "inventory.csv", lambda m: 5000 if m[10] else None, skip_none=True)
    write_zillow(out, "days_on_market.csv", lambda m: 40 if m[10] else None, skip_none=True)
    write_popest(out)
    write_gazetteer(out)
    write_fhfa(out)
    write_hud(out)
    write_acs(out)
    write_qcew(out)
    write_bps_index(out)
    write_bps(out)
    write_fema(out)
    write_delineation(out)
    write_portals(out)
    write_mls(out)
    write_bea(out)
    print(f"fixtures written to {out} ({len(list(out.iterdir()))} files)")


if __name__ == "__main__":
    main()
