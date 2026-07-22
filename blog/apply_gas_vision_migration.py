"""
apply_gas_vision_migration.py

Applies the gas-chart vision-extraction integration DIRECTLY to your real
views.py file, instead of you hand-copying changes into a 2700+ line file
(risky) or me retyping the whole file from memory (riskier still -- any
transcription slip could silently corrupt code elsewhere in the file that
has nothing to do with this change).

This script:
  1. Reads your actual views.py
  2. Deletes 3 functions that are no longer used
  3. Replaces 4 functions with fixed versions (same names, same call sites,
     so nothing else in the file needs to change)
  4. Inserts the new vision-extraction functions
  5. Fixes one string check inside analyze_gas_invoice
  6. Writes the result to views_MIGRATED.py (does NOT overwrite your
     original -- review the diff yourself before replacing it)

Usage:
    python3 apply_gas_vision_migration.py /path/to/your/views.py

    Then review:
    diff /path/to/your/views.py views_MIGRATED.py

    If it looks right:
    cp views_MIGRATED.py /path/to/your/views.py
"""

import re
import sys
from pathlib import Path


def extract_function_block(source, func_name):
    """Return (start_index, end_index, block_text) for a top-level
    `def func_name(...):` definition -- from its `def` line up to (but not
    including) the next top-level `def ` or `@` at column 0, or end of file.
    Raises ValueError if the function isn't found."""
    pattern = re.compile(rf"^def {re.escape(func_name)}\(", re.MULTILINE)
    m = pattern.search(source)
    if not m:
        raise ValueError(f"Could not find function '{func_name}' in the source file")

    start = m.start()
    # Find the next top-level def/@ after this point (not indented)
    next_pattern = re.compile(r"^(def |@)", re.MULTILINE)
    next_match = next_pattern.search(source, m.end())
    end = next_match.start() if next_match else len(source)

    return start, end, source[start:end]


def delete_function(source, func_name):
    start, end, _ = extract_function_block(source, func_name)
    print(f"  Deleting {func_name} ({end - start} chars)")
    return source[:start] + source[end:]


def replace_function(source, func_name, new_code):
    start, end, old_block = extract_function_block(source, func_name)
    print(f"  Replacing {func_name} ({len(old_block)} chars -> {len(new_code)} chars)")
    # ensure exactly one blank line of separation after the replacement, matching
    # the spacing convention the rest of the file already uses between functions
    new_code = new_code.rstrip() + "\n\n\n"
    return source[:start] + new_code + source[end:]


def insert_before_function(source, func_name, new_code):
    start, _, _ = extract_function_block(source, func_name)
    print(f"  Inserting new functions before {func_name}")
    new_code = new_code.rstrip() + "\n\n\n"
    return source[:start] + new_code + source[start:]


# ── Replacement function bodies ──────────────────────────────────────────

FIXED_FR_MONTH_TO_NUM = '''def _fr_month_to_num(label):
    """Month label -> 1..12. FIXED: now handles BOTH French abbreviations
    ('Fev', 'Avr', 'Mai', 'Juil', 'Aou'/'Août') and English ones ('Feb',
    'Apr', 'May', 'Jul', 'Aug') -- the invoice/chart's rendered language
    depends on the app's UI language at the moment it was generated, so
    both show up in practice. The previous French-only version silently
    dropped every English-labeled month (Feb/Apr/May/Jul/Aug) from the
    whole analysis -- confirmed directly in testing: it corrupted the
    total, the peak/low months, and the winter/summer split without any
    error or warning. Returns None if no month can be identified."""
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
'''

