#!/usr/bin/env python3
"""
explain.py
──────────
Conversational Lume-voice explanation generator for scored listings.
Produces 3–4 short bullets stored as `score_explanation` on each listing.

The voice rule: Lume is the narrator. Calm, slightly Italian, never corporate.
Compares listings to "similar flats nearby" (= similar flats nearby), never says
the word "comps" in user-facing prose. Internal field names are unchanged.
"""

from __future__ import annotations


# ── Verdict lead bullet ─────────────────────────────────────────────────────

def _verdict_lead(score: int) -> str:
    if score >= 85: return "Lume thinks this is a hidden gem — well worth a look."
    if score >= 72: return "Lume rates this solid for the area."
    if score >= 60: return "Lume's read: reasonable, with caveats below."
    if score >= 45: return "Lume thinks this is overpriced for what you get."
    return "Lume sees through this one — not worth your time."


def _price_line(delta: float | None, n: int, kind: str = "flats") -> str | None:
    """`kind` = 'flats' for rentals, 'sales' for sale listings."""
    if delta is None:
        return None
    if delta <= -10:
        return (f"It's {abs(delta):.0f}% below similar flats nearby — Lume found "
                f"{n} similar {kind} nearby and this one's cheaper than most.")
    if delta <= -3:
        return f"Slightly below similar flats nearby at {abs(delta):.0f}% below comparable {kind}."
    if delta <= 5:
        return "Priced in line with similar flats nearby."
    if delta <= 15:
        return f"It's {delta:.0f}% above similar flats nearby. Not extreme, but not a bargain."
    return f"Significantly above similar flats nearby at {delta:.0f}% above comparable {kind}."


def _location_line(listing: dict) -> str | None:
    m = listing.get("metro_walk_min")
    name = listing.get("metro_nearest_name") or "the metro"
    line = listing.get("metro_nearest_line") or ""
    line_str = f" ({line})" if line else ""
    if m is None:
        return None
    if m <= 5:
        return f"{m} minutes from {name}{line_str} — Lume rates this excellent transport."
    if m <= 12:
        return f"{m} minutes from {name}{line_str} — decent for getting around."
    return f"Nearest metro is {m} minutes away — limited transport."


def _property_positive_line(listing: dict) -> str | None:
    cond = (listing.get("condition") or "").lower()
    floor = listing.get("floor_n")
    has_lift = listing.get("elevator") is True
    if any(k in cond for k in ("ottim", "ristruttur", "nuovo")) \
            and not ("da " in cond and "ristruttur" in cond):
        if floor and floor >= 3 and has_lift:
            return f"Renovated, floor {floor} with lift — Lume likes this combination."
        return "Recently renovated — move-in ready."
    return None


def _property_negative_line(listing: dict) -> str | None:
    floor = listing.get("floor_n")
    if floor is not None and floor < 0:
        return "Below ground floor — Lume would walk away from this one."
    if floor == 0:
        return "Ground floor — light and noise are usually issues."
    if floor and floor > 2 and listing.get("elevator") is False:
        return f"Floor {floor} without a lift — Lume notes this is a real drawback."
    eff_flag = listing.get("_room_efficiency_flag")
    sqm  = listing.get("sqm") or 0
    rms  = listing.get("rooms") or 0
    if eff_flag == "severe" and sqm and rms:
        return f"Severely cramped — {sqm}m² across {rms} rooms. Lume notes this."
    if eff_flag == "tight" and sqm and rms:
        return f"Tight rooms — {sqm}m² across {rms} rooms. Tight is the word."
    if eff_flag == "micro_studio":
        return "Micro studio — under 30m². Lume sees this as too small to live in comfortably."
    if eff_flag == "small_studio":
        return "Small studio — under 38m². Lume notes this is on the cramped side."
    if eff_flag == "compact_studio":
        return "Compact studio — under 45m². Workable, but tight."
    return None


def _dom_line(dom: int | None) -> str | None:
    if not dom:
        return None
    if dom > 90:
        return f"Listed for {dom} days — Lume's been watching, this one's not moving."
    if dom > 45:
        return f"On the market {dom} days — slow mover."
    return None


# ── RENTALS ─────────────────────────────────────────────────────────────────

