# Immobiliare Scorer

Fetch live sale listings from Immobiliare.it and score each one against
OMI (Agenzia delle Entrate) benchmark data for rental yield and value.

## Setup

```bash
pip install requests
```

Python 3.8+ required. No other dependencies.

## Quick start

```bash
# Napoli + Milano (default), 3 pages each (~75 listings per city)
python fetch_listings.py

# Single city
python fetch_listings.py --city roma

# All configured cities
python fetch_listings.py --all-cities --pages 5

# With price/size filters
python fetch_listings.py --city napoli --max-price 250000 --min-sqm 50

# Custom output prefix
python fetch_listings.py --output napoli_run1
```

## Output files

Each run produces two files with a timestamp:

| File | Use |
|------|-----|
| `listings_YYYYMMDD_HHMMSS.csv` | Open in Excel, filter/sort freely |
| `listings_YYYYMMDD_HHMMSS.json` | Paste into the HTML dashboard tool |

## CSV columns explained

| Column | Description |
|--------|-------------|
| `id` | Immobiliare.it listing ID |
| `city` / `city_key` | City label and lowercase key |
| `neighbourhood` | Micro/macrozone from the listing |
| `address` | Street address |
| `price` | Asking price € |
| `sqm` | Surface m² |
| `ask_psqm` | Asking price per m² (price ÷ sqm) |
| `rooms` | Number of rooms |
| `floor` | Floor abbreviation |
| `elevator` | Has elevator (true/false) |
| `condition` | Property condition text |
| `url` | Direct link to listing |
| `omi_zone` | Matched OMI zone name |
| `omi_fascia` | A / B / C / D |
| `omi_bench` | OMI mid-range benchmark €/m² |
| `omi_bmin` / `omi_bmax` | OMI min/max buy price €/m² |
| `omi_rmin` / `omi_rmax` | OMI min/max rent €/m²/month |
| `vs_omi_pct` | % above/below OMI benchmark (negative = cheaper) |
| `vs_omi_label` | Human label: "12% below OMI", "at OMI benchmark", etc. |
| `est_rent_mo` | Estimated monthly rent € (OMI mid rent × sqm) |
| `est_yield_pct` | Estimated gross yield % |
| `fascia_pct` | Percentile within fascia (0=cheapest, 100=most expensive) |
| `fascia_label` | "cheap in fascia B", "mid in fascia A", etc. |
| `score_price` | Sub-score: price vs OMI (0–100) |
| `score_yield` | Sub-score: estimated yield (0–100) |
| `score_fascia` | Sub-score: within-fascia positioning (0–100) |
| `score_total` | **Composite score** (0–100): 40% price + 35% yield + 25% fascia |

## Scoring logic

```
score_total = 0.40 × score_price + 0.35 × score_yield + 0.25 × score_fascia
```

- **score_price**: how cheap the listing is vs the OMI benchmark for its zone.
  −20% under bench → 100. At bench → 50. +20% over → 0.
- **score_yield**: estimated gross yield quality.
  7%+ → 100. 5% → 70. 4% → 50. 2.5% → 0.
- **score_fascia**: where the listing sits within all fetched listings in the
  same fascia (A/B/C). Cheapest → 100, most expensive → 0.

> **Note**: yields are *gross* estimates. Subtract ~1.5–2pp for cedolare secca
> (21%), maintenance, vacancy, and agency fees to get net yield.

## OMI fascia corrections applied

These neighbourhoods are reassigned from their raw geographic ring to their
actual market character:

| Neighbourhood | Raw fascia | Corrected |
|---|---|---|
| Sanità (NA) | A | B |
| Tribunali (NA) | A | B |
| Materdei (NA) | A | B |
| Avvocata (NA) | A | B |
| Garibaldi (NA) | A | B |
| Scampia (NA) | B | C |
| Ponticelli (NA) | B | C |
| Barra (NA) | B | C |
| Porta Romana (MI) | B | A |

## Adding more cities

Add an entry to `CITIES` in `fetch_listings.py`:
```python
"venezia": {"idComune": "27042", "label": "Venezia", "url_path": "/vendita-case/venezia/"},
```
Then add OMI zone data under `OMI["venezia"]` and a fallback in `CITY_FALLBACKS`.

## Refreshing data

Run the script again — it always produces a new timestamped file.
The JSON output can be pasted directly into the HTML dashboard tool to replace
the embedded listings.

## Rate limiting

The script defaults to 1.2s between page requests per city. Increase `--delay`
if you see connection errors. Immobiliare.it does not require authentication for
read-only search requests.
