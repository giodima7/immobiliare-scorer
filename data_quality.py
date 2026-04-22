"""
data_quality.py — Audit completeness and enrichment quality of rental/sale listings.

Usage:
    import data_quality as dq
    result = dq.audit_all(listings)              # rentals
    result = dq.audit_all(listings, mode='sale') # sales
    single = dq.audit_single(listing)            # single listing dict
    single = dq.audit_single(listing, mode='sale')
"""

FIELDS = {
    "critical": [
        "id", "rent_mo", "sqm", "ask_psqm",
        "latitude", "longitude",
        "omi_zona", "omi_loc_min", "omi_loc_max", "omi_source",
    ],
    "important": [
        "floor_n", "elevator", "condition", "is_external",
        "rooms", "address", "neighbourhood",
        "metro_nearest_dist_m", "metro_walk_min", "metro_nearest_line",
        "comps_median", "comps_n", "comps_confidence",
        "geo_score", "score_physical",
    ],
    "useful": [
        "has_balcony", "has_parking", "energy_class", "heating_type",
        "furnished", "bathrooms", "spese_condominiali",
        "days_on_market", "photo_count",
        "tram_nearest_dist_m", "supermarket_nearest_dist_m",
        "park_nearest_dist_m", "university_nearest_dist_m",
        "score_explanation", "suggested_rent_mo",
    ],
}

SALE_FIELDS = {
    "critical": [
        "id", "price", "sqm", "ask_psqm",
        "latitude", "longitude",
        "omi_zona", "omi_compr_min", "omi_compr_max", "omi_source",
    ],
    "important": [
        "floor_n", "elevator", "condition", "is_external",
        "rooms", "address", "neighbourhood",
        "metro_nearest_dist_m", "metro_walk_min", "metro_nearest_line",
        "comps_sale_median", "comps_sale_n", "comps_sale_confidence",
        "geo_score", "score_physical",
    ],
    "useful": [
        "has_balcony", "has_parking", "energy_class", "heating_type",
        "furnished", "bathrooms",
        "days_on_market", "photo_count",
        "tram_nearest_dist_m", "supermarket_nearest_dist_m",
        "park_nearest_dist_m", "university_nearest_dist_m",
        "estimated_yield_pct", "omi_compr_mid",
        "score_explanation",
    ],
}


def _make_weights(fields: dict) -> tuple[dict, int, list]:
    """Build (weights_dict, weight_total, all_fields) from a FIELDS-style dict."""
    all_fields = fields["critical"] + fields["important"] + fields["useful"]
    weights = (
        {f: 3 for f in fields["critical"]}
        | {f: 2 for f in fields["important"]}
        | {f: 1 for f in fields["useful"]}
    )
    return weights, sum(weights.values()), all_fields


_WEIGHTS, _WEIGHT_TOTAL, _ALL_FIELDS = _make_weights(FIELDS)
_SALE_WEIGHTS, _SALE_WEIGHT_TOTAL, _SALE_ALL_FIELDS = _make_weights(SALE_FIELDS)

_BAD_OMI_SOURCES = {"keyword_fallback", "no_coordinates", "failed"}
_ZERO_FIELDS = {"rent_mo", "sqm", "ask_psqm", "price"}


def _is_present(field: str, value) -> bool:
    """Return True if the value is considered populated for this field."""
    if value is None:
        return False
    if isinstance(value, bool):
        return True   # False is a known value, not missing
    if isinstance(value, str) and value == "":
        return False
    if field in _ZERO_FIELDS and value == 0:
        return False
    return True


def _grade(pct: float) -> str:
    if pct >= 90:
        return "A"
    if pct >= 75:
        return "B"
    if pct >= 60:
        return "C"
    if pct >= 40:
        return "D"
    return "F"


