# Decisions, assumptions and open points

## Build context (read first)

This MVP was built in a cloud sandbox **without network access to any Belgian website** (biddit.be,
immo.notaris.be, agency sites, statbel.fgov.be, waterinfo.be, energiesparen.be all returned a proxy denial).
Consequences:

- No source connector has been run against a live page yet. `make check-live` has only been run in the sandbox,
  where every source fails with a network error. It has **not** passed for 100 sources.
- The tests could not use recorded copies of real pages. They use small hand-written pages that follow the
  schema.org / Open Graph conventions the parser relies on (`tests/pages.py`, labelled as such). This deviates
  from the requirement "never invented HTML". The fix is in place: `immo record-fixture <url> <name>` saves real
  pages and `tests/test_recorded.py` checks every recorded page automatically. Record a few pages per template
  family on the first day of live running and replace the hand-written pages where possible.
- Verified in the sandbox: all 56 tests (93% coverage; 1 skipped until real pages are recorded), ruff, mypy, Alembic migration, and the Docker stack
  (Postgres, web, worker, MailHog) booting with every web page rendering and the worker polling and recording
  source failures.

## Needs verification (first live day)

| Item | Where | How to verify |
|---|---|---|
| Biddit JSON endpoint, params, detail URL | `config/sources.yaml` | biddit.be search page → browser devtools → Network → Fetch/XHR; copy the listings request URL |
| Notary platform JSON endpoint | `config/sources.yaml` | same on immo.notaris.be |
| Zabun / Whise / Omnicasa detail-link patterns and markers | `sources/agency.py` `FAMILIES`, `discovery.py` `MARKERS` | `immo parse <url>` and Parser test on 3 sites per family; record fixtures |
| Discovery directory pages (CIB, BIV) | `config/discovery.yaml` | open the URLs; adjust to pages that actually list agency websites |
| EPC lookup (VEKA "Zoek een EPC") | `EPC_LOOKUP_URL` | no public JSON API known; the adapter expects `{label, score, date}`. EPC label/score are usually in the listing itself (legal duty), so this is a fallback |
| Flood P-/G-score (waterinfo.be) | `FLOOD_LOOKUP_URL` | same; P-/G-scores are also a legal duty in ads and are parsed from the listing text |
| Permits (Omgevingsloket reporting) | `PERMITS_LOOKUP_URL` | find the open-data export; adapter expects `{count, recent}` |
| Statbel file URL and column names | `enrichment/__init__.py` `STATBEL_URL`, `parse_statbel` | `immo statbel-import`; the parser looks for YEAR / REFNIS…NL / TYPE…NL / P25 / P50 / P75 / TRANSACTION columns and raises a clear error otherwise |
| Immoweb / Zimmo newest-listings URLs | `config/sources.yaml` (off) | only after legal review |
| Playwright rendering | `sources/http.py` `_render` | not exercised (no browser sites known yet) |

Each unconfigured or failing adapter returns `None`, logs a warning and never stops the pipeline. A failing source
turns red in Ops/QA.

## Choices

- **One crawler for the three agency template families.** They share the crawl (listing page → detail links →
  detail page), differ in markers and link patterns, and all detail pages are read by the generic extractor
  (JSON-LD → Open Graph → labelled text → optional LLM). Separate per-family parsing code will be added only where
  recorded pages show it is needed.
- **Change detection without re-fetching everything**: each listing card on the index page is hashed; the detail
  page is fetched only when the card is new or changed. A nightly full crawl detects removed listings.
- **The initial backfill does not alert** (it would send hundreds of old homes); its events are marked as matched.
- **Dedup**: the normalised address drops the box number ("bus 3") because sites often omit it; units in one
  building are kept apart by living area (±5% or 3 m²). Without an address: same postal code (or municipality) +
  type + area ±3 m² + price ±2%, rejected when both first photos exist and their dHash differs by more than 10 bits.
- **Unknown EPC or flood score does not block an alert** (speed matters more); the alert says "unknown".
- **One "new" alert per user per property**, even if the home appears on several sources.
- **Quiet hours** hold alerts back; they are sent when quiet hours end (retried every 5 minutes, up to 24 h).
- **Fair value**: three quantile gradient-boosting models (10/50/90%) on log price, features: living area,
  bedrooms, EPC score, type, municipality median €/m² from collected data, Statbel median. Trained only on real
  collected sale listings (houses and apartments, no auctions), only with ≥100 rows; nightly retrain with a 20%
  backtest. "Insufficient data" below 30 comparables (same municipality + type).
- **Rent and yield** come from rental listings collected from the same agency sites (median rent/m² per
  municipality, at least 10 rentals); otherwise no yield is shown.
- **Motivated-seller score** (0–100): days on market (max 40), price cuts (20 each, max 40), relisting (max 20).
- **Segment caps** are enforced when a search is saved (per municipality and segment home/investor);
  `DEFAULT_SEGMENT_CAP=0` means unlimited.
- **Plans** (home/investor) are stored on the user; no payment integration and no feature gating yet.
- **Telegram connect flow**: a one-time code; the user presses Start in the bot; the worker links the chat.
- **E-mail** uses the standard library `smtplib`; MailHog is in docker-compose for trying things out and tests.
- **Times** are stored as naive UTC everywhere (SQLite drops time zones); quiet hours use Europe/Brussels.
- **Feed filtering** runs in Python over the 2000 newest properties (noted in code as a shortcut; move to SQL past
  ~50k properties).
- **LLM** is optional (`LLM_PROVIDER=anthropic`, default model `claude-opus-5-5`, low effort); it only fills
  missing critical fields and polishes visit requests, never adds contact details. Without a key everything works.
- **CSRF**: session cookies are `SameSite=Lax`, which blocks cross-site form posts in current browsers; no separate
  CSRF tokens.
- **Docker image** uses an editable install so `config/` and templates are read from `/app`; `config/` is mounted
  so source lists can be edited without rebuilding.

## Skipped (named, not built)

- Map / radius picker: needs geocoding of every listing. Municipality and postal-code picker instead.
- Separate HTML parsers per template family (see Choices).
- Payment integration (out of scope for the MVP).
- Opened-at tracking for e-mail pixels: `opened_at` is set when the user clicks the alert's "Details" link.
