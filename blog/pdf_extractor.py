"""
chart_extractor.py

Finds and extracts a monthly consumption bar chart (gas or electricity)
from an invoice, whether given as a multi-page PDF (chart location unknown,
varies by page/template) or as a single image directly.

Optionally (--analyze) also generates the same profil/exposition/strategie/
recommandation write-up that the production analyze_gas_invoice endpoint
produces -- ported faithfully from views.py's _summarize_gas_data /
_validate_consumption_text / _fallback_consumption_analysis_gas /
_generate_consumption_analysis_gas, so this is a true preview of what
production will do, not a separate re-implementation. Same contract: every
number/ranking is computed in Python; the LLM only phrases already-correct
facts, and its output is validated (banned fixed-price-policy violations,
unlisted months/percentages, word-count cap) with a deterministic fallback.

Requirements:
    pip install pdf2image requests pillow
    (pdf2image also needs poppler installed on the system:
     Ubuntu/Debian: sudo apt install poppler-utils)

Usage:
    # PDF, don't know which page has the gas chart -- scan all pages, stop at first match
    python chart_extractor.py invoice.pdf --target gas

    # Same, but for electricity
    python chart_extractor.py invoice.pdf --target electricity

    # Also generate the profil/exposition/strategie/recommandation write-up
    python chart_extractor.py invoice.pdf --target gas --analyze

    # Force scanning every page even after a match is found (useful for testing/debugging)
    python chart_extractor.py invoice.pdf --target gas --no-early-stop

    # Only check specific pages
    python chart_extractor.py invoice.pdf --target gas --pages 3 4

    # Single image input -- works the same way, just one "page" to check
    python chart_extractor.py chart_screenshot.png --target gas --analyze

    # Override the image resolution / context window if needed
    python chart_extractor.py invoice.pdf --target gas --max-dimension 1600 --num-ctx 12000
"""

import argparse
import base64
import json
import re
import sys
from io import BytesIO
from pathlib import Path

import requests
from pdf2image import convert_from_path
from PIL import Image

# ---- Config -----------------------------------------------------------

API_URL = "https://gpt.caansoft.com/gpt/api/generate"
MODEL = "qwen2.5vl:7b"
REQUEST_TIMEOUT_SECONDS = 300  # CPU inference can be slow, give it room

# Defaults confirmed by direct A/B testing on a real invoice: at the old
# defaults (1200px / 8192 ctx), small bars near a much larger peak (e.g. 11
# and 33 kWh next to a 1984 kWh peak) were misread or dropped entirely --
# the aggressive downscale was shrinking already-tiny bars into near-nothing
# before the model ever saw them. At 1600px / 12000 ctx, the exact same
# invoice came back 13/13 correct, including those two smallest values.
# Higher resolution costs more context (hence the num_ctx bump too, to avoid
# re-triggering the "exceeds context size" error) and a bit more request
# time, but is the confirmed fix for this failure mode -- not a guess.
DEFAULT_MAX_DIMENSION = 1600
DEFAULT_NUM_CTX = 8192  # NOTE: only tested in combination with DEFAULT_MAX_DIMENSION=1600
                        # at 12000 -- this reverted-to-8192 value has NOT been directly
                        # confirmed sufficient at 1600px. If you see a
                        # "request exceeds the available context size" error, that's
                        # the signal this needs to go back up (12000 is the last
                        # known-working value at this resolution).

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
PDF_EXTENSIONS = {".pdf"}

TARGET_LABELS = {
    "gas": {
        "name": "gas",
        "found_key": "gas_chart_found",
        "hints": 'a flame/gas icon, or text mentioning "Gaz Naturel", "Gaz", or "PCE" near the chart',
        "distinguish_from": "an electricity consumption chart",
    },
    "electricity": {
        "name": "electricity",
        "found_key": "electricity_chart_found",
        "hints": 'a plug/electricity icon, or text mentioning "Électricité", "Electricite", or "PDL" near the chart',
        "distinguish_from": "a gas consumption chart",
    },
}


