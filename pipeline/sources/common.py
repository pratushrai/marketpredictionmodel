"""Shared fetching, parsing, and geography-matching helpers for all sources.

Every source module degrades gracefully: a failure is recorded in the run's
`sources` block (with a body-head excerpt for diagnosis) and the pipeline
continues with whatever else succeeded. Only ZHVI is treated as required.

Offline testing: set LOCAL_FIXTURE_DIR to a directory of fixture files named
by each source's `fixture` key; no network is touched.
"""

import csv
import io
import json
import os
import re
import ssl
import time
import zipfile
import urllib.request
import urllib.error
from pathlib import Path
from xml.etree import ElementTree

USER_AGENT = os.environ.get(
    "PIPELINE_USER_AGENT",
    "real-estate-dashboard/2.0 (+https://github.com/pratushrai/marketpredictionmodel)",
)
FIXTURE_DIR = os.environ.get("LOCAL_FIXTURE_DIR")
CACHE_DIR = os.environ.get("PIPELINE_CACHE_DIR")

# Sources that need a free API key. Absent key -> source is skipped with a
# clear "needs key" status rather than a confusing parse error.
KEYS = {
    "census": os.environ.get("CENSUS_API_KEY", "").strip(),
    "bea": os.environ.get("BEA_API_KEY", "").strip(),
    "hud": os.environ.get("HUD_API_TOKEN", "").strip(),
}


class SourceError(RuntimeError):
    """Fetch or parse failure carrying a short diagnostic excerpt."""


def _fixture_path(fixture):
    return Path(FIXTURE_DIR) / fixture if FIXTURE_DIR else None


def fetch(urls, fixture=None, attempts=3, timeout=180, headers=None, want="bytes"):
    """Fetch the first URL that works. Returns (payload, url_used).

    `want` is "bytes" or "text". Retries each URL with exponential backoff and
    falls through to the next URL on persistent failure.
    """
    if isinstance(urls, str):
        urls = [urls]
    if FIXTURE_DIR and fixture:
        p = _fixture_path(fixture)
        if not p.exists():
            raise SourceError(f"fixture missing: {p}")
        raw = p.read_bytes()
        return (raw.decode("utf-8-sig", "replace") if want == "text" else raw), str(p)

    last = None
    for url in urls:
        cache_file = None
        if CACHE_DIR:
            cache_file = Path(CACHE_DIR) / re.sub(r"[^A-Za-z0-9._-]", "_", url)[-180:]
            if cache_file.exists():
                raw = cache_file.read_bytes()
                return (raw.decode("utf-8-sig", "replace") if want == "text" else raw), url
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    **(headers or {}),
                })
                ctx = ssl.create_default_context()
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    raw = resp.read()
                if cache_file:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_bytes(raw)
                return (raw.decode("utf-8-sig", "replace") if want == "text" else raw), resp.geturl()
            except urllib.error.HTTPError as e:
                body = b""
                try:
                    body = e.read()[:200]
                except Exception:  # noqa: BLE001
                    pass
                last = f"HTTP {e.code} {url} {body.decode('utf-8', 'replace')!r}"
                if e.code in (401, 403, 404):
                    break  # not transient; try the next URL
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e} ({url})"
            time.sleep(2 ** attempt)
    raise SourceError(last or "no URLs supplied")


def head_excerpt(payload, n=180):
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    return " ".join(payload[:n].split())


# ---------------------------------------------------------------- parsing ---

def read_csv_rows(text, delimiter=","):
    """Yield dict rows from CSV text, tolerating BOM and blank trailing lines."""
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    for row in reader:
        if row and any(v not in (None, "") for v in row.values()):
            yield row


def read_zip_member(raw, suffix=(".csv", ".txt")):
    """Return the text of the first member of a zip matching `suffix`."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(tuple(suffix))]
        if not names:
            raise SourceError(f"zip has no {suffix} member: {zf.namelist()[:5]}")
        return zf.read(names[0]).decode("utf-8-sig", "replace")


_XL_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_xlsx(raw, sheet_index=0):
    """Minimal stdlib .xlsx reader -> list of row lists (strings).

    Handles shared strings and inline strings; ignores formatting. Enough for
    the flat reference tables published by Census/HUD/FHFA.
    """
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{_XL_NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_XL_NS}t")))
        sheets = sorted(n for n in zf.namelist()
                        if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheets:
            raise SourceError("xlsx has no worksheets")
        root = ElementTree.fromstring(zf.read(sheets[min(sheet_index, len(sheets) - 1)]))
        rows = []
        for row in root.iter(f"{_XL_NS}row"):
            cells, prev_col = [], 0
            for c in row.findall(f"{_XL_NS}c"):
                ref = c.get("r") or ""
                col = _col_index(re.match(r"[A-Z]+", ref).group(0)) if re.match(r"[A-Z]+", ref) else prev_col + 1
                cells.extend([""] * max(0, col - prev_col - 1))
                prev_col = col
                t, v = c.get("t"), c.find(f"{_XL_NS}v")
                if t == "s" and v is not None:
                    cells.append(shared[int(v.text)] if int(v.text) < len(shared) else "")
                elif t == "inlineStr":
                    cells.append("".join(x.text or "" for x in c.iter(f"{_XL_NS}t")))
                else:
                    cells.append(v.text if v is not None else "")
            rows.append(cells)
        return rows


def _col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def to_float(v):
    """Parse a number, treating Census sentinels and placeholders as missing."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f <= -666666 else f
    s = str(v).strip().replace(",", "").replace("$", "").replace("%", "")
    if s in ("", "-", "--", "N", "NA", "N/A", "(NA)", "(D)", "(S)", ".", "null", "None"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return None if f <= -666666 else f


# -------------------------------------------------------------- geography ---

DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-",
                        "–": "-", "—": "-", "−": "-"})