def audit_single(listing: dict, mode: str = 'rent') -> dict:
    """Return a quality audit dict for a single listing."""
    fields  = SALE_FIELDS  if mode == 'sale' else FIELDS
    weights = _SALE_WEIGHTS if mode == 'sale' else _WEIGHTS
    weight_total = _SALE_WEIGHT_TOTAL if mode == 'sale' else _WEIGHT_TOTAL

    critical_missing: list[str] = []
    important_missing: list[str] = []
    useful_missing: list[str] = []

    weight_present = 0
    critical_present = 0
    important_present = 0

    for group, bucket in (
        ("critical",  critical_missing),
        ("important", important_missing),
        ("useful",    useful_missing),
    ):
        for field in fields[group]:
            val = listing.get(field)
            present = _is_present(field, val)
            if present:
                weight_present += weights[field]
                if group == "critical":
                    critical_present += 1
                elif group == "important":
                    important_present += 1
            else:
                bucket.append(field)

    completeness_pct = round(weight_present / weight_total * 100, 1)
    critical_score   = round(critical_present  / len(fields["critical"])  * 100, 1)
    important_score  = round(important_present / len(fields["important"]) * 100, 1)

    omi_source = listing.get("omi_source") or ""
    needs_reenrichment = bool(critical_missing) or (omi_source in _BAD_OMI_SOURCES)

    return {
        "id":                  listing.get("id"),
        "neighbourhood":       listing.get("neighbourhood"),
        "address":             listing.get("address"),
        "source":              listing.get("source"),
        "omi_source":          listing.get("omi_source"),
        "completeness_pct":    completeness_pct,
        "critical_score":      critical_score,
        "important_score":     important_score,
        "grade":               _grade(completeness_pct),
        "critical_missing":    critical_missing,
        "important_missing":   important_missing,
        "useful_missing":      useful_missing,
        "needs_reenrichment":  needs_reenrichment,
    }


def audit_all(listings: list[dict], mode: str = 'rent') -> dict:
    """Return a full quality audit for a list of listings."""
    all_fields = _SALE_ALL_FIELDS if mode == 'sale' else _ALL_FIELDS
    fields     = SALE_FIELDS      if mode == 'sale' else FIELDS

    empty_summary = {
        "total_listings":                0,
        "avg_completeness_pct":          0.0,
        "grade_distribution":            {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0},
        "field_coverage":                {f: 0.0 for f in all_fields},
        "listings_needing_reenrichment": 0,
        "critical_field_gaps":           {},
        "source_breakdown":              {},
        "omi_source_breakdown":          {},
    }

    if not listings:
        return {"summary": empty_summary, "listings": []}

    audited = [audit_single(l, mode=mode) for l in listings]
    n = len(listings)

    grade_dist = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    source_breakdown: dict[str, int] = {}
    omi_source_breakdown: dict[str, int] = {}
    field_present_count: dict[str, int] = {f: 0 for f in all_fields}
    needs_reenrich_count = 0
    completeness_sum = 0.0

    for listing, audit in zip(listings, audited):
        grade_dist[audit["grade"]] += 1
        completeness_sum += audit["completeness_pct"]
        if audit["needs_reenrichment"]:
            needs_reenrich_count += 1

        src = str(listing.get("source") or "unknown")
        source_breakdown[src] = source_breakdown.get(src, 0) + 1

        omi_src = str(listing.get("omi_source") or "missing")
        omi_source_breakdown[omi_src] = omi_source_breakdown.get(omi_src, 0) + 1

        for field in all_fields:
            if _is_present(field, listing.get(field)):
                field_present_count[field] += 1

    field_coverage = {f: round(cnt / n * 100, 1) for f, cnt in field_present_count.items()}

    # Critical field gaps: only fields with <80% coverage, sorted descending by n_missing
    gap_items = [
        (f, n - field_present_count[f])
        for f in fields["critical"]
        if field_coverage[f] < 80.0
    ]
    gap_items.sort(key=lambda x: -x[1])
    critical_field_gaps = dict(gap_items)

    summary = {
        "total_listings":                n,
        "avg_completeness_pct":          round(completeness_sum / n, 1),
        "grade_distribution":            grade_dist,
        "field_coverage":                field_coverage,
        "listings_needing_reenrichment": needs_reenrich_count,
        "critical_field_gaps":           critical_field_gaps,
        "source_breakdown":              source_breakdown,
        "omi_source_breakdown":          omi_source_breakdown,
    }

    audited_sorted = sorted(audited, key=lambda a: a["completeness_pct"])

    return {"summary": summary, "listings": audited_sorted}