def build_prompt(target):
    t = TARGET_LABELS[target]
    return f"""First, check: does this image contain a monthly {t['name']} consumption history (in kWh)? This can be shown in EITHER of two forms -- treat both as a match:
  (a) A bar chart, with a number printed above each bar.
  (b) A table or plain row of numbers under a row of month labels (no bars at all) -- e.g. a "Historique de la consommation" section listing one value per month in a line, sometimes with small colored markers indicating whether each value was estimated, an actual meter reading, or self-reported.

This is distinct from {t['distinguish_from']} -- look for indicators like {t['hints']}.

If NEITHER form is present anywhere in this image (either no monthly consumption history at all, or only one for a different energy type), respond with exactly this and nothing else:
{{"{t['found_key']}": false}}

If EITHER form IS present, extract the monthly consumption values from it. For each month shown, return the number associated with it -- whether that number sits above a bar or simply appears in a row/table under the month's label. If a specific month has no visible bar, no printed number, or no value in the row/table, use null for that month only.

Read the numbers one at a time, in the order the months appear. For each one, look closely at every digit before answering.

Return ONLY valid JSON, no markdown formatting, no explanation, in this format:
{{"{t['found_key']}": true, "values": {{"Month Year": value_or_null, ...}}}}
using the exact month labels shown, in the same order they appear (left to right, or top to bottom if stacked)."""


# ---- Input loading (PDF or image) -----------------------------------------------------

def detect_input_type(path):
    ext = Path(path).suffix.lower()
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    # fall back to checking the actual file bytes, in case the extension is missing/wrong
    with open(path, "rb") as f:
        header = f.read(5)
    if header.startswith(b"%PDF-"):
        return "pdf"
    try:
        Image.open(path).verify()
        return "image"
    except Exception:
        raise ValueError(f"Could not determine file type for {path} (not a recognizable PDF or image)")


def load_pages(path, dpi=200, pages=None):
    """
    Returns a list of (page_number, PIL.Image) tuples, regardless of whether
    the input was a PDF (one entry per page) or a plain image (a single entry).
    """
    input_type = detect_input_type(path)

    if input_type == "image":
        img = Image.open(path).convert("RGB")
        return [(1, img)]

    # PDF
    kwargs = {"dpi": dpi}
    if pages:
        kwargs["first_page"] = min(pages)
        kwargs["last_page"] = max(pages)

    images = convert_from_path(path, **kwargs)

    if pages:
        offset = min(pages)
        images = [(offset + i, img) for i, img in enumerate(images) if (offset + i) in pages]
    else:
        images = [(i + 1, img) for i, img in enumerate(images)]

    return images


def resize_image_if_needed(pil_image, max_dimension=DEFAULT_MAX_DIMENSION):
    """
    Downscale an image so its longest side is at most max_dimension.
    Full PDF pages rendered at 200+ DPI can be huge (1600-2400px), which
    costs far more vision tokens than a cropped screenshot would -- this
    keeps requests within the model's context window without needing to
    lower the PDF render DPI (which would hurt fine-text readability).

    max_dimension=1600 is the confirmed floor for reading small bars
    accurately (see DEFAULT_MAX_DIMENSION note above) -- going lower
    reintroduces the small-bar misread/drop failure mode.
    """
    w, h = pil_image.size
    longest = max(w, h)
    if longest <= max_dimension:
        return pil_image
    scale = max_dimension / longest
    new_size = (int(w * scale), int(h * scale))
    return pil_image.resize(new_size, Image.LANCZOS)


def image_to_base64(pil_image):
    buffer = BytesIO()
    pil_image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


# ---- Model call & parsing -----------------------------------------------------

def query_vision_model(image_b64, prompt, model=MODEL, num_ctx=DEFAULT_NUM_CTX):
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"num_ctx": num_ctx},
    }
    response = requests.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    if not response.ok:
        raise requests.exceptions.HTTPError(
            f"{response.status_code} error from server. Response body:\n{response.text}"
        )
    data = response.json()
    return data.get("response", "")


