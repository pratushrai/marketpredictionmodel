"""Scoring: headline growth forecast plus a score per asset class.

Two ideas carry most of the weight here.

**Net absorption, not raw demand.** A metro where jobs are booming *and*
builders are flooding the market is not the same investment as one where jobs
are booming and nothing is being permitted. Every income-producing class is
therefore scored on demand growth *minus* supply pressure, where supply
pressure is permits in that structure type relative to the existing stock of
that structure type.

**Land flips the sign.** For a developer buying land, heavy permitting and a
permissive entitlement climate are the *product*, not the risk — so the land
class rewards exactly the supply signal that penalises apartment owners.

Commercial classes have no public price index, so they are scored on demand
fundamentals (sector employment, establishment and wage growth from QCEW)
rather than on returns. That limitation is stated in the dashboard.
"""

from .sources.common import clamp, median, percentile_ranks

MODEL_VERSION = "2.0"

# Industrial electricity price, cents/kWh, EIA state profiles (2024 average).
# Data-centre siting is power-cost driven, so this is a first-order input.
STATE_POWER_CENTS = {
    "AL": 6.9, "AK": 18.6, "AZ": 7.6, "AR": 6.6, "CA": 18.5, "CO": 8.4,
    "CT": 16.9, "DE": 9.6, "DC": 12.4, "FL": 8.6, "GA": 7.2, "HI": 34.8,
    "ID": 6.9, "IL": 8.4, "IN": 8.3, "IA": 6.6, "KS": 8.2, "KY": 6.7,
    "LA": 6.1, "ME": 11.9, "MD": 10.3, "MA": 17.3, "MI": 8.9, "MN": 8.6,
    "MS": 6.7, "MO": 8.1, "MT": 7.3, "NE": 7.9, "NV": 8.2, "NH": 15.1,
    "NJ": 12.4, "NM": 7.1, "NY": 8.0, "NC": 7.6, "ND": 7.9, "OH": 7.7,
    "OK": 6.3, "OR": 8.1, "PA": 8.5, "RI": 15.6, "SC": 7.1, "SD": 8.1,
    "TN": 7.3, "TX": 6.5, "UT": 6.7, "VT": 12.1, "VA": 8.3, "WA": 6.7,
    "WV": 7.1, "WI": 8.7, "WY": 6.7,
}

