#!/usr/bin/env python3
"""
explain.py
──────────
Deterministic 3–4 bullet explanation generator for scored listings.
Stored as `score_explanation` list[str] on each listing.

Icon selection: ✓ good  ⚠ neutral/warning  ✗ bad
"""

from __future__ import annotations


def explain(listing: dict) -> list[str]:
    """
    Return 3–4 human-readable bullets explaining the score.

    Called after score_rental() has run, so all score_ and comps_ fields
    are already present on the listing dict.
    """
    bullets: list[str] = []

    hidden_gem = listing.get("hidden_gem", False)
    good_value = listing.get("good_value", False)
    ldi_score  = listing.get("ldi_score")
    gem_delta  = listing.get("comps_delta_pct")  # already in %, e.g. -8.3

    # ── −1. Price ceiling notice (prepended first when score was capped) ──────
    if listing.get("score_was_capped") and gem_delta is not None and gem_delta > 0:
        bullets.insert(0, f"⚠ Score capped — asking {gem_delta:.0f}% above comparable listings nearby")

    # ── 0. Gem bullet (prepended when applicable) ─────────────────────────────
    if hidden_gem and gem_delta is not None and ldi_score is not None:
        bullets.append(
            f"✦ Rare find — desirable zone (LDI {ldi_score}/100) "
            f"priced {gem_delta:.1f}% vs area comps"
        )
    elif good_value and gem_delta is not None:
        bullets.append(
            f"↑ Good value — {abs(gem_delta):.1f}% below comps in a well-located area"
        )

    # ── 1. Price vs comps bullet (always present) ─────────────────────────────
    delta  = listing.get("comps_delta_pct")       # already in %, e.g. 8.3
    n_c    = listing.get("comps_n") or 0
    radius = listing.get("comps_radius_m")
    src    = listing.get("comps_source") or "none"
    conf   = listing.get("comps_conf_label") or "Low"
    score_p = listing.get("score_price")          # 0–100

    if delta is not None and src not in ("none",):
        sign = "+" if delta > 0 else ""
        scope_str = (
            f"{radius}m radius" if radius else
            "OMI zone" if "omi_zone" in src else
            "OMI benchmark"
        )
        if hidden_gem and ldi_score is not None and ldi_score >= 65 and delta <= -8.0:
            bullets.append(
                f"✓ Asking {sign}{delta:.1f}% vs {n_c} comps within {scope_str} "
                f"— prime area (LDI {ldi_score}/100) amplifies value"
            )
        else:
            icon = "✓" if (score_p or 50) >= 60 else ("⚠" if (score_p or 50) >= 35 else "✗")
            bullets.append(
                f"{icon} Asking {sign}{delta:.1f}% vs {n_c} comps within {scope_str} "
                f"(confidence: {conf})"
            )
    else:
        bullets.append("⚠ No local comps available — priced against OMI benchmark only")

    # ── 2. Location bullet ────────────────────────────────────────────────────
    geo_s  = listing.get("geo_score")
    metro  = listing.get("metro_nearest_dist_m")
    metro_n = listing.get("metro_nearest_name") or ""
    tram   = listing.get("tram_nearest_dist_m")
    park   = listing.get("park_nearest_dist_m")

    if geo_s is not None:
        icon = "✓" if geo_s >= 65 else ("⚠" if geo_s >= 40 else "✗")
        parts = []
        if metro is not None:
            label = f"{metro_n} " if metro_n else ""
            parts.append(f"metro {label}{metro}m")
        if tram is not None and tram < 400:
            parts.append(f"tram {tram}m")
        if park is not None and park < 600:
            parts.append(f"park {park}m")
        detail = ", ".join(parts) if parts else "no transport data"
        bullets.append(f"{icon} Location score {geo_s}/100 — {detail}")
    else:
        bullets.append("⚠ Location data not enriched yet")

    # ── 3. Best positive physical feature ─────────────────────────────────────
    prop_s = listing.get("score_property") or listing.get("score_physical") or 50
    positives: list[tuple[int, str]] = []

    floor = listing.get("floor_n")
    if floor is not None and floor >= 3:
        positives.append((10, f"floor {floor}"))
    ec = listing.get("energy_class")
    if ec and ec.upper().startswith("A"):
        positives.append((15, f"energy class {ec}"))
    if listing.get("has_balcony") is True:
        positives.append((8, "balcony/terrace"))
    if listing.get("elevator") is True and floor and floor > 2:
        positives.append((6, "lift"))
    if str(listing.get("heating_type") or "").lower() == "autonomous":
        positives.append((5, "autonomous heating"))
    yr = listing.get("year_built")
    if yr and yr >= 2010:
        positives.append((8, f"built {yr}"))
    if listing.get("is_external") is True:
        positives.append((7, "external-facing"))
    cond = (listing.get("condition") or "").lower()
    if any(k in cond for k in ("ottim", "ristruttur", "nuovo")):
        positives.append((10, f"condition: {listing['condition']}"))

    if positives:
        positives.sort(key=lambda x: -x[0])
        feats = ", ".join(f for _, f in positives[:3])
        icon = "✓"   # always positive — we only list positive features here
        bullets.append(f"{icon} Property highlights: {feats}")

    # ── 4. Worst issue / deal-breaker ─────────────────────────────────────────
    negatives: list[tuple[int, str]] = []

    if floor == 0:
        negatives.append((20, "ground floor"))
    if floor and floor > 4 and listing.get("elevator") is False:
        negatives.append((30, f"floor {floor} with no lift"))
    dom = listing.get("days_on_market") or 0
    if dom > 60:
        negatives.append((25, f"on market {dom} days — may have issues"))
    elif dom > 30:
        negatives.append((10, f"on market {dom} days"))
    spese = listing.get("spese_condominiali") or 0
    rent_mo = listing.get("rent_mo") or 0
    if rent_mo > 0 and spese > rent_mo * 0.20:
        negatives.append((15, f"condo fees €{int(spese)}/mo ({round(spese/rent_mo*100)}% of rent)"))
    if "da " in cond and "ristruttur" in cond:
        negatives.append((12, "needs renovation"))
    sqm = listing.get("sqm") or 0
    rooms = listing.get("rooms") or 0
    if sqm > 0 and rooms > 0 and sqm / rooms < 12:
        negatives.append((15, f"very small rooms ({sqm}m² / {rooms} rooms)"))

    if negatives:
        negatives.sort(key=lambda x: -x[0])
        worst = negatives[0][1]
        bullets.append(f"✗ Watch out: {worst}")
    elif prop_s < 40:
        bullets.append("⚠ Limited physical data — verify in person")

    return bullets[:4]