def extract_json_from_response(raw_text):
    """
    The model sometimes wraps output in ```json fences or adds stray text.
    Pull out the last {...} block found, which is usually the actual answer.
    """
    text = raw_text.strip()
    text = text.replace("```json", "").replace("```", "")
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None, text
    candidate = text[start:end + 1]
    try:
        return json.loads(candidate), text
    except json.JSONDecodeError:
        return None, text


# ─────────────────────────────────────────────────────────────────────────────
# Gas consumption analysis (profil / exposition / stratégie / recommandation)
#
# Ported faithfully from views.py's _summarize_gas_data / _validate_consumption_text /
# _fallback_consumption_analysis_gas / _generate_consumption_analysis_gas, so this
# is a true preview of production behavior, not a separate re-implementation.
# Same contract: every fact (peak/low months, ratios, winter/summer split) is
# computed in Python; the LLM's only job is to phrase already-correct facts, and
# its output is validated (banned fixed-price-policy violations, unlisted
# months/percentages, word-count cap) with a deterministic fallback on any drift.
# ─────────────────────────────────────────────────────────────────────────────

_GAS_WINTER_MONTHS = {11, 12, 1, 2, 3}  # Nov -> Mar (heating season); rest counts as summer

_FRENCH_MONTHS = {
    "janvier": "01", "février": "02", "fevrier": "02", "mars": "03",
    "avril": "04", "mai": "05", "juin": "06", "juillet": "07",
    "août": "08", "aout": "08", "septembre": "09", "octobre": "10",
    "novembre": "11", "décembre": "12", "decembre": "12",
}

# Terms that contradict Volt's fixed-price-only sales policy, or describe
# products/offers not present in the summarized data. Any hit -> reject the
# LLM's output and use the deterministic fallback instead.
_BANNED_STRATEGY_TERMS = [
    "taux variable", "prix variable", "tarif variable",
    "indexé", "indexée", "indexés", "indexées",
    "spot", "prépayé", "prépayés", "prépayée", "prépayées",
    "bloc d'achat", "blocs d'achat", "blocs d'achats",
]


_MONTH_PREFIXES = {
    1: ["jan"],
    2: ["fev", "fév", "feb"],
    3: ["mar"],
    4: ["avr", "apr"],
    5: ["mai", "may"],
    6: ["jun", "juin"],
    7: ["jul", "juil"],
    8: ["aou", "aoû", "aug"],
    9: ["sep"],
    10: ["oct"],
    11: ["nov"],
    12: ["dec", "déc"],
}


def fr_month_to_num(label):
    """Month label -> 1..12. Handles BOTH French abbreviations ('Fev', 'Avr',
    'Mai', 'Juil', 'Aou'/'Août') and English ones ('Feb', 'Apr', 'May', 'Jul',
    'Aug') -- the invoice/chart's rendered language depends on the app's UI
    language at the moment it was generated, so both show up in practice; the
    original French-only version silently dropped every English-labeled month
    (Feb/Apr/May/Jul/Aug) from the analysis entirely. Returns None if no month
    can be identified."""
    if not label:
        return None
    m = re.match(r"[a-zàâäéèêëîïôöûüç]+", str(label).strip().lower())
    if not m:
        return None
    w = m.group(0)
    for month_num, prefixes in _MONTH_PREFIXES.items():
        if any(w.startswith(p) for p in prefixes):
            return month_num
    return None


def norm_month_mmyyyy(label):
    """Normalize an invoice month label ('Nov 25', 'janvier 2026') to 'MM/YYYY'
    so peak/low months match what validate_consumption_text expects. Returns
    the original label unchanged if the month or year can't be parsed."""
    mnum = fr_month_to_num(label)
    ym = re.search(r"(\d{2,4})", str(label or ""))
    if mnum is None or not ym:
        return label
    year = ym.group(1)
    if len(year) == 2:
        year = "20" + year
    return f"{mnum:02d}/{year}"