# Each asset class: what drives demand, which permits compete with it, which
# slice of the existing stock it belongs to, and how much policy matters.
#
#   demand   - (feature, weight) pairs, weights sum to 1
#   supply   - permit fields that add competing space for this class
#   stock    - share-of-stock field used to size supply pressure
#   supplySign - -1 penalises new supply (owning), +1 rewards it (developing)
#   yieldWeight / policyWeight / hazardWeight - blend of the non-demand terms
ASSET_CLASSES = {
    "sfr": {
        "label": "Single-family homes", "group": "Residential",
        "demand": [("popGrowth", 0.30), ("empGrowth", 0.25), ("incomeGrowth", 0.20),
                   ("emp_professional_g1", 0.10), ("emp_health_g1", 0.10),
                   ("emp_construction_g1", 0.05)],
        "supply": ["permits_unit1"], "stock": "stockSingleFamily",
        "supplySign": -1, "supplyWeight": 0.15,
        "yieldWeight": 0.15, "policyWeight": 0.10, "hazardWeight": 0.06,
        "affordWeight": 0.16,
        "note": "Owner-occupied and rental detached housing; the deepest and "
                "most liquid market, and the only class with a direct public "
                "price index.",
    },
    "townhome": {
        "label": "Townhomes & attached", "group": "Residential",
        "demand": [("popGrowth", 0.30), ("empGrowth", 0.25), ("incomeGrowth", 0.15),
                   ("emp_professional_g1", 0.15), ("densityProxy", 0.15)],
        "supply": ["permits_unit1"], "stock": "stockTownhome",
        "supplySign": -1, "supplyWeight": 0.12,
        "yieldWeight": 0.15, "policyWeight": 0.14, "hazardWeight": 0.05,
        "affordWeight": 0.14,
        "note": "Attached for-sale product. Highly sensitive to zoning: "
                "missing-middle reform is what makes it buildable at all.",
    },
    "duplex": {
        "label": "Duplex & 2-4 unit", "group": "Residential",
        "demand": [("renterShare", 0.25), ("popGrowth", 0.25), ("empGrowth", 0.25),
                   ("incomeGrowth", 0.15), ("emp_health_g1", 0.10)],
        "supply": ["permits_unit2", "permits_unit34"], "stock": "stockSmallMulti",
        "supplySign": -1, "supplyWeight": 0.12,
        "yieldWeight": 0.24, "policyWeight": 0.12, "hazardWeight": 0.05,
        "affordWeight": 0.12,
        "note": "Small multifamily: residential financing with commercial-style "
                "yield. Very little new supply is built, so existing stock is "
                "structurally scarce.",
    },
    "apartment": {
        "label": "Apartment buildings (5+)", "group": "Residential",
        "demand": [("renterShare", 0.22), ("popGrowth", 0.25), ("empGrowth", 0.25),
                   ("emp_professional_g1", 0.13), ("emp_health_g1", 0.15)],
        "supply": ["permits_unit5p"], "stock": "stockApartment",
        "supplySign": -1, "supplyWeight": 0.26,
        "yieldWeight": 0.18, "policyWeight": 0.16, "hazardWeight": 0.05,
        "affordWeight": 0.05,
        "note": "The class where supply matters most: 5+ unit permitting swings "
                "hard by cycle and a delivery wave can flatten rents for years. "
                "Rent-control regime is a direct policy risk.",
    },
    "industrial": {
        "label": "Industrial & warehouse", "group": "Commercial",
        "demand": [("emp_transport_g1", 0.30), ("emp_transport_g3a", 0.20),
                   ("emp_wholesale_g1", 0.20), ("emp_manufacturing_g1", 0.15),
                   ("empGrowth", 0.15)],
        "supply": [], "stock": None, "supplySign": -1, "supplyWeight": 0.0,
        "yieldWeight": 0.0, "policyWeight": 0.30, "hazardWeight": 0.08,
        "affordWeight": 0.10, "estabDriver": "transport",
        "note": "Logistics demand tracks transportation and warehousing "
                "employment (NAICS 48-49). Land cost and entitlement speed "
                "decide whether it can actually be built.",
    },
    "datacenter": {
        "label": "Data centers", "group": "Commercial",
        "demand": [("emp_information_g1", 0.30), ("emp_utilities_g1", 0.25),
                   ("emp_professional_g1", 0.20), ("gdpGrowth", 0.15),
                   ("empGrowth", 0.10)],
        "supply": [], "stock": None, "supplySign": -1, "supplyWeight": 0.0,
        "yieldWeight": 0.0, "policyWeight": 0.22, "hazardWeight": 0.12,
        "affordWeight": 0.06, "powerWeight": 0.26, "estabDriver": "utilities",
        "note": "Sited on power price, power availability and hazard exposure "
                "far more than on population. Industrial electricity cost is "
                "weighted heavily and cheap-power metros dominate.",
    },
    "retail": {
        "label": "Retail (strip & centers)", "group": "Commercial",
        "demand": [("emp_retail_g1", 0.28), ("popGrowth", 0.25),
                   ("incomeGrowth", 0.22), ("empGrowth", 0.15),
                   ("emp_retail_g3a", 0.10)],
        "supply": [], "stock": None, "supplySign": -1, "supplyWeight": 0.0,
        "yieldWeight": 0.0, "policyWeight": 0.16, "hazardWeight": 0.06,
        "affordWeight": 0.10, "saturationDriver": "retail", "saturationWeight": 0.18,
        "note": "Rooftops and household income drive sales; existing retail "
                "establishments per capita measure how saturated the trade "
                "area already is.",
    },
    "hospitality": {
        "label": "Hotels & hospitality", "group": "Commercial",
        "demand": [("emp_accommodation_g1", 0.32), ("emp_leisure_g1", 0.25),
                   ("emp_accommodation_g3a", 0.15), ("popGrowth", 0.13),
                   ("gdpGrowth", 0.15)],
        "supply": [], "stock": None, "supplySign": -1, "supplyWeight": 0.0,
        "yieldWeight": 0.0, "policyWeight": 0.16, "hazardWeight": 0.14,
        "affordWeight": 0.08, "saturationDriver": "accommodation",
        "saturationWeight": 0.10,
        "note": "Accommodation employment (NAICS 72) is the closest public "
                "proxy for room-night demand. Hazard exposure matters more "
                "here — a hurricane season closes the asset.",
    },
    "restaurant": {
        "label": "Restaurants & F&B", "group": "Commercial",
        "demand": [("emp_accommodation_g1", 0.30), ("popGrowth", 0.22),
                   ("incomeGrowth", 0.20), ("empGrowth", 0.16),
                   ("emp_leisure_g1", 0.12)],
        "supply": [], "stock": None, "supplySign": -1, "supplyWeight": 0.0,
        "yieldWeight": 0.0, "policyWeight": 0.16, "hazardWeight": 0.06,
        "affordWeight": 0.12, "saturationDriver": "accommodation",
        "saturationWeight": 0.14,
        "note": "Food-service demand follows daytime population and discretionary "
                "income. Establishment density signals how contested the market is.",
    },
    "office": {
        "label": "Office", "group": "Commercial",
        "demand": [("emp_professional_g1", 0.30), ("emp_finance_g1", 0.22),
                   ("emp_information_g1", 0.18), ("emp_professional_g3a", 0.15),
                   ("empGrowth", 0.15)],
        "supply": [], "stock": None, "supplySign": -1, "supplyWeight": 0.0,
        "yieldWeight": 0.0, "policyWeight": 0.14, "hazardWeight": 0.04,
        "affordWeight": 0.12,
        "note": "Structurally challenged post-2020: office-using employment can "
                "grow while space demand per worker falls, so treat a high score "
                "as 'least bad', not as a buy signal.",
    },
    "land": {
        "label": "Land & development", "group": "Land",
        "demand": [("popGrowth", 0.32), ("empGrowth", 0.24),
                   ("permitGrowth1", 0.18), ("incomeGrowth", 0.12),
                   ("emp_construction_g1", 0.14)],
        # Sign flips: for a developer, heavy permitting is proof the pipeline
        # works, not competing supply.
        "supply": ["permits_unit1", "permits_unit2", "permits_unit34", "permits_unit5p"],
        "stock": None, "supplySign": 1, "supplyWeight": 0.28,
        "yieldWeight": 0.0, "policyWeight": 0.26, "hazardWeight": 0.06,
        "affordWeight": 0.06, "policyField": "developmentClimate",
        "note": "Scored from the developer's side: permit velocity and a "
                "permissive entitlement climate are the asset. This is the one "
                "class where heavy construction raises the score.",
    },
}