def explain_all(listings: list[dict]) -> list[dict]:
    """Add score_explanation field to each listing. Mutates in place, also returns."""
    for l in listings:
        l["score_explanation"] = explain(l)
    return listings


def explain_sale(listing: dict) -> list[str]:
    """
    Return 3–4 human-readable bullets explaining the sale listing score.

    Called after score_sale_listing() has run, so all score_ and comps_sale_
    fields are already present on the listing dict.
    """
    bullets: list[str] = []

    hidden_gem = listing.get("hidden_gem", False)
    good_value = listing.get("good_value", False)
    ldi_score  = listing.get("ldi_score")
    gem_delta  = listing.get("comps_sale_delta_pct")  # already in %, e.g. -8.3

    # ── Price ceiling notice ──────────────────────────────────────────────────
    if listing.get("score_was_capped") and gem_delta is not None and gem_delta > 0:
        bullets.insert(0, f"⚠ Score capped — asking {gem_delta:.0f}% above comparable sales nearby")

    # ── Gem bullet ────────────────────────────────────────────────────────────
    if hidden_gem and gem_delta is not None and ldi_score is not None:
        bullets.append(
            f"✦ Rare find — desirable zone (LDI {ldi_score}/100) "
            f"priced {gem_delta:.1f}% vs area sales"
        )
    elif good_value and gem_delta is not None:
        bullets.append(
            f"↑ Good value — {abs(gem_delta):.1f}% below comparable sales in a well-located area"
        )

    # ── 1. Price vs comps bullet ──────────────────────────────────────────────
    delta  = listing.get("comps_sale_delta_pct")
    n_c    = listing.get("comps_sale_n") or 0
    radius = listing.get("comps_sale_radius_m")
    src    = listing.get("comps_sale_source") or "none"
    conf   = listing.get("comps_sale_conf_label") or "Low"
    score_p = listing.get("score_price")

    if delta is not None and src not in ("none",):
        sign = "+" if delta > 0 else ""
        scope_str = (
            f"{radius}m radius" if radius else
            "OMI zone" if "omi_zone" in src else
            "OMI benchmark"
        )
        if hidden_gem and ldi_score is not None and ldi_score >= 65 and delta <= -8.0:
            bullets.append(
                f"✓ Asking {sign}{delta:.1f}% vs {n_c} comparable sales within {scope_str} "
                f"— prime area (LDI {ldi_score}/100) amplifies value"
            )
        else:
            icon = "✓" if (score_p or 50) >= 60 else ("⚠" if (score_p or 50) >= 35 else "✗")
            bullets.append(
                f"{icon} Asking {sign}{delta:.1f}% vs {n_c} comparable sales within {scope_str} "
                f"(confidence: {conf})"
            )
    else:
        bullets.append("⚠ No local sale comps available — priced against OMI benchmark only")

    # ── 2. Estimated yield bullet ─────────────────────────────────────────────
    yield_pct = listing.get("estimated_yield_pct")
    if yield_pct is not None:
        icon = "✓" if yield_pct >= 5.0 else ("⚠" if yield_pct >= 3.5 else "✗")
        bullets.append(f"{icon} Estimated gross yield ~{yield_pct:.1f}%/yr (based on OMI rent data)")
    else:
        vs_omi = listing.get("vs_omi_label")
        if vs_omi:
            bullets.append(f"⚠ {vs_omi} — no yield estimate available")

    # ── 3. Location bullet ────────────────────────────────────────────────────
    geo_s   = listing.get("geo_score")
    metro   = listing.get("metro_nearest_dist_m")
    metro_n = listing.get("metro_nearest_name") or ""
    tram    = listing.get("tram_nearest_dist_m")
    park    = listing.get("park_nearest_dist_m")

    if geo_s is not None:
        icon = "✓" if geo_s >= 65 else ("⚠" if geo_s >= 40 else "✗")
        parts = []
        if metro is not None:
            label = f"{metro_n} " if metro_n else ""
            parts.append(f"metro {label}{metro}m")
        if tram is not None and tram < 400:
            parts.append(f"tram {tram}m")
        if park is not None and park < 600:
            parts.append(f"park {park}m")
        detail = ", ".join(parts) if parts else "no transport data"
        bullets.append(f"{icon} Location score {geo_s}/100 — {detail}")
    else:
        bullets.append("⚠ Location data not enriched yet")

    # ── 4. Physical highlights ────────────────────────────────────────────────
    positives: list[tuple[int, str]] = []

    floor = listing.get("floor_n")
    if floor is not None and floor >= 3:
        positives.append((10, f"floor {floor}"))
    ec = listing.get("energy_class")
    if ec and ec.upper().startswith("A"):
        positives.append((15, f"energy class {ec}"))
    if listing.get("has_balcony") is True:
        positives.append((8, "balcony/terrace"))
    if listing.get("elevator") is True and floor and floor > 2:
        positives.append((6, "lift"))
    if str(listing.get("heating_type") or "").lower() == "autonomous":
        positives.append((5, "autonomous heating"))
    yr = listing.get("year_built")
    if yr and yr >= 2010:
        positives.append((8, f"built {yr}"))
    if listing.get("is_external") is True:
        positives.append((7, "external-facing"))
    cond = (listing.get("condition") or "").lower()
    if any(k in cond for k in ("ottim", "ristruttur", "nuovo")):
        positives.append((10, f"condition: {listing['condition']}"))

    negatives: list[tuple[int, str]] = []

    if floor == 0:
        negatives.append((20, "ground floor"))
    if floor and floor > 4 and listing.get("elevator") is False:
        negatives.append((30, f"floor {floor} with no lift"))
    dom = listing.get("days_on_market") or 0
    if dom > 90:
        negatives.append((25, f"on market {dom} days — may have issues"))
    elif dom > 45:
        negatives.append((10, f"on market {dom} days"))
    ask_psqm = listing.get("ask_psqm") or 0
    omi_compr_max = listing.get("omi_compr_max")
    if ask_psqm > 0 and omi_compr_max and ask_psqm > omi_compr_max * 1.3:
        negatives.append((20, f"€{ask_psqm:,.0f}/m² exceeds OMI ceiling (€{omi_compr_max:,.0f}/m²)"))
    sqm = listing.get("sqm") or 0
    rooms = listing.get("rooms") or 0
    if sqm > 0 and rooms > 0 and sqm / rooms < 12:
        negatives.append((12, f"very small rooms ({sqm}m² / {rooms} rooms)"))
    if "da_ristrutturare" in cond or ("da " in cond and "ristruttur" in cond):
        negatives.append((15, "needs renovation"))

    if positives:
        positives.sort(key=lambda x: -x[0])
        feats = ", ".join(f for _, f in positives[:3])
        bullets.append(f"✓ Property highlights: {feats}")

    if negatives:
        negatives.sort(key=lambda x: -x[0])
        worst = negatives[0][1]
        bullets.append(f"✗ Watch out: {worst}")

    return bullets[:4]


def explain_all_sales(listings: list[dict]) -> list[dict]:
    """Add score_explanation field to each sale listing. Mutates in place, also returns."""
    for l in listings:
        l["score_explanation"] = explain_sale(l)
    return listings
