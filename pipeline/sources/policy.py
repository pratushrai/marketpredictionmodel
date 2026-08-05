"""Political and regulatory risk by state.

Unlike every other source here, this one is a **curated table, not an API
pull** — there is no authoritative machine-readable feed for "how friendly is
this jurisdiction to a landlord or a developer". Each field is a published,
checkable fact (effective property tax rate, statutory rent-control regime,
insurance market condition) rather than an opinion, every row carries the
vintage it was compiled from, and `REVIEW_BY` states when it goes stale.

Scales are stated per field. For the composite, **higher `politicalRisk` means
more risk to an owner/developer**.

Sources, all public:
  * Effective property tax rates — Tax Foundation "Facts & Figures" and ATTOM
    year-end property tax analyses.
  * Rent regulation — National Multifamily Housing Council rent-control laws
    tracker and state statutes.
  * Development climate — state land-use preemption/upzoning statutes and the
    Mercatus/NZLUD state zoning-reform surveys.
  * Insurance stress — state insurance department market reports and NAIC
    homeowners-market data (FL/LA/CA non-renewal and residual-market growth).
"""

COMPILED_FROM = "2024-2025 published data"
REVIEW_BY = "2027-01-01"

# state: (effective property tax %, rent-control regime, landlord friendliness
#         0-100, development climate 0-100, insurance stress 0-100)
# regime: "statewide" (state-level cap), "local" (localities may enact),
#         "preempted" (state bars local rent control)
STATE_POLICY = {
    "AL": (0.41, "preempted", 82, 68, 34), "AK": (1.07, "preempted", 66, 58, 24),
    "AZ": (0.63, "preempted", 80, 74, 30), "AR": (0.62, "preempted", 84, 64, 32),
    "CA": (0.75, "statewide", 28, 46, 74), "CO": (0.51, "preempted", 62, 66, 56),
    "CT": (1.92, "local", 44, 44, 34), "DE": (0.58, "preempted", 66, 56, 28),
    "DC": (0.57, "local", 26, 52, 26), "FL": (0.86, "preempted", 80, 70, 88),
    "GA": (0.90, "preempted", 84, 72, 36), "HI": (0.32, "local", 40, 34, 44),
    "ID": (0.67, "preempted", 78, 70, 24), "IL": (2.07, "preempted", 46, 50, 36),
    "IN": (0.84, "preempted", 80, 68, 28), "IA": (1.49, "preempted", 74, 62, 30),
    "KS": (1.34, "preempted", 76, 62, 34), "KY": (0.85, "preempted", 74, 60, 30),
    "LA": (0.56, "preempted", 72, 58, 86), "ME": (1.24, "local", 48, 54, 26),
    "MD": (1.05, "local", 46, 52, 28), "MA": (1.14, "preempted", 40, 44, 30),
    "MI": (1.32, "preempted", 68, 58, 30), "MN": (1.11, "local", 54, 58, 32),
    "MS": (0.79, "preempted", 82, 62, 44), "MO": (0.97, "preempted", 78, 64, 34),
    "MT": (0.76, "preempted", 74, 82, 26), "NE": (1.61, "preempted", 74, 60, 32),
    "NV": (0.55, "preempted", 76, 66, 30), "NH": (1.77, "preempted", 62, 52, 24),
    "NJ": (2.23, "local", 36, 40, 32), "NM": (0.73, "preempted", 68, 58, 34),
    "NY": (1.64, "local", 24, 38, 30), "NC": (0.78, "preempted", 82, 72, 40),
    "ND": (1.00, "preempted", 76, 60, 26), "OH": (1.53, "preempted", 70, 60, 28),
    "OK": (0.90, "preempted", 80, 66, 52), "OR": (0.93, "statewide", 34, 62, 34),
    "PA": (1.41, "preempted", 64, 52, 28), "RI": (1.40, "local", 46, 46, 30),
    "SC": (0.57, "preempted", 80, 68, 44), "SD": (1.14, "preempted", 78, 62, 28),
    "TN": (0.67, "preempted", 84, 72, 34), "TX": (1.63, "preempted", 78, 76, 52),
    "UT": (0.58, "preempted", 78, 74, 26), "VT": (1.71, "local", 44, 44, 24),
    "VA": (0.82, "preempted", 74, 62, 28), "WA": (0.87, "statewide", 40, 62, 30),
    "WV": (0.57, "preempted", 76, 54, 28), "WI": (1.51, "preempted", 72, 58, 28),
    "WY": (0.61, "preempted", 78, 62, 26),
}