# Headline home-value forecast (unchanged shape, now with employment/permits).
FORECAST_WEIGHTS = {
    "momentum_1y": 0.34,
    "momentum_6m_annualized": 0.20,
    "trend_3y_annualized": 0.13,
    "trend_5y_annualized": 0.08,
    "fhfa_1y": 0.10,
    "population_growth_tilt": 1.10,
    "employment_growth_tilt": 0.55,
    "permit_oversupply_drag": 0.09,
    "rent_yield_tilt": 0.45,
    "overvaluation_drag_per_pti_unit": 0.007,
    "unemployment_drag": 0.18,
    "political_risk_drag": 0.010,
}


def forecast_growth(m, nat):
    """12-month home-value appreciation estimate for a metro."""
    parts = [
        (FORECAST_WEIGHTS["momentum_1y"], m.get("g1")),
        (FORECAST_WEIGHTS["momentum_6m_annualized"], m.get("g6a")),
        (FORECAST_WEIGHTS["trend_3y_annualized"], m.get("g3a")),
        (FORECAST_WEIGHTS["trend_5y_annualized"], m.get("g5a")),
        (FORECAST_WEIGHTS["fhfa_1y"], m.get("fhfaG1")),
    ]
    used = [(w, v) for w, v in parts if v is not None]
    if not used:
        return None
    # Missing components deliberately shrink the estimate toward zero.
    pred = sum(w * v for w, v in used)

    def tilt(field, weight, cap=None):
        nonlocal pred
        v, base = m.get(field), nat.get(field)
        if v is None or base is None:
            return
        delta = v - base
        if cap:
            delta = clamp(delta, -cap, cap)
        pred += weight * delta

    tilt("popGrowth", FORECAST_WEIGHTS["population_growth_tilt"], 0.05)
    tilt("empGrowth", FORECAST_WEIGHTS["employment_growth_tilt"], 0.06)
    tilt("rentYield", FORECAST_WEIGHTS["rent_yield_tilt"], 0.03)

    # Permit overhang: building far above the national rate per capita caps
    # near-term appreciation.
    pi, pi_nat = m.get("permitIntensity"), nat.get("permitIntensity")
    if pi is not None and pi_nat:
        pred -= FORECAST_WEIGHTS["permit_oversupply_drag"] * clamp(
            (pi - pi_nat) / max(pi_nat, 0.5), -1.5, 3.0) * 0.1

    if m.get("pti") is not None and nat.get("pti") is not None and m["pti"] > nat["pti"]:
        pred -= FORECAST_WEIGHTS["overvaluation_drag_per_pti_unit"] * (m["pti"] - nat["pti"])
    if m.get("unemployment") is not None and nat.get("unemployment") is not None:
        pred -= FORECAST_WEIGHTS["unemployment_drag"] * (m["unemployment"] - nat["unemployment"])
    if m.get("politicalRisk") is not None:
        pred -= FORECAST_WEIGHTS["political_risk_drag"] * ((m["politicalRisk"] - 50.0) / 50.0)
    return round(clamp(pred, -0.12, 0.16), 5)