def explain(listing: dict) -> list[str]:
    """
    Return up to 4 conversational bullets explaining the rental score.
    Called after score_rental() so all comps_/score_ fields are populated.
    """
    bullets: list[str] = []

    score = listing.get("score_total") or 0
    delta = listing.get("comps_delta_pct")
    n_c   = listing.get("comps_n") or 0

    # 1. Verdict lead — Lume's overall read
    bullets.append(_verdict_lead(score))

    # 2. Price-vs-neighbours line
    pline = _price_line(delta, n_c, "flats")
    if pline:
        bullets.append(pline)

    # 3. Location
    lline = _location_line(listing)
    if lline:
        bullets.append(lline)

    # 4. Property positive OR negative (whichever is more interesting)
    pos = _property_positive_line(listing)
    neg = _property_negative_line(listing)
    if neg:
        bullets.append(neg)
    elif pos:
        bullets.append(pos)

    # Days on market — appended only when very stale and we have spare slot
    dline = _dom_line(listing.get("days_on_market"))
    if dline and len(bullets) < 4:
        bullets.append(dline)

    # ── Override leads (prepended, push the verdict down) ────────────────────

    # Corporate / short-term rental
    if listing.get("_is_corporate_rental"):
        bullets.insert(0, "Lume spotted this is a corporate / short-term rental "
                          "— priced for furnished flexible lets, not standard contracts.")
        if listing.get("_corporate_ceiling_applied"):
            bullets.insert(1, "Lume capped the score at 75 — corporate rental adjustment.")

    # High condo fees
    if listing.get("condo_fee_flag") == "high_condo_fees":
        condo = listing.get("spese_condominiali") or listing.get("condominium_fees") or 0
        rent  = listing.get("rent_mo") or listing.get("price") or 0
        eff   = listing.get("ask_psqm_effective") or 0
        try: pct = round(float(condo) / float(rent) * 100) if rent else 0
        except (TypeError, ValueError): pct = 0
        bullets.insert(0, (f"Lume noticed high condo fees — €{condo:,.0f}/mo "
                           f"adds {pct}% on top. Effective €{eff:.1f}/m²/mo."))

    # Absolute-value gate
    if listing.get("_absolute_value_gate_applied"):
        apq = listing.get("ask_psqm") or 0
        asm = listing.get("sqm") or 0
        bullets.insert(0, (f"Lume capped this — €{apq:,.0f}/m² on a {asm}m² flat is "
                           f"poor absolute value regardless of similar flats nearby."))

    # Score-was-capped (above-comps ceiling)
    if (listing.get("score_was_capped") and not listing.get("_absolute_value_gate_applied")
            and not listing.get("_corporate_ceiling_applied")
            and delta is not None and delta > 0):
        bullets.insert(0, (f"Lume capped this — asking {delta:.0f}% above similar flats nearby."))

    return bullets[:4]


def explain_all(listings: list[dict]) -> list[dict]:
    """Add score_explanation + score_reasons fields to each rental listing."""
    for l in listings:
        l["score_explanation"] = explain(l)
        l["score_reasons"]     = score_reasons(l)
    return listings


# ── Score reasons (component-tagged bullets for the callout box) ────────────
#
# Each reason carries:
#   component  — 'Price' | 'Property' | 'Location' | 'Penalty'
#                (matches the rows shown in the score-breakdown panel)
#   sentiment  — 'positive' | 'negative'
#   text       — the explanation, no component prefix (UI prepends a pill)
#   weight     — significance (used only for sorting, dropped before return)
#
# Hard rule: every reason is grounded in a CONCRETE observed signal. We never
# fabricate a reason from a missing field. A listing with sparse data simply
# produces fewer bullets — that's honest.