FIXED_COMPUTE_GAS_SEASON_SPLIT = '''def _compute_gas_season_split(pairs):
    """From [{"month","kwh"}, ...] compute the winter (Nov-Mar) vs summer
    (Apr-Oct) share.

    FIXED: the most-recent-12-months window is now decided by POSITION in
    the chart's chronological order FIRST -- including any month with a
    null/missing value -- and only after the window is fixed do we drop
    the null entries from the actual sum. The previous version took "the
    last 12 non-null values," which let a missing month silently get
    replaced by an OLDER month outside the true 12-month window (confirmed
    directly in testing: a null January value caused the previous year's
    February to be pulled into the window instead, corrupting the total
    and the seasonal split without any visible error).

    Returns a result dict, or None if there is not enough usable data.
    """
    if not isinstance(pairs, list) or not pairs:
        return None

    window = list(pairs)[-12:]

    parsed = []
    for item in window:
        if not isinstance(item, dict):
            continue
        mnum = _fr_month_to_num(item.get("month"))
        kwh = _to_float(item.get("kwh"))
        if mnum is None or kwh is None:
            continue  # no bar / unparseable month -- excluded from the sum, window stays fixed
        parsed.append((item.get("month"), mnum, kwh))

    if not parsed:
        return None

    winter = round(sum(k for _, m, k in parsed if m in _GAS_WINTER_MONTHS), 1)
    summer = round(sum(k for _, m, k in parsed if m not in _GAS_WINTER_MONTHS), 1)
    total = round(winter + summer, 1)
    if total <= 0:
        return None

    winter_pct = round(winter / total * 100)
    summer_pct = 100 - winter_pct  # force the two shares to add up to 100
    both_seasons = winter > 0 and summer > 0

    return {
        "winterPct": winter_pct,
        "summerPct": summer_pct,
        "winterKwh": winter,
        "summerKwh": summer,
        "totalKwh": total,
        "monthsUsed": len(parsed),
        "monthsInWindow": len(window),
        "monthly": {
            "months": [lbl for lbl, _, _ in parsed],
            "values": [k for _, _, k in parsed],
        },
        "confidence": "high" if (both_seasons and len(parsed) >= 10) else "low",
    }
'''

FIXED_SUMMARIZE_GAS_DATA = '''def _summarize_gas_data(pairs):
    """Gas counterpart of _summarize_enedis_data. Same window-position fix
    as _compute_gas_season_split above (see that function's docstring for
    the full explanation) -- this is the version used to build the
    peak_months / lowest_months / season_split facts that feed the slide-4
    profil/exposition/strategie/recommandation write-up."""
    if not pairs:
        return None

    window = list(pairs)[-12:]

    parsed = []
    for item in window:
        if not isinstance(item, dict):
            continue
        mnum = _fr_month_to_num(item.get("month"))
        kwh = _to_float(item.get("kwh"))
        if mnum is None or kwh is None:
            continue
        parsed.append((item.get("month"), mnum, kwh))

    if not parsed:
        return None

    total = round(sum(k for _, _, k in parsed), 1)
    if total <= 0:
        return None
    avg = round(total / len(parsed), 1)

    ranked = sorted(parsed, key=lambda t: t[2], reverse=True)
    peak_months = [{"month": _norm_month_mmyyyy(l), "value": k} for l, _, k in ranked[:3]]
    lowest_months = [{"month": _norm_month_mmyyyy(l), "value": k} for l, _, k in ranked[-3:]][::-1]
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
            "from": _norm_month_mmyyyy(window_labels[0]) if window_labels else None,
            "to": _norm_month_mmyyyy(window_labels[-1]) if window_labels else None,
        },
        "total_annual_kwh": total,
        "avg_monthly_kwh": avg,
        "peak_months": peak_months,
        "lowest_months": lowest_months,
        "peak_to_average_ratio": peak_to_average_ratio,
        "consumption_by_period": [],  # gas is a single series -- no tariff periods
        "season_split": season_split,
    }
'''

FIXED_EXTRACT_GAS_MONTHLY_PAIRS = '''def _extract_gas_monthly_pairs(pdf_bytes):
    """Get the monthly gas-consumption series from an invoice (PDF or image)
    as [{"month","kwh"}, ...], using the vision-based, design-agnostic
    reader directly.

    Deliberately does NOT attempt a coordinate-based fast path first. That
    approach only produces a correct result when a specific supplier's PDF
    happens to put exactly one numeric label per month in predictable rows
    -- true for some invoice layouts, silently wrong or empty for others.
    With multiple document variants in play, "try the fragile method first"
    just adds a chance of returning incomplete/incorrect data before ever
    reaching the reliable path, rather than actually saving meaningful time.

    Returns (pairs, source):
      - source 'vision' with a list (possibly empty, may contain nulls),
      - source 'empty' when no gas chart/table was found on any checked page,
      - source 'vision_unavailable' when every vision call failed outright
        (e.g. the model/server was unreachable), distinct from 'empty' so
        the caller can tell "we don't know" from "confirmed none."
    """
    vision_pairs = _extract_gas_monthly_via_vision(pdf_bytes)
    if vision_pairs is None:
        return None, "vision_unavailable"
    if vision_pairs:
        return vision_pairs, "vision"
    return [], "empty"
'''