# ------------------------------------------------------------- features ----

def derive_features(m):
    """Per-metro derived inputs used by the asset-class scorer."""
    f = {}
    sectors = m.get("sectors") or {}
    for name, rec in sectors.items():
        if rec.get("g1") is not None:
            f[f"emp_{name}_g1"] = rec["g1"]
        if rec.get("g3a") is not None:
            f[f"emp_{name}_g3a"] = rec["g3a"]
    total = sectors.get("total") or {}
    if total.get("g1") is not None:
        f["empGrowth"] = total["g1"]
    if total.get("g3a") is not None:
        f["empGrowth3a"] = total["g3a"]
    if total.get("wage"):
        f["avgWage"] = total["wage"]

    for key in ("popGrowth", "popGrowth1", "incomeGrowth", "renterShare",
                "gdpGrowth", "rentYield", "permitGrowth1", "permitGrowth3a"):
        if m.get(key) is not None:
            f[key] = m[key]

    pop = m.get("pop")
    units = m.get("housingUnits")
    permits = m.get("permitUnits")
    if permits is not None and pop:
        f["permitIntensity"] = permits / (pop / 1000.0)   # permits per 1k people
    if m.get("stockApartment") is not None and units:
        f["densityProxy"] = m["stockApartment"] + (m.get("stockTownhome") or 0)

    # Supply pressure per class: permits of that type against existing stock of
    # that type. Falls back to a per-capita rate when ACS stock is unavailable.
    for cls, spec in ASSET_CLASSES.items():
        if not spec["supply"]:
            continue
        added = sum(m.get(p) or 0 for p in spec["supply"])
        if not added:
            continue
        stock_share = m.get(spec["stock"]) if spec["stock"] else None
        if stock_share and units:
            existing = max(units * stock_share, 250.0)
            f[f"supply_{cls}"] = added / existing
        elif pop:
            f[f"supply_{cls}"] = added / (pop / 1000.0) / 10.0

    # Establishment density, for the saturation term on retail/hospitality/F&B.
    for spec in ASSET_CLASSES.values():
        driver = spec.get("saturationDriver")
        if driver and pop:
            estabs = (sectors.get(driver) or {}).get("estabs")
            if estabs:
                f[f"estabPerCapita_{driver}"] = estabs / (pop / 100000.0)
        growth_driver = spec.get("estabDriver")
        if growth_driver:
            estabs = (sectors.get(growth_driver) or {}).get("estabs")
            if estabs and pop:
                f[f"estabPerCapita_{growth_driver}"] = estabs / (pop / 100000.0)

    if m.get("politicalRisk") is not None:
        f["politicalRisk"] = m["politicalRisk"]
    if m.get("developmentClimate") is not None:
        f["developmentClimate"] = m["developmentClimate"]
    if m.get("hazardRisk") is not None:
        f["hazardRisk"] = m["hazardRisk"]
    if m.get("pti") is not None:
        f["pti"] = m["pti"]
    if m.get("zhvi") is not None:
        f["zhvi"] = m["zhvi"]

    state = (m.get("state") or "").split("-")[0].strip().upper()
    if state in STATE_POWER_CENTS:
        f["powerCents"] = STATE_POWER_CENTS[state]
    return f