def values_dict_to_pairs(values):
    """Convert the extractor's {"Month Year": value_or_null, ...} output into
    the [{"month","kwh"}, ...] shape the analysis pipeline expects. Keeps
    null entries (kwh=None) rather than dropping them: summarize_gas_data
    needs the full chronological sequence, gaps included, to correctly
    determine the true most-recent-12-months window -- dropping nulls here
    would let a missing month silently get replaced by an older month
    outside the real window (exactly what happened before this fix)."""
    return [{"month": k, "kwh": v} for k, v in values.items()]


def summarize_gas_data(pairs):
    """All facts (peak/low months, peak-to-average ratio, winter/summer split)
    computed HERE in Python -- the LLM never ranks, sorts, or calculates a
    percentage itself.

    The most-recent-12-months window is decided by POSITION in the chart's
    left-to-right chronological order FIRST, including any month with no
    printed value -- only after the window is fixed do we drop the null/
    unparseable entries from the actual sum. This is what prevents a missing
    bar from silently being replaced by an older month outside the true
    12-month window (taking "the last 12 non-null values" instead would let
    exactly that happen -- confirmed in testing: a null Jan value caused an
    older Feb from the prior year to be pulled in instead).

    Returns a summary dict, or None if there's not enough usable data.
    """
    if not pairs:
        return None

    window = list(pairs)[-12:]

    parsed = []
    missing_months = []
    for item in window:
        if not isinstance(item, dict):
            continue
        label = item.get("month")
        mnum = fr_month_to_num(label)
        kwh = item.get("kwh")
        if mnum is None or not isinstance(kwh, (int, float)):
            missing_months.append(label)
            continue  # no bar / unparseable month -- excluded from the sum, window stays fixed
        parsed.append((label, mnum, kwh))

    if not parsed:
        return None

    total = round(sum(k for _, _, k in parsed), 1)
    if total <= 0:
        return None
    avg = round(total / len(parsed), 1)

    ranked = sorted(parsed, key=lambda t: t[2], reverse=True)
    peak_months = [{"month": norm_month_mmyyyy(l), "value": k} for l, _, k in ranked[:3]]
    lowest_months = [{"month": norm_month_mmyyyy(l), "value": k} for l, _, k in ranked[-3:]][::-1]
    peak_to_average_ratio = round(ranked[0][2] / avg, 2) if avg else None

    winter = round(sum(k for _, m, k in parsed if m in _GAS_WINTER_MONTHS), 1)
    summer = round(total - winter, 1)
    winter_pct = round(winter / total * 100)
    season_split = {
        "winter_total": winter,
        "summer_total": summer,
        "winter_share_pct": winter_pct,
        "summer_share_pct": 100 - winter_pct,
        "dominant_season": "hiver" if winter >= summer else "été",
    }

    window_labels = [item.get("month") for item in window if isinstance(item, dict)]

    return {
        "period": {
            "from": norm_month_mmyyyy(window_labels[0]) if window_labels else None,
            "to": norm_month_mmyyyy(window_labels[-1]) if window_labels else None,
        },
        "total_annual_kwh": total,
        "avg_monthly_kwh": avg,
        "peak_months": peak_months,
        "lowest_months": lowest_months,
        "peak_to_average_ratio": peak_to_average_ratio,
        "season_split": season_split,
        "months_in_window": len(window),
        "months_with_data": len(parsed),
        "missing_months": missing_months,
    }


def extract_mentioned_months(text):
    """Find every month reference in the text, numeric ('12/2025') or French
    prose ('décembre 2025'), normalized to 'MM/YYYY'."""
    found = set()
    found.update(re.findall(r"\b(?:0[1-9]|1[0-2])/20\d{2}\b", text))
    for name, mm in _FRENCH_MONTHS.items():
        for match in re.finditer(rf"\b{name}\b\s+(\d{{4}})", text, flags=re.IGNORECASE):
            found.add(f"{mm}/{match.group(1)}")
    return found