NEW_VISION_FUNCTIONS = '''# ═════════════════════════════════════════════════════════════════════════
# NEW: vision-based gas chart/table extraction (design-agnostic replacement
# for the old coordinate-only method). Confirmed end-to-end on real
# invoices at max_dimension=1600 / num_ctx=8192 (13/13 exact match on the
# invoice with the smallest, most error-prone bars).
# ═════════════════════════════════════════════════════════════════════════

def _detect_file_type(file_bytes):
    """Detect whether downloaded bytes are a PDF or a plain image, without
    relying on the source URL's extension (nginx-served URLs may lack one,
    or be misleading). Returns 'pdf' or 'image'; raises ValueError if
    neither can be confirmed."""
    if file_bytes[:5] == b"%PDF-":
        return "pdf"
    try:
        Image.open(io.BytesIO(file_bytes)).verify()
        return "image"
    except Exception:
        raise ValueError("Downloaded file is neither a recognizable PDF nor an image")


def _render_file_to_images(file_bytes, dpi=200, max_pages=8):
    """Return [(page_number, PIL.Image), ...] for either a PDF (one entry
    per page, up to max_pages, rendered via PyMuPDF -- pure pip package, no
    poppler/system binary needed) or a plain image (a single entry).
    Works directly from downloaded bytes, no temp file needed."""
    file_type = _detect_file_type(file_bytes)

    if file_type == "image":
        img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        return [(1, img)]

    import fitz  # PyMuPDF -- imported lazily so it's only required if a PDF actually shows up

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        zoom = dpi / 72.0  # PDF units are 72 points/inch
        mat = fitz.Matrix(zoom, zoom)
        n = len(doc) if not max_pages else min(len(doc), max_pages)
        pages = []
        for i in range(n):
            pix = doc[i].get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append((i + 1, img))
        return pages
    finally:
        doc.close()


_GAS_CHART_VISION_MODEL = "qwen2.5vl:7b"
_GAS_CHART_VISION_MAX_PAGES = 8       # stop scanning a document after this many pages
_GAS_CHART_VISION_DPI = 200

# Confirmed by direct A/B testing on a real invoice (13 months, including
# two very small values -- 11 and 33 kWh -- next to a 1984 kWh peak): at
# lower settings (1200px), those small bars were misread or dropped
# entirely. At 1600px, the exact same invoice came back 13/13 correct.
# 8192 context was confirmed sufficient at 1600px in that same test.
_GAS_CHART_VISION_MAX_DIMENSION = 1600
_GAS_CHART_VISION_NUM_CTX = 8192

_GAS_CHART_VISION_HINTS = 'a flame/gas icon, or text mentioning "Gaz Naturel", "Gaz", or "PCE" near the chart'


def _build_gas_vision_prompt():
    """Prompt asks the model to detect EITHER a bar chart OR a plain table/
    row of numbers under month labels (some suppliers -- e.g. TotalEnergies
    -- show monthly history as a "Historique de la consommation" line with
    no bars at all) -- confirmed necessary after testing against a
    non-EDF invoice design that has no chart whatsoever, just a row of
    numbers under month labels."""
    return f"""First, check: does this image contain a monthly gas consumption history (in kWh)? This can be shown in EITHER of two forms -- treat both as a match:
  (a) A bar chart, with a number printed above each bar.
  (b) A table or plain row of numbers under a row of month labels (no bars at all) -- e.g. a "Historique de la consommation" section listing one value per month in a line, sometimes with small colored markers indicating whether each value was estimated, an actual meter reading, or self-reported.

This is distinct from an electricity consumption chart/table -- look for indicators like {_GAS_CHART_VISION_HINTS}.

If NEITHER form is present anywhere in this image (either no monthly consumption history at all, or only one for a different energy type), respond with exactly this and nothing else:
{{"gas_chart_found": false}}

If EITHER form IS present, extract the monthly consumption values from it. For each month shown, return the number associated with it -- whether that number sits above a bar or simply appears in a row/table under the month's label. If a specific month has no visible bar, no printed number, or no value in the row/table, use null for that month only.

Read the numbers one at a time, in the order the months appear. For each one, look closely at every digit before answering.

Return ONLY valid JSON, no markdown formatting, no explanation, in this format:
{{"gas_chart_found": true, "values": {{"Month Year": value_or_null, ...}}}}
using the exact month labels shown, in the same order they appear (left to right, or top to bottom if stacked)."""


def _resize_image_for_vision(pil_image, max_dimension=_GAS_CHART_VISION_MAX_DIMENSION):
    """Downscale so the image's longest side is at most max_dimension.
    A full PDF page rendered at 200+ DPI can be 1600-2400px, which costs
    more vision tokens than the model's context window may allow --
    downscaling keeps requests within budget. max_dimension=1600 is the
    confirmed floor for reading small bars accurately -- going lower
    reintroduces the small-bar misread/drop failure mode."""
    w, h = pil_image.size
    longest = max(w, h)
    if longest <= max_dimension:
        return pil_image
    scale = max_dimension / longest
    return pil_image.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def _image_to_base64_png(pil_image):
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _call_vision_llm(prompt, image_b64, model=_GAS_CHART_VISION_MODEL,
                      num_ctx=_GAS_CHART_VISION_NUM_CTX, timeout=280):
    """Vision counterpart of _call_market_llm: same Ollama-compatible
    /api/generate endpoint and urllib.request usage, plus an attached image
    and an explicit context window (a rendered page costs meaningfully more
    context as vision tokens than a text prompt does). Returns the raw
    response text, or None on any network/timeout/parse failure (mirrors
    _call_market_llm's failure contract)."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"num_ctx": num_ctx},
        "keep_alive": "30m",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://gpt.caansoft.com/gpt/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        print(f"Vision LLM call failed: {e}")
        return None

    return body.get("response") or None


def _extract_json_object(text):
    """Pull the last {...} block out of a vision-model response, tolerating
    ```json fences and stray commentary the model sometimes adds despite
    being told not to. Returns a dict, or None if nothing parseable was
    found."""
    if not text:
        return None
    t = re.sub(r"```(?:json)?|```", "", text).strip()
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None


def _extract_gas_monthly_via_vision(file_bytes, max_pages=_GAS_CHART_VISION_MAX_PAGES):
    """Design-agnostic gas chart/table reader. Renders each page (or loads
    the single image) and asks the vision model to locate the GAS monthly
    consumption history specifically -- explicitly distinguished from
    electricity, since a single invoice commonly has both -- stopping at
    the first page where it's found rather than checking every remaining
    page. Works the same way regardless of supplier, chart library, or
    whether the source is a scanned/image-only file.

    Returns:
      - [{"month": <label>, "kwh": <value_or_None>}, ...] if found. Null
        months are KEPT (not dropped) -- _compute_gas_season_split and
        _summarize_gas_data need the full sequence, gaps included, to
        correctly determine the true most-recent-12-months window.
      - [] if every checked page was confirmed to have no gas chart/table.
      - None if every vision call failed outright (network/model down), so
        the caller can tell "confirmed no chart" apart from "couldn't check."
    """
    try:
        pages = _render_file_to_images(file_bytes, dpi=_GAS_CHART_VISION_DPI, max_pages=max_pages)
    except Exception as e:
        print(f"Could not render file to images for vision extraction: {e}")
        return None

    prompt = _build_gas_vision_prompt()
    any_call_succeeded = False

    for page_num, img in pages:
        img = _resize_image_for_vision(img)
        img_b64 = _image_to_base64_png(img)

        raw = _call_vision_llm(prompt, img_b64)
        if raw is None:
            continue  # this page's call failed; still try the remaining pages

        any_call_succeeded = True
        parsed = _extract_json_object(raw)
        if not parsed:
            continue

        if parsed.get("gas_chart_found") is True:
            values = parsed.get("values") or {}
            pairs = [{"month": k, "kwh": v} for k, v in values.items()]  # keep nulls
            print(f"Gas chart/table found via vision on page {page_num}: "
                  f"{sum(1 for p in pairs if p['kwh'] is not None)} of {len(pairs)} month(s) with a value")
            return pairs
        # gas_chart_found is False (or missing/unparseable) -> keep scanning

    if not any_call_succeeded:
        return None  # every page's call failed outright
    return []  # every page was checked; none had a gas chart/table
'''


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 apply_gas_vision_migration.py /path/to/your/views.py")
        sys.exit(1)

    src_path = Path(sys.argv[1])
    if not src_path.exists():
        print(f"File not found: {src_path}")
        sys.exit(1)

    source = src_path.read_text(encoding="utf-8")
    print(f"Loaded {src_path} ({len(source)} chars)\n")

    # 1. Add the PIL import, right after the PyPDF2 import line
    if "from PIL import Image" not in source:
        source = source.replace(
            "from PyPDF2 import PdfReader, PdfWriter",
            "from PyPDF2 import PdfReader, PdfWriter\nfrom PIL import Image",
            1,
        )
        print("Added: from PIL import Image")
    else:
        print("Skipped: PIL import already present")

    # 2. Add the new _MONTH_PREFIXES dict (needed by the fixed _fr_month_to_num)
    #    right before the existing _fr_month_to_num definition.
    month_prefixes_block = '''_MONTH_PREFIXES = {
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


'''
    print("\nStep 1: Deleting dead functions...")
    for func in ["_extract_monthly_from_chart", "_llm_extract_monthly_consumption", "_extract_json_array"]:
        source = delete_function(source, func)

    print("\nStep 2: Replacing fixed functions...")
    source = insert_before_function(source, "_fr_month_to_num", month_prefixes_block)
    source = replace_function(source, "_fr_month_to_num", FIXED_FR_MONTH_TO_NUM)
    source = replace_function(source, "_compute_gas_season_split", FIXED_COMPUTE_GAS_SEASON_SPLIT)
    source = replace_function(source, "_summarize_gas_data", FIXED_SUMMARIZE_GAS_DATA)
    source = replace_function(source, "_extract_gas_monthly_pairs", FIXED_EXTRACT_GAS_MONTHLY_PAIRS)

    print("\nStep 3: Inserting new vision-extraction functions...")
    source = insert_before_function(source, "_extract_gas_monthly_pairs", NEW_VISION_FUNCTIONS)

    print("\nStep 4: Fixing the xsource check inside analyze_gas_invoice...")
    before = source.count('xsource == "llm_unavailable"')
    source = source.replace('xsource == "llm_unavailable"', 'xsource == "vision_unavailable"')
    print(f"  Replaced {before} occurrence(s)")

    print("\nStep 5: Switching _call_market_llm (electricity text analysis) from gpt-oss:20b to qwen2.5vl:7b...")
    before = source.count('"model": "gpt-oss:20b"')
    source = source.replace('"model": "gpt-oss:20b"', '"model": "qwen2.5vl:7b"')
    print(f"  Replaced {before} occurrence(s)")
    print("  (This affects _generate_market_analysis and _generate_consumption_analysis,")
    print("   both of which call _call_market_llm -- same text-in/text-out contract,")
    print("   Qwen handles plain text prompts the same way any text model does.)")

    out_path = Path("views_MIGRATED.py")
    out_path.write_text(source, encoding="utf-8")
    print(f"\nDone. Wrote {out_path.resolve()} ({len(source)} chars)")
    print(f"\nReview it with:\n  diff {src_path} {out_path}")
    print(f"\nIf it looks right:\n  cp {out_path} {src_path}")

    # Sanity check: does it still parse as valid Python?
    import ast
    try:
        ast.parse(source)
        print("\nSyntax check: PASSED (the migrated file parses as valid Python)")
    except SyntaxError as e:
        print(f"\nSyntax check: FAILED -- {e}")
        print("Do NOT copy this over your real views.py yet -- something didn't merge cleanly.")
        sys.exit(1)


if __name__ == "__main__":
    main()