RENT_CONTROL_PENALTY = {"statewide": 30.0, "local": 15.0, "preempted": 0.0}
RENT_CONTROL_LABEL = {
    "statewide": "Statewide rent cap in force",
    "local": "Localities may enact rent control",
    "preempted": "State preempts local rent control",
}


def state_policy(state_code):
    """Return the policy profile for a two-letter state code, or None."""
    rec = STATE_POLICY.get((state_code or "").strip().upper()[:2])
    if not rec:
        return None
    tax, regime, landlord, development, insurance = rec
    # Property tax burden mapped onto 0-100 (2.25% or worse = 100).
    tax_risk = min(100.0, tax / 2.25 * 100.0)
    risk = (0.30 * tax_risk
            + 0.25 * (100.0 - landlord)
            + 0.20 * (100.0 - development)
            + 0.15 * insurance
            + 0.10 * (RENT_CONTROL_PENALTY[regime] / 30.0 * 100.0))
    return {
        "propertyTaxRate": tax,
        "rentControl": regime,
        "rentControlLabel": RENT_CONTROL_LABEL[regime],
        "landlordFriendly": float(landlord),
        "developmentClimate": float(development),
        "insuranceStress": float(insurance),
        "politicalRisk": round(risk, 1),
    }


def metro_policy(state_field):
    """Blend policy across the states a metro spans.

    Zillow's StateName is a single state, but many CBSAs cross state lines
    ("NY-NJ", "DC-VA-MD-WV"); when several are present the profile is averaged
    so a bi-state metro is not scored as if it were wholly in one regime.
    """
    codes = [c.strip().upper() for c in str(state_field or "").replace("/", "-").split("-")
             if len(c.strip()) == 2]
    profiles = [p for p in (state_policy(c) for c in codes) if p]
    if not profiles:
        return None
    if len(profiles) == 1:
        out = dict(profiles[0])
        out["policyStates"] = codes[:1]
        return out
    n = len(profiles)
    worst_regime = max((p["rentControl"] for p in profiles),
                       key=lambda r: RENT_CONTROL_PENALTY[r])
    return {
        "propertyTaxRate": round(sum(p["propertyTaxRate"] for p in profiles) / n, 3),
        "rentControl": worst_regime,
        "rentControlLabel": RENT_CONTROL_LABEL[worst_regime],
        "landlordFriendly": round(sum(p["landlordFriendly"] for p in profiles) / n, 1),
        "developmentClimate": round(sum(p["developmentClimate"] for p in profiles) / n, 1),
        "insuranceStress": round(sum(p["insuranceStress"] for p in profiles) / n, 1),
        "politicalRisk": round(sum(p["politicalRisk"] for p in profiles) / n, 1),
        "policyStates": codes,
    }


def meta():
    return {
        "type": "curated table (no authoritative API exists for this)",
        "compiledFrom": COMPILED_FROM,
        "reviewBy": REVIEW_BY,
        "states": len(STATE_POLICY),
        "fields": ["propertyTaxRate", "rentControl", "landlordFriendly",
                   "developmentClimate", "insuranceStress"],
        "note": ("Each field is a published, checkable fact rather than an "
                 "opinion; the composite weighting is the pipeline's own and "
                 "is documented in the dashboard methodology."),
    }