def score_all(metros):
    """Attach per-asset-class scores to every metro.

    Everything is percentile-ranked across the metro universe before being
    combined, so features measured in different units (percent growth, permits
    per capita, cents per kWh) contribute on one comparable 0-100 scale and no
    single outlier can dominate.
    """
    for m in metros:
        m["_f"] = derive_features(m)

    feature_names = set()
    for m in metros:
        feature_names.update(m["_f"])
    pct = {}
    for name in feature_names:
        pct[name] = percentile_ranks([(m["id"], m["_f"].get(name)) for m in metros])

    def rank(m, name, default=50.0):
        return pct.get(name, {}).get(m["id"], default)

    for m in metros:
        scores, detail = {}, {}
        for cls, spec in ASSET_CLASSES.items():
            # ---- demand: weighted percentile blend, renormalised over the
            # drivers this metro actually has data for.
            num = den = 0.0
            present = 0
            for feat, w in spec["demand"]:
                if m["_f"].get(feat) is None:
                    continue
                num += w * rank(m, feat)
                den += w
                present += 1
            if den <= 0 or present < 2:
                scores[cls] = None
                continue
            demand = num / den

            terms = [(demand, 1.0)]

            # ---- supply: penalise (or, for land, reward) new construction.
            sw = spec.get("supplyWeight", 0.0)
            if sw and f"supply_{cls}" in pct:
                s_rank = rank(m, f"supply_{cls}")
                terms.append((s_rank if spec["supplySign"] > 0 else 100.0 - s_rank, sw))

            # ---- yield and affordability.
            if spec.get("yieldWeight"):
                terms.append((rank(m, "rentYield"), spec["yieldWeight"]))
            if spec.get("affordWeight"):
                afford = 100.0 - rank(m, "pti") if "pti" in pct and m["_f"].get("pti") is not None \
                    else 100.0 - rank(m, "zhvi")
                terms.append((afford, spec["affordWeight"]))

            # ---- policy: development-led classes read the development climate,
            # income-producing classes read overall political risk.
            if spec.get("policyWeight"):
                if spec.get("policyField") == "developmentClimate" and m["_f"].get("developmentClimate") is not None:
                    terms.append((rank(m, "developmentClimate"), spec["policyWeight"]))
                elif m["_f"].get("politicalRisk") is not None:
                    terms.append((100.0 - rank(m, "politicalRisk"), spec["policyWeight"]))

            if spec.get("hazardWeight") and m["_f"].get("hazardRisk") is not None:
                terms.append((100.0 - rank(m, "hazardRisk"), spec["hazardWeight"]))

            if spec.get("powerWeight") and m["_f"].get("powerCents") is not None:
                terms.append((100.0 - rank(m, "powerCents"), spec["powerWeight"]))

            sat = spec.get("saturationDriver")
            if spec.get("saturationWeight") and sat and m["_f"].get(f"estabPerCapita_{sat}") is not None:
                terms.append((100.0 - rank(m, f"estabPerCapita_{sat}"), spec["saturationWeight"]))

            total_w = sum(w for _, w in terms)
            scores[cls] = round(sum(v * w for v, w in terms) / total_w, 1)
            detail[cls] = {
                "demand": round(demand, 1),
                "supply": round(rank(m, f"supply_{cls}"), 1) if f"supply_{cls}" in pct
                          and m["_f"].get(f"supply_{cls}") is not None else None,
                "drivers": present,
            }
        m["assetScoresRaw"] = scores
        m["assetDetail"] = detail

    # Averaging many percentiles pulls every metro toward 50 (the central-limit
    # squeeze), which would leave the dashboard's colour scale unusable. Re-rank
    # each class's composite across the universe so a score reads directly as
    # "this metro is in the Nth percentile for this asset class".
    for cls in ASSET_CLASSES:
        spread = percentile_ranks([(m["id"], m["assetScoresRaw"].get(cls)) for m in metros])
        for m in metros:
            m.setdefault("assetScores", {})[cls] = (
                round(spread[m["id"]], 1) if m["id"] in spread else None)

    # Rank each class so the dashboard can show "#3 of 894 for industrial".
    for cls in ASSET_CLASSES:
        ordered = sorted((m for m in metros if m["assetScores"].get(cls) is not None),
                         key=lambda x: -x["assetScores"][cls])
        for i, m in enumerate(ordered, 1):
            m.setdefault("assetRank", {})[cls] = i

    for m in metros:
        m.pop("_f", None)
        m.pop("assetScoresRaw", None)
    return metros


def national_baselines(metros):
    """Median of each field the forecast tilts against."""
    fields = ("zhvi", "g1", "g3a", "rentYield", "pti", "popGrowth", "empGrowth",
              "unemployment", "incomeGrowth", "permitIntensity", "politicalRisk",
              "hazardRisk", "fhfaG1", "renterShare")
    nat = {}
    for f in fields:
        v = median([m.get(f) for m in metros])
        if v is not None:
            nat[f] = v
    return nat


def model_meta():
    return {
        "version": MODEL_VERSION,
        "type": ("percentile-ranked, multi-source composite — a transparent "
                 "weighted model, not a trained ML predictor"),
        "forecastWeights": FORECAST_WEIGHTS,
        "assetClasses": {
            k: {"label": v["label"], "group": v["group"], "note": v["note"],
                "demandDrivers": [d for d, _ in v["demand"]],
                "supplySign": v["supplySign"],
                "supplyInputs": v["supply"]}
            for k, v in ASSET_CLASSES.items()
        },
    }