def validate_consumption_text(text, summary):
    """Reject any generated field that cites a month/percentage not present in
    the summary, or that uses a banned fixed-price-policy-violating term."""
    if not text:
        return False

    allowed_months = {
        m["month"] for m in summary.get("peak_months", []) + summary.get("lowest_months", [])
    }
    mentioned_months = extract_mentioned_months(text)
    if mentioned_months - allowed_months:
        print(f"Rejected: unlisted month(s) {mentioned_months - allowed_months}")
        return False

    allowed_pcts = set()
    if summary.get("season_split"):
        allowed_pcts.add(summary["season_split"]["winter_share_pct"])
        allowed_pcts.add(summary["season_split"]["summer_share_pct"])

    mentioned_pcts_raw = re.findall(r"(\d+(?:[.,]\d+)?)\s?%", text)
    for raw in mentioned_pcts_raw:
        val = float(raw.replace(",", "."))
        if not any(abs(val - allowed) < 0.15 for allowed in allowed_pcts):
            print(f"Rejected: unlisted percentage {val}")
            return False

    lowered = text.lower()
    hit_terms = [term for term in _BANNED_STRATEGY_TERMS if term in lowered]
    if hit_terms:
        print(f"Rejected: banned strategy term(s) found: {hit_terms}")
        return False

    return True


def fallback_consumption_analysis_gas(summary):
    """Plain templated output, used only if the LLM output fails validation.
    Guaranteed numerically correct since it's built directly from summary."""
    peak = summary["peak_months"][0]
    low = summary["lowest_months"][0]
    season = summary.get("season_split")

    profil = (
        f"Consommation maximale en {peak['month']} ({peak['value']} kWh), "
        f"minimale en {low['month']} ({low['value']} kWh)"
    )
    if season:
        profil += (
            f", avec {season['winter_share_pct']}% de la consommation concentrée "
            "en hiver (novembre à mars)"
        )
    profil += "."

    return {
        "profil": profil,
        "exposition": (
            f"Avec un pic à {summary['peak_to_average_ratio']}x la consommation moyenne "
            "concentré en hiver, votre budget gaz est fortement exposé aux variations "
            "de prix pendant la saison de chauffe."
        ),
        "strategie": (
            "Un contrat à prix fixe sécurise votre budget gaz sur toute la période "
            "hivernale et vous protège des hausses au moment où vous consommez le plus."
        ),
        "recommandation": (
            "Dans ce contexte, sécuriser dès maintenant votre contrat à prix fixe "
            "protège votre budget contre les hausses, en particulier sur votre forte "
            "consommation hivernale."
        ),
    }


def parse_llm_fields(text, field_names):
    """Parse a 'LABEL: text' per-line response into {field_name: text}."""
    result = {}
    if not text:
        return result
    for line in text.splitlines():
        line = line.strip()
        for field in field_names:
            prefix = f"{field}:"
            if line.upper().startswith(prefix.upper()):
                result[field.lower()] = line.split(":", 1)[1].strip()
                break
    return result