def score_reasons(listing: dict) -> list[dict]:
    """Up to 3 component-tagged bullets, sorted by significance."""
    candidates: list[dict] = []
    def add(component, sentiment, weight, text):
        candidates.append({
            "component": component,
            "sentiment": sentiment,
            "weight":    weight,
            "text":      text,
        })

    # ── PRICE COMPONENT ─────────────────────────────────────────────────
    delta_rent = listing.get("comps_delta_pct")
    delta_sale = listing.get("comps_sale_delta_pct")
    delta      = delta_rent if delta_rent is not None else delta_sale
    n_comps    = (listing.get("comps_n") or 0) or (listing.get("comps_sale_n") or 0)

    if delta is not None and n_comps >= 10:
        if delta <= -15:
            add("Price", "positive", 9, f"{abs(delta):.0f}% below similar flats nearby")
        elif delta <= -8:
            add("Price", "positive", 6, f"{abs(delta):.0f}% below similar flats nearby")
        elif delta <= -3:
            # Mid-range below — informational neutral bullet
            add("Price", "neutral",  2,
                f"{abs(delta):.0f}% below comparable listings — modest discount")
        elif delta < 5:
            # In-line band [-3, +5) — neutral, lowest weight
            add("Price", "neutral",  1, "Priced in line with similar flats nearby")
        elif delta < 8:
            # Slight premium band [+5, +8)
            add("Price", "neutral",  2,
                f"{delta:.0f}% above comparable listings — slight premium")
        elif delta < 15:
            add("Price", "negative", 6, f"{delta:.0f}% above similar flats nearby")
        else:
            add("Price", "negative", 9, f"{delta:.0f}% above similar flats nearby")

    # Score capping (gate or corporate ceiling) trumps the delta phrasing
    if listing.get("_absolute_value_gate_applied"):
        cap = listing.get("score_total")
        ask = listing.get("ask_psqm") or listing.get("ask_psqm_rent")
        try:
            ask_str = f"€{float(ask):,.0f}/m²"
        except (TypeError, ValueError):
            ask_str = "Price-per-m²"
        cap_str = f"score capped at {cap}" if cap is not None else "score capped"
        add("Price", "negative", 11,
            f"{ask_str} is poor absolute value — {cap_str}")

    if listing.get("_corporate_ceiling_applied"):
        add("Price", "negative", 8, "Corporate ceiling — score capped at 75")

    # Condo fees pushing effective rent up
    if listing.get("condo_fee_flag") == "high_condo_fees":
        condo = listing.get("spese_condominiali") or listing.get("condominium_fees") or 0
        try:    condo_str = f"€{int(condo):,}/mo"
        except (TypeError, ValueError): condo_str = "High condo fees"
        add("Price", "negative", 7,
            f"{condo_str} condo fees push the effective cost up")

    # Corporate / short-term rental
    if listing.get("_is_corporate_rental"):
        add("Price", "negative", 8,
            "Corporate / short-term rental — priced for furnished flexible lets")

    # ── LOCATION COMPONENT ──────────────────────────────────────────────
    metro_min  = listing.get("metro_walk_min")
    metro_dist = listing.get("metro_nearest_dist_m")
    if metro_min is None and metro_dist:
        # Same fallback the dashboard uses (~70 m/min walking pace)
        metro_min = max(1, round(metro_dist / 70))
    metro_name = listing.get("metro_nearest_name") or ""
    metro_line = listing.get("metro_nearest_line") or ""
    line_str   = f" ({metro_line})" if metro_line else ""
    name_str   = f" to {metro_name}" if metro_name else " to nearest metro"
    if metro_min is not None:
        if metro_min <= 3:
            add("Location", "positive", 7,
                f"{metro_min} min walk{name_str}{line_str}")
        elif metro_min <= 7:
            add("Location", "positive", 4,
                f"{metro_min} min{name_str}{line_str}")
        elif metro_min >= 18:
            add("Location", "negative", 6,
                f"Far from metro — {metro_min} min walk")
        elif metro_min >= 13:
            add("Location", "negative", 4,
                f"Limited transport — {metro_min} min to nearest metro")

    # LDI signals (area desirability)
    ldi   = listing.get("ldi_score") or 0
    score = listing.get("score_total") or 0
    if ldi >= 80 and (delta is not None and delta <= -8):
        add("Location", "positive", 9,
            "Bargain in a highly desirable area — Lume rarely sees this combination")
    elif ldi <= 20 and score >= 70:
        add("Location", "negative", 4,
            "Lower-demand zone — limited resale and rental appeal")

    # ── PROPERTY COMPONENT ──────────────────────────────────────────────
    cond_raw    = (listing.get("condition") or "").lower()
    floor_n     = listing.get("floor_n")
    elevator    = listing.get("elevator")
    bathrooms   = listing.get("bathrooms")
    has_balcony = listing.get("has_balcony")
    year_built  = listing.get("year_built")

    is_renovated = (any(k in cond_raw for k in ("ottim", "ristruttur", "nuovo"))
                    and not ("da " in cond_raw and "ristruttur" in cond_raw))

    # Property strengths — concrete positives, requires ≥2 to fire
    strengths: list[str] = []
    if is_renovated:                                  strengths.append("recently renovated")
    if floor_n is not None and floor_n >= 3 and elevator is True:
        strengths.append(f"floor {floor_n} with lift")
    if bathrooms and bathrooms >= 2:                  strengths.append(f"{bathrooms} bathrooms")
    if has_balcony is True:                           strengths.append("balcony")
    if year_built and year_built >= 2010:             strengths.append("post-2010 build")
    if len(strengths) >= 2:
        text = ", ".join(strengths[:3])
        add("Property", "positive", 5, text[0].upper() + text[1:])

    # Property weaknesses — concrete signals only
    floor_label_l   = (listing.get("floor_label") or "").lower()
    floor_label_raw = str(listing.get("floor_label_raw") or "").strip().upper()
    is_rialzato = ("rialzato" in floor_label_l
                   or floor_label_l.strip() == "r"
                   or floor_label_raw == "R")

    if floor_n is not None and floor_n < 0:
        add("Property", "negative", 9, "Below ground floor — Lume would walk away")
    elif floor_n == 0:
        add("Property", "negative", 6, "Ground floor — light and noise are usually issues")
    elif is_rialzato:
        # Italian "piano rialzato" — slightly elevated above street level but
        # still suffers from street noise, reduced light and limited views.
        add("Property", "negative", 5,
            "Piano rialzato — slightly elevated but street noise and limited light remain")
    elif floor_n is not None and floor_n > 2 and elevator is False:
        add("Property", "negative", 8,
            f"Floor {floor_n} without a lift — significant drawback")

    if "da " in cond_raw and "ristruttur" in cond_raw:
        add("Property", "negative", 6, "Needs renovation — factor in upfront work")
    elif "fatiscent" in cond_raw:
        add("Property", "negative", 7,
            "In poor condition — significant work needed")

    # Room efficiency flags (using our actual flag names)
    sqm   = listing.get("sqm")
    rooms = listing.get("rooms")
    flag  = listing.get("_room_efficiency_flag")
    if flag == "severe" and sqm and rooms:
        add("Property", "negative", 8,
            f"Severely cramped — {sqm}m² across {rooms} rooms")
    elif flag == "tight" and sqm and rooms:
        add("Property", "negative", 5,
            f"Tight rooms — {sqm}m² across {rooms} rooms")
    elif flag == "micro_studio" and sqm:
        add("Property", "negative", 8,
            f"Micro studio at {sqm}m² — very small for daily living")
    elif flag == "small_studio" and sqm:
        add("Property", "negative", 6,
            f"Small studio at {sqm}m² — on the cramped side")
    elif flag == "compact_studio" and sqm:
        add("Property", "negative", 3,
            f"Compact studio at {sqm}m² — workable but tight")

    # Pre-1960 building without renovation
    if year_built and year_built < 1960 and not is_renovated:
        add("Property", "negative", 4,
            f"Built {year_built} and not renovated — likely poor insulation")

    # ── PENALTY COMPONENT ───────────────────────────────────────────────
    # Days on market is a market signal — files under Penalty.
    dom = listing.get("days_on_market")
    if dom is not None:
        if dom > 90:
            add("Penalty", "negative", 5,
                f"On market {dom} days — Lume notices it's not moving")
        elif 0 <= dom <= 7:
            add("Penalty", "positive", 2, "Fresh listing — just appeared")

    # ── Sale-specific: estimated yield ──────────────────────────────────
    yield_pct = listing.get("estimated_yield_pct")
    if yield_pct is not None:
        if yield_pct >= 5.0:
            add("Price", "positive", 6, f"Strong yield — ~{yield_pct:.1f}%/yr gross")
        elif yield_pct < 3.0:
            add("Price", "negative", 5, f"Weak yield — ~{yield_pct:.1f}%/yr gross")

    # ── Fallback bullets for weak components without a specific rule ────
    # Only built from CONCRETE non-null fields — never says "no balcony" when
    # has_balcony is None, only when has_balcony is False.
    existing_neg = {r["component"] for r in candidates if r["sentiment"] == "negative"}

    # PROPERTY fallback ─────────────────────────────────────────────────
    prop_score = listing.get("score_property") or listing.get("score_physical")
    if prop_score is not None and prop_score <= 55 and "Property" not in existing_neg:
        weak_items: list[str] = []
        if floor_n is not None and floor_n <= 2 and elevator is not True:
            # Only fires when floor IS known. Lift state can be False or None
            # ("not True" covers both); the floor itself anchors the claim.
            if elevator is False:
                weak_items.append(f"floor {floor_n} with no lift")
            else:
                weak_items.append(f"floor {floor_n} without confirmed lift")
        if cond_raw in ("buono", "abitabile") and not strengths:
            weak_items.append("basic condition")
        if (year_built is not None and 1960 <= year_built < 1990
                and not is_renovated):
            weak_items.append(f"{year_built}s build, not renovated")
        if has_balcony is False:
            weak_items.append("no outdoor space")

        if weak_items:
            add("Property", "negative", 4,
                "Below average for the area — " + ", ".join(weak_items[:2]))
        else:
            # Generic, score-anchored — references no specific absent field.
            add("Property", "negative", 3,
                "Limited standout features for the asking price")

    # LOCATION fallback ─────────────────────────────────────────────────
    loc_score = listing.get("score_location") or listing.get("score_geo")
    if loc_score is not None and loc_score <= 55 and "Location" not in existing_neg:
        weak_items = []
        if metro_min is not None and metro_min >= 10:
            weak_items.append(f"{metro_min} min to nearest metro")
        park_dist = listing.get("park_nearest_dist_m")
        if park_dist is not None and park_dist > 600:
            weak_items.append("no park within 600m")
        super_dist = listing.get("supermarket_nearest_dist_m")
        if super_dist is not None and super_dist > 500:
            weak_items.append(f"nearest supermarket {int(super_dist)}m away")

        if weak_items:
            add("Location", "negative", 4,
                "Below-average location — " + ", ".join(weak_items[:2]))
        else:
            add("Location", "negative", 3,
                "Quieter / less-connected pocket of the city")

    candidates.sort(key=lambda r: -r["weight"])
    return [
        {"component": r["component"], "sentiment": r["sentiment"], "text": r["text"]}
        for r in candidates[:3]
    ]