def norm_name(s):
    """Normalize a place name for fuzzy joining across agencies."""
    s = (s or "").translate(DASHES).lower()
    s = re.sub(r"\b(metro(politan)?|micro(politan)?)\s+(statistical\s+)?area\b", " ", s)
    s = re.sub(r"\b(msa|cbsa|nectа|necta)\b", " ", s)
    s = re.sub(r"[^a-z0-9\- ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def split_cbsa_title(name):
    """'Austin-Round Rock-San Marcos, TX' -> (['austin', 'austin-round rock', ...], ['TX'])."""
    name = (name or "").translate(DASHES)
    base = re.sub(r"\s+(Metro|Micro)(politan)?\s*(Statistical)?\s*Area$", "", name.strip())
    city_part, _, state_part = base.rpartition(",")
    if not city_part:
        city_part, state_part = base, ""
    city_part = city_part.split("/")[0].strip()
    pieces = [p.strip() for p in re.split(r"-+", city_part) if p.strip()]
    cands = ["-".join(pieces[: i + 1]) for i in range(len(pieces))] or [city_part]
    states = [s.strip().upper() for s in re.split(r"[-,\s]+", state_part) if s.strip()]
    return [norm_name(c) for c in cands], states


class CbsaIndex:
    """Join arbitrary agency records onto the pipeline's metro list.

    Agencies key metros three different ways — 5-digit CBSA code, a full CBSA
    title, or a Zillow-style "City, ST" — so this index accepts all three.
    """

    def __init__(self):
        self.by_code = {}
        self.by_name = {}

    def add(self, key, code=None, name=None, state=None):
        if code:
            self.by_code[str(code).strip().lstrip("C").zfill(5)] = key
        if name:
            cands, states = split_cbsa_title(name)
            st_list = [state.upper()] if state else states
            for c in cands:
                for st in (st_list or [""]):
                    self.by_name.setdefault((c, st), key)
                self.by_name.setdefault((c, ""), key)

    def lookup(self, code=None, name=None, state=None):
        if code:
            hit = self.by_code.get(str(code).strip().lstrip("C").zfill(5))
            if hit:
                return hit
        if name:
            cands, states = split_cbsa_title(name)
            st_list = [state.upper()] if state else states
            for c in cands:
                for st in st_list:
                    hit = self.by_name.get((c, st))
                    if hit:
                        return hit
            for c in cands:
                hit = self.by_name.get((c, ""))
                if hit:
                    return hit
        return None


# County -> CBSA crosswalk, for county-grained sources (FEMA, some BLS series).
DELINEATION_URLS = [
    "https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/2023/delineation-files/list1_2023.xlsx",
    "https://www2.census.gov/programs-surveys/metro-micro/geographies/reference-files/2020/delineation-files/list1_2020.xls",
]


def load_county_to_cbsa():
    """Return {5-digit county FIPS: (cbsa_code, cbsa_title)}."""
    raw, _ = fetch(DELINEATION_URLS, fixture="delineation.xlsx")
    rows = read_xlsx(raw)
    header_idx = next((i for i, r in enumerate(rows[:15])
                       if any("CBSA Code" in (c or "") for c in r)), 0)
    header = [(c or "").strip() for c in rows[header_idx]]
    idx = {h: i for i, h in enumerate(header)}
    need = ("CBSA Code", "CBSA Title", "FIPS State Code", "FIPS County Code")
    if not all(k in idx for k in need):
        raise SourceError(f"unexpected delineation header: {header[:8]}")
    out = {}
    for r in rows[header_idx + 1:]:
        if len(r) <= idx["FIPS County Code"]:
            continue
        code = (r[idx["CBSA Code"]] or "").strip()
        st = (r[idx["FIPS State Code"]] or "").strip()
        cty = (r[idx["FIPS County Code"]] or "").strip()
        if not (code and st and cty):
            continue
        out[f"{int(float(st)):02d}{int(float(cty)):03d}"] = (code, (r[idx["CBSA Title"]] or "").strip())
    if not out:
        raise SourceError("delineation file produced no county rows")
    return out


# ------------------------------------------------------------- statistics ---

def median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def percentile_ranks(pairs):
    """[(key, value)] -> {key: 0..100}. Ties share the average rank."""
    valid = sorted(((v, k) for k, v in pairs if v is not None), key=lambda p: p[0])
    n = len(valid)
    if n == 0:
        return {}
    if n == 1:
        return {valid[0][1]: 50.0}
    ranks, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and valid[j + 1][0] == valid[i][0]:
            j += 1
        avg = 100.0 * ((i + j) / 2) / (n - 1)
        for k in range(i, j + 1):
            ranks[valid[k][1]] = avg
        i = j + 1
    return ranks


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def cagr(first, last, years):
    """Compound annual growth rate; None when undefined."""
    if not first or not last or first <= 0 or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1.0 / years) - 1.0


def safe_div(a, b):
    if a is None or not b:
        return None
    return a / b


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n")
    return path.stat().st_size