def call_text_llm(prompt, model=MODEL, num_ctx=8192, timeout=REQUEST_TIMEOUT_SECONDS):
    """Text-only counterpart of query_vision_model -- same endpoint, no image
    attached. Returns the raw response text, or None on any failure.

    Note: this stays at a smaller default num_ctx than the vision path --
    text-only analysis prompts are far smaller than a rendered page image, so
    the larger DEFAULT_NUM_CTX used for vision isn't needed here."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": num_ctx},
    }
    try:
        response = requests.post(API_URL, json=payload, timeout=timeout)
        if not response.ok:
            print(f"Text LLM call failed: {response.status_code} {response.text}")
            return None
        return response.json().get("response") or None
    except requests.exceptions.RequestException as e:
        print(f"Text LLM call failed: {e}")
        return None


def generate_consumption_analysis_gas(pairs, model=MODEL):
    """All facts computed in summarize_gas_data; the LLM only phrases them.
    Validated against the summary; falls back to a plain template on mismatch.
    Returns {profil, exposition, strategie, recommandation}, or None if
    there's no usable data at all."""
    summary = summarize_gas_data(pairs)
    if not summary:
        return None, None

    prompt = (
        "Tu es un rédacteur pour Volt Consulting. Voici un JSON contenant DÉJÀ TOUS "
        "les calculs sur la consommation de GAZ d'un client — ne recalcule rien, ne "
        "classe rien, n'invente aucun chiffre ni mois : reformule ces faits en "
        "français fluide.\n\n"
        f"{json.dumps(summary, ensure_ascii=False)}\n\n"
        "Champs à utiliser :\n"
        "- peak_months[0] et lowest_months[0] : mois de plus forte/faible consommation.\n"
        "- season_split : dominant_season et les deux share_pct (part hiver "
        "novembre→mars / part été avril→octobre).\n"
        "- peak_to_average_ratio : pour juger de l'exposition au marché.\n\n"
        "Rédige quatre phrases courtes (30 mots maximum chacune — respecte STRICTEMENT "
        "cette limite, une seule phrase par champ) pour un client professionnel, sur "
        "sa consommation de GAZ :\n"
        "1) PROFIL: le profil de consommation (saisonnalité), en t'appuyant sur "
        "peak_months[0], lowest_months[0] et season_split.\n"
        "2) EXPOSITION: l'exposition du budget gaz aux fluctuations de prix, surtout "
        "en hiver, en t'appuyant sur peak_to_average_ratio et la part hivernale.\n"
        "3) STRATEGIE: recommande TOUJOURS un contrat à PRIX FIXE — jamais un taux "
        "variable, un tarif indexé, une offre spot, ou des blocs d'achat prépayés, et "
        "jamais un produit non mentionné dans les données. Explique en une phrase "
        "pourquoi la stabilité du prix fixe protège ce profil (forte consommation "
        "hivernale).\n"
        "4) RECOMMANDATION: une recommandation d'action concrète et incitative — "
        "sécuriser dès maintenant le contrat à PRIX FIXE pour protéger le budget, "
        "compte tenu de l'exposition hivernale (toujours prix fixe, jamais variable).\n"
        "Réponds STRICTEMENT selon ce format, sans aucun autre texte :\n"
        "PROFIL: <texte>\n"
        "EXPOSITION: <texte>\n"
        "STRATEGIE: <texte>\n"
        "RECOMMANDATION: <texte>"
    )

    text = call_text_llm(prompt, model=model)
    fields = parse_llm_fields(text, ["PROFIL", "EXPOSITION", "STRATEGIE", "RECOMMANDATION"])

    if not fields:
        print("LLM returned no parseable fields -- using deterministic fallback.")
        return fallback_consumption_analysis_gas(summary), summary

    if any(len(v.split()) > 40 for v in fields.values()):
        print("Rejected: a field exceeded the 40-word safety cap -- using fallback.")
        return fallback_consumption_analysis_gas(summary), summary

    if not validate_consumption_text(" ".join(fields.values()), summary):
        print("Using deterministic fallback due to validation failure above.")
        return fallback_consumption_analysis_gas(summary), summary

    return {
        "profil": fields.get("profil", ""),
        "exposition": fields.get("exposition", ""),
        "strategie": fields.get("strategie", ""),
        "recommandation": fields.get("recommandation", ""),
    }, summary


# ---- Main processing -----------------------------------------------------

def find_chart(input_path, target="gas", dpi=200, pages=None, model=MODEL,
                num_ctx=DEFAULT_NUM_CTX, max_dimension=DEFAULT_MAX_DIMENSION, early_stop=True):
    t = TARGET_LABELS[target]
    prompt = build_prompt(target)

    print(f"Loading pages from {input_path}...")
    page_images = load_pages(input_path, dpi=dpi, pages=pages)
    print(f"Loaded {len(page_images)} page(s)/image(s). Looking for a {t['name']} chart...\n")

    results = []
    for page_num, img in page_images:
        print(f"--- Page {page_num} ---")
        img = resize_image_if_needed(img, max_dimension=max_dimension)
        img_b64 = image_to_base64(img)

        try:
            raw_response = query_vision_model(img_b64, prompt=prompt, model=model, num_ctx=num_ctx)
        except requests.exceptions.RequestException as e:
            print(f"  Request failed: {e}")
            results.append({"page": page_num, "error": str(e)})
            continue

        parsed, cleaned_text = extract_json_from_response(raw_response)
        found_key = t["found_key"]

        if parsed is not None and parsed.get(found_key) is True:
            print(f"  {t['name'].capitalize()} chart FOUND. Values: {json.dumps(parsed.get('values'), ensure_ascii=False)}")
            results.append({"page": page_num, "found": True, "data": parsed.get("values", {})})
            if early_stop:
                print(f"\nStopping early -- {t['name']} chart found on page {page_num}. "
                      f"(use --no-early-stop to scan remaining pages anyway)")
                break
        elif parsed is not None and parsed.get(found_key) is False:
            print(f"  No {t['name']} chart on this page.")
            results.append({"page": page_num, "found": False})
        else:
            print(f"  Could not parse JSON. Raw model output:\n  {cleaned_text}")
            results.append({"page": page_num, "raw_output": cleaned_text})
        print()

    return results