# ── SALES ───────────────────────────────────────────────────────────────────

def _sale_yield_line(yield_pct: float | None) -> str | None:
    if yield_pct is None:
        return None
    if yield_pct >= 5.0:
        return f"Lume estimates ~{yield_pct:.1f}%/yr gross yield — strong for Milan."
    if yield_pct >= 3.5:
        return f"Lume estimates ~{yield_pct:.1f}%/yr gross yield — fair, not exciting."
    return f"Lume estimates ~{yield_pct:.1f}%/yr gross yield — weak yield, watch out."


def explain_sale(listing: dict) -> list[str]:
    """
    Return up to 4 conversational bullets explaining the sale score.
    """
    bullets: list[str] = []

    score = listing.get("score_total") or 0
    delta = listing.get("comps_sale_delta_pct")
    n_c   = listing.get("comps_sale_n") or 0

    # 1. Verdict
    bullets.append(_verdict_lead(score))

    # 2. Price-vs-neighbours
    pline = _price_line(delta, n_c, "sales")
    if pline:
        bullets.append(pline)

    # 3. Yield
    yline = _sale_yield_line(listing.get("estimated_yield_pct"))
    if yline:
        bullets.append(yline)

    # 4. Property positive / negative
    pos = _property_positive_line(listing)
    neg = _property_negative_line(listing)
    if neg:
        bullets.append(neg)
    elif pos:
        bullets.append(pos)

    # OMI ceiling check (sale-specific)
    apq = listing.get("ask_psqm") or 0
    omi_max = listing.get("omi_compr_max")
    if (apq > 0 and omi_max and apq > omi_max * 1.3
            and len(bullets) < 4):
        bullets.append(
            f"€{apq:,.0f}/m² is well above the OMI ceiling — Lume sees overreach."
        )

    # ── Override leads ─────────────────────────────────────────────────────

    if listing.get("_is_corporate_rental"):
        bullets.insert(0, "Lume spotted this is corporate-marketed — "
                          "priced for buyers expecting furnished/flexible terms.")
        if listing.get("_corporate_ceiling_applied"):
            bullets.insert(1, "Lume capped the score at 75 — corporate adjustment.")

    if listing.get("_absolute_value_gate_applied"):
        asm = listing.get("sqm") or 0
        bullets.insert(0, (f"Lume capped this — €{apq:,.0f}/m² on a {asm}m² flat is "
                           f"poor absolute value regardless of similar flats nearby."))

    if (listing.get("score_was_capped") and not listing.get("_absolute_value_gate_applied")
            and not listing.get("_corporate_ceiling_applied")
            and delta is not None and delta > 0):
        bullets.insert(0, f"Lume capped this — asking {delta:.0f}% above similar flats nearby.")

    return bullets[:4]


def explain_all_sales(listings: list[dict]) -> list[dict]:
    """Add score_explanation + score_reasons fields to each sale listing."""
    for l in listings:
        l["score_explanation"] = explain_sale(l)
        l["score_reasons"]     = score_reasons(l)
    return listings
