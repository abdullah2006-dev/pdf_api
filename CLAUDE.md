# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Django service that turns a client's energy-consumption/pricing payload into a branded **Volt Consulting** proposal — either a print PDF (legacy) or an interactive HTML "slide deck" (current). It is a **stateless generation API**: `blog/models.py` is entirely commented out and the only migrations delete the one former model, so `db.sqlite3` is effectively unused. Almost all logic lives in one large module, **`blog/views.py`** (~3k lines) of function-based views.

## Commands

Requires a virtualenv (`venv/`) and, for PDF output, native GTK/Pango libraries (WeasyPrint) — see `README.md` for OS-specific install.

```bash
# activate venv first (Windows: venv\Scripts\activate | Linux: source venv/bin/activate)
pip install -r requirements.txt
python manage.py migrate            # runs, but the app stores nothing
python manage.py runserver          # http://127.0.0.1:8000/  (port 8000 = the "pdf-service")
```

There is **no test suite** (`blog/tests.py` is an empty stub) and **no linter/formatter config**. `python manage.py test` works but exercises nothing.

## Request → deck data flow (the core pattern)

Every generation endpoint follows the same shape (`energy_offer_summary` / `comparatif_gas` are the current ones):

1. Parse JSON body → the important object is `comparatifClientHistoryPdfDto` (rates, client, `energyType` = `"ELECTRICITY"` or `"GAS"`), plus `chartDataDto`, client fields, and a `sales` object.
2. `generate_*_chart(...)` renders matplotlib figures **as base64 data-URIs** (matplotlib uses the `Agg` backend) that get embedded directly in the HTML — there are no separate image files for charts.
3. `build_comparatif_dto_*` validates/normalizes the payload (GAS vs ELECTRICITY have mutually-exclusive required fields and forbidden fields).
4. `build_presentation_data_*` assembles the full context dict, including per-slide sub-dicts (`slide6`, `gas_info`, `sales`, `market_analysis`, `consumption_analysis`, …).
5. `render_to_string(template, {"data": presentation_data})`.
6. For decks: `save_html_file(...)` writes the rendered HTML to disk and returns a public URL. For legacy PDFs: `generate_pdf*(...)` uses WeasyPrint then PyPDF2 to strip pages.

Endpoints (`blog/urls.py`, mounted at the project root in `api/urls.py`):

| Path | View | Output |
|------|------|--------|
| `api/comparatif-electricity/` | `energy_offer_summary` | HTML deck (`volt-electricity.html`) |
| `api/comparatif-gas/` | `comparatif_gas` | HTML deck (`volt-gas.html`) |
| `api/generate-market-analysis/` | `generate_market_analysis` | LLM JSON (slide 3 text) |
| `api/generate-consumption-analysis/` | `generate_consumption_analysis` | LLM JSON (slide 4 text) |
| `editor/save-file/` | `save_file_edit` | writes edits back into a saved deck |
| `api/volt.html-consulting-presentation*/` | `volt_consulting_presentation*` | **legacy** PDF (`volt.html` / `volt_Electricity.html`) |

All views are `@csrf_exempt @require_http_methods(["POST"])`. DRF is installed with `AllowAny`.

## Two template generations (don't confuse them)

- **Legacy PDF deck**: `volt.html`, `volt_Electricity.html` → WeasyPrint (`@page { size: 530mm 265mm }`, zoom) → PyPDF2 removes interleaved blank pages. Largely frozen.
- **Current interactive deck**: `templates/volt-electricity.html` and `templates/volt-gas.html`. These are the actively-edited files. They are **8-slide HTML decks** built on custom web components (`<deck-stage>`, `<image-slot>`). `templates/Volt-comparitif/` holds the standalone source of those components (`deck-stage.js`, `image-slot.js`, `styles.css`) — but **each deck template carries its own inlined copy of that CSS + JS**, so a change to shared styling/behavior must be made in *both* `volt-electricity.html` and `volt-gas.html` (they are parallel: navy/green vs orange `--accent` themes).

### Deck layout facts that repeatedly matter
- Slides are a **fixed `--slide-height: 794px`** with `overflow: hidden`; content that exceeds it is clipped and can slide under the footer. `.footer-bar` is often `position: absolute; bottom: 0`, so to create space above it you must *shrink the content above*, not move the footer.
- Styling is heavily **scoped per slide** via `section[data-slide="hero|about|market|consumption|comparatif|resultat-offre"]` rules, frequently with `!important` that **overrides inline styles** — when an inline size "does nothing," look for the scoped rule.
- `<image-slot>` renders at a fixed width in a shadow DOM and defaults to `fit="cover"` (crops); use `fit="contain"` and note that changing only `height` may not visibly resize a wide logo.

## LLM integration (slides 3 & 4)

Market and consumption analyses call an in-house **Ollama-compatible** endpoint: `POST https://gpt.caansoft.com/gpt/api/generate`, model `gpt-oss:20b` (`_call_market_llm`, 280s timeout, `stream:false`). Raw payloads are large, so `_summarize_chart_data` / `_summarize_enedis_data` compress the input before prompting; `_generate_analyses_parallel` runs both via a `ThreadPoolExecutor`. There are validators/fallbacks (`_validate_consumption_text`, `_fallback_consumption_analysis`) because the model can hallucinate months/figures.

## Host- and proxy-aware behavior (critical for deploys)

- `save_html_file` / `generate_pdf` choose the output root by `request.get_host()`:
  - `volt-crm.caansoft.com` → `STAGING_MEDIA_ROOT`, `crm.volt-consulting.com` → `PRODUCTION_MEDIA_ROOT` (both under `BASE_UPLOAD_DIR = /home/file_upload/fileuploadutility/uploads/volt`), else local `MEDIA_ROOT`.
- On the servers the Django app runs on port 8000 **behind the nginx `/pdf-service/` location** (prefix stripped), and nginx only proxies `/api/`-style paths — a bare path served by the SPA catch-all returns 405. Generated decks therefore embed `window.__VOLT_API_BASE__` (`/pdf-service` on the CRM hosts, empty locally) and `window.__VOLT_EDIT_TARGET__` (the exact file path) so the inline editor's save call reaches Django in every environment.
- **Saved decks are snapshots.** Editing a `templates/*.html` file only affects *newly generated* decks; already-generated client files under the upload roots keep the old markup until regenerated.

## Inline editor (`save_file_edit`)

Slides 3 & 4 have `contenteditable` fields wrapped in `<!-- EDIT:start:{key} -->` / `<!-- EDIT:end:{key} -->` markers with a matching `data-edit-key`. The floating save button POSTs `{path, key, html}`; the view replaces the region between those markers, sanitizes with **bleach**, and re-attaches the editing attributes (`contenteditable` / `data-edit-key` / `spellcheck`) that bleach would otherwise strip (which previously "locked" fields after one save). Allowed write roots are the templates dir plus the media/upload roots. In production (`DEBUG=False`) it requires an authenticated staff user; it stays open when `DEBUG=True`.