def print_summary(results, target):
    t = TARGET_LABELS[target]
    print("=" * 50)
    print("SUMMARY")
    print("=" * 50)

    found_pages = [r for r in results if r.get("found") is True]
    not_found_pages = [r for r in results if r.get("found") is False]
    error_pages = [r for r in results if "error" in r or "raw_output" in r]

    if found_pages:
        print(f"\n{t['name'].capitalize()} chart found on page(s): {', '.join(str(r['page']) for r in found_pages)}")
        for r in found_pages:
            print(f"  Page {r['page']}: {json.dumps(r['data'], ensure_ascii=False)}")
    else:
        print(f"\nNo {t['name']} chart found in the page(s) checked.")

    if not_found_pages:
        print(f"\nPages checked with no {t['name']} chart: {', '.join(str(r['page']) for r in not_found_pages)}")

    if error_pages:
        print(f"\nPages that failed or returned unparseable output: {', '.join(str(r['page']) for r in error_pages)}")

    print()


def build_gas_response(pairs, model=MODEL):
    """Build a response matching the SAME shape as production's
    analyze_gas_invoice endpoint: winterPct/summerPct/winterKwh/summerKwh as
    flat top-level keys (not nested under season_split), plus the monthly
    series and the generated analysis text. Returns None if there isn't
    enough usable data."""
    summary = summarize_gas_data(pairs)
    if not summary:
        return None

    analysis, _ = generate_consumption_analysis_gas(pairs, model=model)
    season = summary.get("season_split") or {}

    # Same window summarize_gas_data used (last 12 by chart position,
    # nulls included) -- NOT re-sliced independently, so this can't drift
    # out of sync with the window the stats above were actually computed on.
    window = list(pairs)[-12:]
    months_in_window = [p.get("month") for p in window if isinstance(p, dict)]
    values_in_window = [p.get("kwh") for p in window if isinstance(p, dict)]

    return {
        "status": "success",
        "winterPct": season.get("winter_share_pct"),
        "summerPct": season.get("summer_share_pct"),
        "winterKwh": season.get("winter_total"),
        "summerKwh": season.get("summer_total"),
        "totalKwh": summary.get("total_annual_kwh"),
        "avgMonthlyKwh": summary.get("avg_monthly_kwh"),
        "peakToAverageRatio": summary.get("peak_to_average_ratio"),
        "peakMonths": summary.get("peak_months"),
        "lowestMonths": summary.get("lowest_months"),
        "monthsInWindow": summary.get("months_in_window"),
        "monthsWithData": summary.get("months_with_data"),
        "missingMonths": summary.get("missing_months"),
        "monthly": {
            "months": months_in_window,
            "values": values_in_window,
        },
        "consumptionAnalysis": analysis or {},
    }


def print_analysis(pairs, model=MODEL):
    """Run the gas consumption analysis pipeline on extracted pairs and print
    a readable result: the computed facts (summary), the final text fields,
    and whether the LLM's output was used or the fallback kicked in."""
    print("=" * 50)
    print("CONSUMPTION ANALYSIS")
    print("=" * 50)

    if not pairs:
        print("\nNo values to analyze (no chart found, or every month was null).")
        return None

    response = build_gas_response(pairs, model=model)
    if response is None:
        print("\nNot enough usable data to compute an analysis.")
        return None

    print(f"\nComputed facts (Python, not the LLM):")
    print(f"  Months in window: {response['monthsInWindow']} (of which {response['monthsWithData']} had data)")
    if response.get("missingMonths"):
        print(f"  Missing/no-data months in window: {response['missingMonths']}")
    print(f"  Total: {response['totalKwh']} kWh, avg/month: {response['avgMonthlyKwh']} kWh")
    print(f"  Peak month: {response['peakMonths'][0]}")
    print(f"  Lowest month: {response['lowestMonths'][0]}")
    print(f"  Peak-to-average ratio: {response['peakToAverageRatio']}x")
    print(f"  winterPct: {response['winterPct']}")
    print(f"  summerPct: {response['summerPct']}")
    print(f"  winterKwh: {response['winterKwh']}")
    print(f"  summerKwh: {response['summerKwh']}")

    print(f"\nGenerated text:")
    for field in ("profil", "exposition", "strategie", "recommandation"):
        print(f"  {field.upper()}: {response['consumptionAnalysis'].get(field, '')}")
    print()

    return response


def main():
    parser = argparse.ArgumentParser(
        description="Find and extract a gas or electricity consumption bar chart from a PDF or image."
    )
    parser.add_argument("input_path", help="Path to a PDF or image file")
    parser.add_argument("--target", choices=["gas", "electricity"], default="gas",
                         help="Which chart type to look for (default: gas)")
    parser.add_argument("--pages", nargs="+", type=int, default=None,
                         help="Specific 1-indexed PDF page numbers to check (default: all pages). Ignored for image input.")
    parser.add_argument("--dpi", type=int, default=200, help="PDF rendering resolution (default: 200)")
    parser.add_argument("--max-dimension", type=int, default=DEFAULT_MAX_DIMENSION,
                         help=f"Downscale images so the longest side is at most this many pixels "
                              f"(default: {DEFAULT_MAX_DIMENSION} -- confirmed by testing to correctly "
                              f"read small bars that were misread/dropped at lower resolutions)")
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX,
                         help=f"Ollama context window size for the vision call (default: {DEFAULT_NUM_CTX})")
    parser.add_argument("--model", default=MODEL, help=f"Ollama model name (default: {MODEL})")
    parser.add_argument("--no-early-stop", action="store_true",
                         help="Check every page even after the target chart is found (default: stop at first match)")
    parser.add_argument("--analyze", action="store_true",
                         help="Also generate the profil/exposition/strategie/recommandation "
                              "write-up from the extracted values (gas only)")
    parser.add_argument("--text-model", default=None,
                         help="Model to use for the --analyze text-generation step "
                              "(default: same as --model)")
    parser.add_argument("--out", default=None, help="Optional path to save results as JSON")
    args = parser.parse_args()

    if not Path(args.input_path).exists():
        print(f"File not found: {args.input_path}")
        sys.exit(1)

    results = find_chart(
        args.input_path,
        target=args.target,
        dpi=args.dpi,
        pages=args.pages,
        model=args.model,
        num_ctx=args.num_ctx,
        max_dimension=args.max_dimension,
        early_stop=not args.no_early_stop,
    )

    print_summary(results, args.target)

    gas_response = None
    if args.analyze:
        if args.target != "gas":
            print("Note: --analyze currently only implements the gas write-up "
                  "(profil/exposition/strategie/recommandation). Skipping.")
        else:
            found = next((r for r in results if r.get("found") is True), None)
            values = found["data"] if found else {}
            pairs = values_dict_to_pairs(values)
            gas_response = print_analysis(pairs, model=args.text_model or args.model)

    if args.out:
        # Save the combined structure (chart pages checked + the flat
        # winterPct/summerPct analysis, if --analyze was used) rather than
        # just the raw per-page chart results, so the saved file mirrors
        # what analyze_gas_invoice actually returns.
        output = {"pages": results}
        if gas_response is not None:
            output.update(gas_response)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"Results saved to {args.out}")


if __name__ == "__main__":
    main()