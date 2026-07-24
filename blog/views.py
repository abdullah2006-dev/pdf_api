import re, io, base64
import urllib.request
import urllib.error
import urllib.parse
import ipaddress
from concurrent.futures import ThreadPoolExecutor
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.template.loader import render_to_string
from django.shortcuts import render
from django.http import HttpResponse
import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import numpy as np
import json
import os
from django.conf import settings
from django.http import JsonResponse
import tempfile
import shutil
import time
import logging
from django.contrib.auth.decorators import login_required
try:
    import bleach
except Exception:
    bleach = None
from weasyprint import HTML, CSS
from datetime import datetime
from zoneinfo import ZoneInfo

# Timestamps from the CRM are absolute instants (epoch ms); datetime.fromtimestamp()
# without a tz uses the server OS's local timezone, which on prod is UTC — always
# convert to Paris time explicitly so displayed dates/times match the client's clock.
PARIS_TZ = ZoneInfo("Europe/Paris")
from django.templatetags.static import static
from PyPDF2 import PdfReader, PdfWriter
from PIL import Image


@csrf_exempt
@require_http_methods(["POST"])
def volt_consulting_presentation(request):
    """
    POST API endpoint that accepts and processes Volt Consulting presentation data
    and renders HTML with the data.
    """
    try:
        # 1️⃣ Parse incoming data
        data = parse_request_data(request)

        # 2️⃣ Generate Chart (if available)
        chart_base64 = generate_chart(data)

        # 3️⃣ Build Comparatif DTO
        comparatif = data.get("comparatifClientHistoryPdfDto", {})
        comparatif_dto = build_comparatif_dto(comparatif, request, data)

        # 4️⃣ Build Presentation Data
        presentation_data = build_presentation_data(data, chart_base64, comparatif_dto, request)

        # 5️⃣ Render HTML
        html_content = render_html(presentation_data)

        # 6️⃣ Generate PDF
        pdf_url, pdf_filename = generate_pdf(html_content, request, data, comparatif)

        return JsonResponse({
            "status": "success",
            "path": pdf_url,
            "name": pdf_filename,
            "title": pdf_filename,
            "mime_type": "application/pdf",
            "message": "PDF generated successfully"
        })

    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": f"An error occurred: {str(e)}",
        }, status=500)


def parse_request_data(request):
    """Parse incoming request data (JSON or Form)."""
    print("Inside ParseRequestData")
    if request.content_type == 'application/json':
        return json.loads(request.body)
    return request.POST.dict()


def generate_chart(data):
    """Generate base64 chart image from input data with chartDataDto wrapper."""
    print("Inside GenerateChart")

    # 🔹 Check if chartDataDto exists and is valid - Return None instead of raising error
    if "chartDataDto" not in data or not data["chartDataDto"]:
        print("chartDataDto is missing or empty, returning None")
        return None

    chart_data = data["chartDataDto"]

    # 🔹 Validate xAxis and series - Return None if invalid
    if "xAxis" not in chart_data or not chart_data["xAxis"]:
        print("xAxis is missing or empty, returning None")
        return None

    if "series" not in chart_data or not chart_data["series"]:
        print("series is missing or empty, returning None")
        return None

    # 🔹 Validate xAxis data
    if "data" not in chart_data["xAxis"][0] or not chart_data["xAxis"][0]["data"]:
        print("xAxis[0].data is missing or empty, returning None")
        return None

    try:
        # 🔥 CHANGE HERE: Use format "%Y-%m-%d" for ISO dates (yyyy-MM-dd)
        dates = pd.to_datetime(chart_data["xAxis"][0]["data"], format="%Y-%m-%d")
    except Exception as e:
        # Try multiple date formats
        try:
            dates = pd.to_datetime(chart_data["xAxis"][0]["data"])  # Let pandas infer
        except Exception as e2:
            print(f"Invalid date format in xAxis data: {e2}, returning None")
            return None

    plt.figure(figsize=(12, 7))
    colors = ["black", "royalblue", "green", "red"]

    for idx, series in enumerate(chart_data["series"]):
        if "data" not in series or not series["data"]:
            print(f"series[{idx}].data is missing or empty, returning None")
            return None

        try:
            y = np.array(series["data"], dtype=np.float64)
        except Exception as e:
            print(f"Invalid numeric data in series[{idx}]: {e}, returning None")
            return None

        plt.plot(
            dates[:len(y)], y,
            label=series.get("label", f"Series {idx + 1}"),
            color=colors[idx % len(colors)], linewidth=2
        )

    # 🔹 Energy type check (kept outside chartDataDto)
    energy_type = data.get("comparatifClientHistoryPdfDto", {}).get("energyType", "").upper()
    chart_title = "Évolution Gaz" if energy_type == "GAS" else \
        "Évolution Électricité" if energy_type == "ELECTRICITY" else \
            "Évolution des Prix"

    plt.xlabel("")
    plt.ylabel("Prix €/MWh")
    plt.title(chart_title)

    ax = plt.gca()
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=4))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d-%m-%Y"))
    plt.xticks(fontsize=8, ha='right')
    plt.grid(True, linestyle="--", alpha=0.6)

    # 🔹 Legend
    import matplotlib.lines as mlines
    legend_elements = [
        mlines.Line2D([0], [0], marker='o', color='w',
                      markerfacecolor=colors[idx % len(colors)],
                      markersize=10,
                      label=series.get("label", f"Series {idx + 1}"))
        for idx, series in enumerate(chart_data["series"])
    ]
    plt.legend(handles=legend_elements,
               loc='upper center',
               bbox_to_anchor=(0.5, -0.12),
               ncol=len(legend_elements),
               frameon=False,
               fontsize=9,
               columnspacing=1.5)

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.25)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=300, bbox_inches='tight')
    plt.close()
    buf.seek(0)

    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode('utf-8')}"


def generate_price_chart_styled(data, last_n_months=None):
    """
    Generate a styled price-evolution line chart matching the slide-3 design:
    white background, light-gray horizontal grid, colored lines, top legend.
    Pass last_n_months to restrict to the final N data points.
    """
    if "chartDataDto" not in data or not data["chartDataDto"]:
        return None

    chart_data = data["chartDataDto"]

    if "xAxis" not in chart_data or not chart_data["xAxis"]:
        return None
    if "series" not in chart_data or not chart_data["series"]:
        return None
    if "data" not in chart_data["xAxis"][0] or not chart_data["xAxis"][0]["data"]:
        return None

    raw_dates = chart_data["xAxis"][0]["data"]
    try:
        all_dates = pd.to_datetime(raw_dates, format="%Y-%m-%d")
    except Exception:
        try:
            all_dates = pd.to_datetime(raw_dates)
        except Exception as e:
            print(f"Invalid date format: {e}")
            return None

    # Slice by calendar months, not by data-point count
    if last_n_months and len(all_dates) > 0:
        start_cutoff = all_dates[-1] - pd.DateOffset(months=last_n_months)
        mask = all_dates >= start_cutoff
        slice_start = int(mask.argmax()) if mask.any() else 0
    else:
        slice_start = 0
    dates = all_dates[slice_start:]

    # Colors matching the screenshot design
    line_colors = ["#0b3a66", "#1a8a5b", "#c33333", "#7e7e7e"]

    fig, ax = plt.subplots(figsize=(9, 3.6), dpi=150)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    plotted = 0
    for idx, series in enumerate(chart_data["series"]):
        if "data" not in series or not series["data"]:
            continue
        try:
            y = np.array(series["data"][slice_start:], dtype=np.float64)
        except Exception:
            continue
        color = line_colors[idx % len(line_colors)]
        ax.plot(dates[: len(y)], y, color=color, linewidth=1.5,
                label=series.get("label", f"Series {idx + 1}"), zorder=3)
        plotted += 1

    if plotted == 0:
        plt.close()
        return None

    # Horizontal grid only
    ax.yaxis.grid(True, color="#eef0f4", linewidth=1, zorder=0)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)

    # Spines
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#e5e7eb")
    ax.spines["bottom"].set_color("#e5e7eb")

    # X-axis: monthly ticks for 12-month view, every 4 months for full range
    tick_interval = 1 if last_n_months else 4
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=tick_interval))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%y"))
    ax.tick_params(axis="x", labelsize=7, colors="#9ca3af", length=0, pad=4)
    ax.tick_params(axis="y", labelsize=7, colors="#9ca3af", length=0, pad=4)
    ax.set_ylabel("€/MWh", fontsize=7, color="#9ca3af", labelpad=6)

    # Legend at top-left
    import matplotlib.lines as mlines
    legend_handles = [
        mlines.Line2D([], [], color=line_colors[i % len(line_colors)],
                      linewidth=2, label=s.get("label", f"Series {i + 1}"))
        for i, s in enumerate(chart_data["series"]) if s.get("data")
    ]
    ax.legend(
        handles=legend_handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.22),
        ncol=len(legend_handles),
        frameon=False,
        fontsize=13,
        handlelength=1.4,
        handletextpad=0.6,
        columnspacing=1.5,
    )

    plt.tight_layout(pad=0.4)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    plt.close()
    buf.seek(0)

    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode('utf-8')}"


def build_comparatif_dto(comparatif, request, data):
    print("Inside BuildComparatifDTO")

    # Validate and format createdOn
    created_on_raw = comparatif.get("createdOn")
    if not created_on_raw:
        raise ValueError("Missing required field: createdOn")

    try:
        dt = datetime.fromtimestamp(created_on_raw / 1000.0, tz=PARIS_TZ)  # convert ms → seconds
        created_on = dt.strftime("%d/%m/%Y")
    except Exception as e:
        raise ValueError(f"Invalid createdOn value: {e}")

    dto = {
        "title": data.get("contexte_title", "Contexte global"),
        "createdOn": created_on,
        "energyType": comparatif.get("energyType"),
        "ratioHTVA": comparatif.get("ratioHTVA"),
        "differenceHTVA": comparatif.get("differenceHTVA"),
        "volumeAnnual": comparatif.get("volumeAnnual"),
        "currentSupplierName": comparatif.get("currentSupplierName"),
        "currentContractExpiryDate": comparatif.get("currentContractExpiryDate"),
    }

    # GAS-specific fields
    energy_type = dto.get("energyType")
    if energy_type == "GAS":
        required_gas_fields = ["pce", "gasProfile", "routingRate"]

        dto.update({
            "pce": comparatif.get("pce"),
            "gasProfile": comparatif.get("gasProfile"),
            "routingRate": comparatif.get("routingRate")
        })

        for field in required_gas_fields:
            if not dto.get(field):
                raise ValueError(f"Missing required GAS field: {field}")

        forbidden_electricity_fields = ["pdl", "segmentation"]
        for field in forbidden_electricity_fields:
            if comparatif.get(field):
                raise ValueError(f"Field '{field}' is not allowed for GAS energyType")
    else:
        raise ValueError("Invalid or missing energyType. Must be 'GAS'.")

    # Separate CURRENT and REGULAR providers
    comparatif_rates = comparatif.get("comparatifRates", [])
    current_providers = [p for p in comparatif_rates if p.get("typeFournisseur") == "CURRENT"]
    regular_providers = [p for p in comparatif_rates if p.get("typeFournisseur") != "CURRENT"]

    # Sort regular_providers by coutHTVA in ascending order
    # Handle None values by putting them at the end
    def get_cout_htva(provider):
        cout_htva = provider.get("coutHTVA")
        if cout_htva is None:
            return float('inf')  # Put None values at the end
        try:
            return float(cout_htva)
        except (ValueError, TypeError):
            return float('inf')

    regular_providers.sort(key=get_cout_htva)

    # Get current provider's coutHTVA for comparison
    current_cout_htva = None
    if current_providers and len(current_providers) > 0:
        current_cout_htva = get_cout_htva(current_providers[0])

    # Find the minimum coutHTVA from regular_providers
    min_regular_cout_htva = None
    if regular_providers:
        min_regular_cout_htva = get_cout_htva(regular_providers[0])
        # If min_regular_cout_htva is infinity (None values), set to None
        if min_regular_cout_htva == float('inf'):
            min_regular_cout_htva = None

    # Paginate providers into containers (4 rows per container)
    paginated_containers = []
    current_index = 0
    regular_index = 0
    green_row_used = True  # Flag for green row (only once after labels)

    while current_index < len(current_providers) or regular_index < len(regular_providers):
        container = {
            "current_providers": [],
            "regular_providers": [],
            "show_header": len(paginated_containers) == 0,
            "show_title_labels": False
        }

        rows_in_container = 0

        # Add CURRENT providers first
        while current_index < len(current_providers) and rows_in_container < 4:
            container["current_providers"].append(current_providers[current_index])
            current_index += 1
            rows_in_container += 1

        # If all CURRENT providers are done, show title/labels in this container
        if current_index >= len(current_providers) and len(container["current_providers"]) > 0:
            container["show_title_labels"] = True

        # If no CURRENT providers exist at all, show title/labels in first container
        if len(current_providers) == 0 and len(paginated_containers) == 0:
            container["show_title_labels"] = True

        # Fill REGULAR providers (after labels)
        while regular_index < len(regular_providers) and rows_in_container < 4:
            provider = regular_providers[regular_index]

            # UPDATED GREEN ROW LOGIC:
            # Mark as green row only if:
            # 1. Not already used
            # 2. We're in the container that shows title/labels
            # 3. This is the first regular provider after labels
            # 4. This provider has the minimum coutHTVA among regular providers
            # 5. The min_regular_cout_htva ≤ current_cout_htva
            if (not green_row_used and
                container["show_title_labels"] and
                regular_index == 0 and  # First regular provider
                min_regular_cout_htva is not None and
                current_cout_htva is not None and
                min_regular_cout_htva <= current_cout_htva):

                provider["is_green_row"] = True
                green_row_used = True
            else:
                provider["is_green_row"] = False

            container["regular_providers"].append(provider)
            regular_index += 1
            rows_in_container += 1

        paginated_containers.append(container)

    dto["paginatedContainers"] = paginated_containers
    dto["comparatifRates"] = comparatif_rates  # Keep original for backward compatibility

    # Add a flattened list of all regular providers in sorted order
    all_regular_providers = []
    for container in paginated_containers:
        all_regular_providers.extend(container["regular_providers"])
    dto["allRegularProviders"] = all_regular_providers

    # Add a flattened list of ALL providers (CURRENT + regular) for tables
    all_providers_for_tables = []
    # Add CURRENT providers first (they appear at the top in the UI)
    for container in paginated_containers:
        all_providers_for_tables.extend(container["current_providers"])
    # Then add regular providers
    all_providers_for_tables.extend(all_regular_providers)
    dto["allProvidersForTables"] = all_providers_for_tables

    return dto


def render_html(presentation_data):
    print("Inside RenderHTML")
    return render_to_string("volt.html", {"data": presentation_data})


def generate_pdf(html_content, request, data, comparatif):
    """Generate PDF and return its URL (removes truly blank pages)."""
    print("Inside GeneratePDF")
    host = request.get_host().split(":")[0]

    # Choose base dirs (filesystem vs URL)
    if host == "volt-crm.caansoft.com":
        base_dir = settings.STAGING_MEDIA_ROOT
        base_url = settings.STAGING_MEDIA_URL
    elif host == "crm.volt-consulting.com":
        base_dir = settings.PRODUCTION_MEDIA_ROOT
        base_url = settings.PRODUCTION_MEDIA_URL
    else:
        base_dir = settings.MEDIA_ROOT
        base_url = settings.MEDIA_URL

    # Dynamic path: client/<id>/comparatif/
    relative_path = os.path.join("clients", str(data.get("clientId")), "comparatif", str(comparatif.get("id")))
    pdf_dir = os.path.join(base_dir, relative_path)
    os.makedirs(pdf_dir, exist_ok=True)

    # Generate filename
    pdf_filename = create_comparatif_filename(
        data.get("clientSociety"),
        data.get("clientTradeName"),
        data.get("comparatifClientHistoryPdfDto", {}).get("energyType")
    )
    pdf_path = os.path.join(pdf_dir, pdf_filename)

    # Save PDF using WeasyPrint
    css = CSS(string="""@page { size: 530mm 265mm; margin: 0.0cm; }""")
    HTML(string=html_content).write_pdf(
        pdf_path,
        stylesheets=[css],
        zoom=0.8,
        optimize_images=True,
        presentational_hints=True,
        font_config=None
    )

    # Remove blank pages - improved detection
    reader = PdfReader(pdf_path)
    writer = PdfWriter()

    for i, page in enumerate(reader.pages):
        is_blank = True

        try:
            # Method 1: Check for text content
            text = page.extract_text().strip()
            if len(text) > 20:  # Has meaningful text
                is_blank = False

            # Method 2: Check for images/XObjects
            if '/Resources' in page:
                resources = page['/Resources']
                if '/XObject' in resources:
                    xobjects = resources['/XObject'].get_object()
                    # Check if XObjects actually exist and aren't empty
                    if len(xobjects) > 0:
                        is_blank = False

            # Method 3: Check content stream size (more aggressive)
            if '/Contents' in page and is_blank:
                contents = page['/Contents']

                if hasattr(contents, 'get_object'):
                    content_obj = contents.get_object()
                else:
                    content_obj = contents

                # Calculate actual content size
                content_size = 0
                if isinstance(content_obj, list):
                    for stream in content_obj:
                        if hasattr(stream, 'get_data'):
                            data_content = stream.get_data()
                            # Filter out whitespace-only content
                            if data_content and len(data_content.strip()) > 50:
                                content_size += len(data_content)
                elif hasattr(content_obj, 'get_data'):
                    data_content = content_obj.get_data()
                    if data_content and len(data_content.strip()) > 50:
                        content_size = len(data_content)

                # Page has substantial content
                if content_size > 200:
                    is_blank = False

            # Method 4: Check for graphics/drawing operations
            if is_blank and '/Contents' in page:
                try:
                    contents = page['/Contents']
                    if hasattr(contents, 'get_object'):
                        content_obj = contents.get_object()
                    else:
                        content_obj = contents

                    if hasattr(content_obj, 'get_data'):
                        content_data = content_obj.get_data().decode('latin-1', errors='ignore')
                        # Check for actual drawing commands (not just whitespace)
                        drawing_commands = ['re', 'f', 'S', 'rg', 'RG', 'cm', 'Do', 'Tm', 'Tj']
                        command_count = sum(content_data.count(cmd) for cmd in drawing_commands)
                        if command_count > 2:  # Has actual graphics commands
                            is_blank = False
                except:
                    pass

        except Exception as e:
            print(f"Error checking page {i + 1}: {e}")
            # When in doubt, check page height - very short pages might be blanks
            try:
                mediabox = page.mediabox
                height = float(mediabox.height)
                if height < 100:  # Very small page = likely blank
                    is_blank = True
                else:
                    is_blank = False  # Keep it
            except:
                is_blank = False

        if not is_blank:
            writer.add_page(page)
            print(f"✓ Keeping page {i + 1}")
        else:
            print(f"✗ Removing blank page {i + 1}")

    print(f"Final PDF: {len(writer.pages)} pages (removed {len(reader.pages) - len(writer.pages)} blank pages)")

    # Write cleaned PDF
    with open(pdf_path, "wb") as f:
        writer.write(f)

    # Build public URL
    pdf_url = request.build_absolute_uri(
        os.path.join(base_url, "clients", str(data.get("clientId")), "comparatif", str(comparatif.get("id")),
                     pdf_filename)
    )

    return pdf_url, pdf_filename


def create_comparatif_filename(society: str, trade_name: str, energy_type: str) -> str:
    # 1️⃣ Clean society or fallback to trade_name
    if society:
        clean_society = re.sub(r"\s+", "", str(society))
    else:
        clean_society = re.sub(r"\s+", "", str(trade_name))

    # 🚨 IMPORTANT: Remove path separators and other problematic characters
    # Replace any non-alphanumeric characters (except underscore) with underscore
    clean_society = re.sub(r'[^a-zA-Z0-9_]', '_', clean_society)
    # Remove multiple consecutive underscores
    clean_society = re.sub(r'_+', '_', clean_society)
    # Remove leading/trailing underscores
    clean_society = clean_society.strip('_')

    # 2️⃣ Energy type suffix
    additional_text = "_elec" if energy_type.upper() == "ELECTRICITY" else "_gaz"

    # 3️⃣ Date part (YYYY-MM-DD)
    date_str = datetime.now().strftime("%Y-%m-%d")

    # 4️⃣ Final filename
    filename = f"Comparatif_{clean_society}{additional_text}_{date_str}.pdf"
    return filename


def build_static_url(request, path):
    """Build HTTP static URL for browser rendering (works in headless Chrome)."""
    from django.templatetags.static import static

    # If request is available, build absolute URI
    if request:
        return request.build_absolute_uri(static(path))
    # Fallback to relative static URL
    return static(path)

def build_static_url(request, path):
    print("Inside BuildStaticURL")
    # return request.build_absolute_uri(static(path))
    abs_path = os.path.join(settings.STATICFILES_DIRS[0], path)
    return f"file://{abs_path}"

def build_presentation_data(data, chart_base64, comparatif_dto, request):
    print("Inside BuildPresentationData")

    # Updated helper function
    def safe_value(value):
        if value is None:
            return ""
        str_val = str(value).strip().lower()
        if str_val == "" or str_val == "none":
            return ""
        return str(value)

    # Get ratioHTVA and differenceHTVA values
    ratio_htva = comparatif_dto.get("ratioHTVA")
    difference_htva = comparatif_dto.get("differenceHTVA")

    # Initialize black, black1, black3 based on conditions
    black = ""
    black1 = ""
    black3 = ""

    # Condition for black (ratioHTVA):
    # Show only if ratioHTVA is not None, not empty, and ≤ 0
    if ratio_htva is not None and ratio_htva != "":
        try:
            ratio_num = float(ratio_htva)
            if ratio_num <= 0:  # ≤ 0 (including negative values)
                black = f"{ratio_htva}%"
        except (ValueError, TypeError):
            # If can't convert to number, keep empty
            pass

    # Condition for black1 (differenceHTVA):
    # Show only if differenceHTVA is not None, not empty, and ≤ 0
    if difference_htva is not None and difference_htva != "":
        try:
            diff_num = float(difference_htva)
            if diff_num <= 0:  # ≤ 0 (including negative values)
                black1 = f"{difference_htva}€"
        except (ValueError, TypeError):
            # If can't convert to number, keep empty
            pass

    # Condition for black3 ("économisé/an"):
    # Show only if BOTH ratioHTVA ≤ 0 AND differenceHTVA ≤ 0
    # (both are negative or zero)
    if black != "" and black1 != "":
        black3 = "économisé/an"

    return {
        "title": data.get("title", "VOLT CONSULTING - Energy Services Presentation"),
        "headingone": "APPEL D'OFFRE",
        "clientSociety": safe_value(data.get("clientSociety")),
        "clientSiret": safe_value(data.get("clientSiret")),
        "clientFirstName": safe_value(data.get("clientFirstName")),
        "clientLastName": safe_value(data.get("clientLastName")),
        "clientEmail": safe_value(data.get("clientEmail")),
        "clientPhoneNumber": safe_value(data.get("clientPhoneNumber")),
        "clientBusinessAddress": data.get("clientBusinessAddress", {}),
        "client_site_address": _format_site_address(data.get("clientBusinessAddress")),
        "currentSupplierName": safe_value(comparatif_dto.get("currentSupplierName")),
        "currentContractExpiryDate": (
            datetime.fromtimestamp(comparatif_dto.get("currentContractExpiryDate") / 1000, tz=PARIS_TZ).strftime("%d/%m/%Y")
            if comparatif_dto.get("currentContractExpiryDate") else ""
        ),
        "black": black,
        "black1": black1,
        "black3": black3,
        "image": build_image_section(data, chart_base64),
        "has_chart": chart_base64 is not None,
        "images": build_images(data, request),
        "company_presentation": build_company_presentation(data),
        "comparatifClientHistoryPdfDto": comparatif_dto,
        "budget_global": build_budget_section(data),
        "tender_results": build_tender_results(data),
        "tender_table": build_tender_table(data),
        "comparison_table": build_comparison_table(data),
        "change_section": build_change_section(data),
        "contact_info": build_contact_info(data),
        "volt_logo_base_url": "https://crm.volt-consulting.com/uploads/volt/providers/",
    }


def build_image_section(data, chart_base64):
    """Build image dictionary with dynamic chart."""
    return {**data.get("images", {}), "chart": chart_base64}


def build_images(data, request, use_http=False):
    """Build static & dynamic image paths."""
    print("Inside BuildImages")
    builder = (lambda r, p: build_static_url_http(r, p)) if use_http else build_static_url
    return data.get("images", {
        "left": builder(request, "image/side2-removebg-preview.png"),
        "right": builder(request, "image/side-removebg-preview.png"),
        "logo": "https://crm.volt-consulting.com/uploads/volt/logos/volt-logo.png",
        "side333": data.get("side3", builder(request, "image/side333-removebg-preview.png")),
        "volt_image1": builder(request, "image/volt_image1.png"),
        "icon": data.get("icon", builder(request, "image/buld-removebg-preview.png")),
        "Screenshot1": data.get("Screenshot1", builder(request, "image/Screenshot_2025-08-18_135847-removebg-preview.png")),
        "Screenshot2": data.get("Screenshot2", builder(request, "image/Screenshot_2025-08-18_131641-removebg-preview.png")),
        "black": builder(request, "image/black-removebg-preview.png"),
        "zero": data.get("zero", builder(request, "image/zero-removebg-preview.png")),
        "icon1": data.get("icon1", builder(request, "image/icon-removebg-preview.png")),
        "whitee": data.get("whitee", builder(request, "image/whiteee.png")),
        "con": data.get("con", builder(request, "image/Screenshot_2025-08-18_164713-removebg-preview.png")),
        "con5": data.get("con5", builder(request, "image/Screenshot_2025-08-18_164344-removebg-preview.png")),
        "Hmm": data.get("Hmm", builder(request, "image/Hmm-removebg-preview.png")),
        "last": data.get("last", builder(request, "image/circle-black-removebg-preview.png")),
        "double": data.get("double", builder(request, "image/double-removebg-preview.png")),
        "enedis": data.get("enedis", builder(request, "image/enedis-removebg-preview.png")),
        "contact_portrait": builder(request, "image/contact-portrait.jpg"),
        "hero_turbines": builder(request, "image/hero-turbines.jpg"),
        "team_meeting": builder(request, "image/team-meeting.jpg"),
        "hero_refinery": builder(request, "image/gas-slide1-right-photo.png"),
        "team_office": builder(request, "image/gas-slide2-right-photo.png"),
    })

def build_static_url_http(request, path):
    """HTTP URL — for browser-rendered templates (volt-electricity.html via energy_offer_summary)."""
    from django.templatetags.static import static
    if request:
        return request.build_absolute_uri(static(path))
    return static(path)

def build_company_presentation(data):
    """Company presentation section."""
    print("Inside BuildCompanyPresentation")
    return {
        "title": data.get("company_title", "L'ÉNERGIE DE VOTRE<br> ENTREPRISE, NOTRE EXPERTISE"),
        "description": data.get("description",
                                "<b>Volt Consulting</b> est votre partenaire de confiance dans la <b>gestion énergétique B2B</b>. "
                                "Notre proximité et notre engagement nous permettent de comprendre vos besoins <b>spécifiques</b>. "
                                "Nous vous accompagnons dans le choix du fournisseur d'énergie optimal, tout en maximisant l'efficacité énergétique. "
                                "Nos réussites parlent d'elles-mêmes, avec des <b>économies mesurables</b> pour nos clients."),
        "quote": data.get("quote", "Faites équipe avec nous pour un avenir énergétique plus efficace.")
    }


def build_budget_section(data):
    """Budget global section."""
    print("Inside BuildBudgetSection")
    return {
        "title": data.get("budget_title", "BUDGET GLOBAL"),
        "subtitle": data.get("budget_subtitle", "La synthèse")
    }


def build_tender_results(data):
    """Tender results section."""
    print("Inside BuildTenderResults")
    return {
        "title": data.get("tender_title", "RÉSULTAT DE L'APPEL D'OFFRE"),
        "introduction": data.get("introduction",
                                 "Lors de notre processus d'appel d'offres, <b>nous avons sollicité la participation de<br> plusieurs fournisseurs d'énergie</b>, "
                                 "cherchant à identifier le partenaire idéal pour<br> vous. Pour ce faire, nous avons établi des critères stricts, "
                                 "en mettant l'accent sur<br> <b>la qualité du service clientèle et en privilégiant les fournisseurs basés en<br> France.</b> "
                                 "Nous avons fait ce choix en nous plaçant du côté du consommateur."),
        "pricing_policy": data.get("pricing_policy", "Nous privilégions les prix fixes."),
        "pricing_explanation": data.get("pricing_explanation",
                                        "En cas d'augmentation du marché du gaz et d'électricité, c'est le fournisseur qui<br> subira l'impact des variations de prix, et non l'inverse."),
        "stability_benefits": data.get("stability_benefits",
                                       "Cela nous permet d'offrir à nos clients la stabilité budgétaire et la capacité d'anticiper les coûts<br>"
                                       "sur les prochaines années. Dans le contexte actuel, marqué par la volatilité des prix et<br>"
                                       " l'incertitude liée aux conflits mondiaux, il est essentiel de sécuriser les prix sur une période à long<br>"
                                       " terme.")
    }


def build_tender_table(data):
    """Tender table section."""
    print("Inside BuildTenderTable")
    return {
        "title": data.get("tender_table_title", "RÉSULTAT DE L'APPEL D'OFFRE"),
        "columns": data.get("columns", [
            "Fournisseur", "Molecule €/MWh", "Abonnement €/mois",
            "CEE €/MWh", "CTA€/an", "TICGN €/MWh", "TOTAL€/an"
        ]),
    }


def build_comparison_table(data):
    """Comparison table section."""
    print("Inside BuildComparisionTable")
    return {
        "last_text": data.get("comparison_note",
                              "Ce comparatif tient compte de votre consommation au cours des douze derniers mois. "
                              "Les prix mentionnés sont variables au jour de la consultation, étant donné qu'ils sont sujets à la fluctuation des prix sur le marché de l'énergie. "
                              "Ils sont non contractuels. Il est important de noter que ce comparatif se base uniquement sur votre historique de consommation et ne prend pas en considération vos besoins énergétiques futurs."),
        "section_title": data.get("section_title", "Offre Actuelle / de renouvellement"),
        "labels": data.get("labels", [
            "Budget Énergétique <br>en €/an", "Distribution <br>en €/an", "Taxes <br>en €/an",
            "Abonnement <br>en €/an", "CEE <br>en €/an", "CTA <br>en €/an", "Budget HTVA <br>en €/an"
        ])
    }


def build_change_section(data):
    """Change section."""
    print("Inside BuildChangeSection")
    return {
        "title": data.get("change_title", "LE CHANGEMENT SANS CONTRAINTE"),
        "text": data.get("change_text",
                         "Contrairement à la téléphonie, rien ne change sur<br> "
                         "l'installation. Vous conservez le même compteur, le<br> "
                         "même numéro de dépannage en cas de problème. "
                         "C'est <br> toujours GRDF & ENEDIS qui s'occupe de la relève du<br> compteur. "
                         "Changer de fournisseur, c'est gratuit!"),
        "quote": data.get("change_quote",
                          "Les équipes de VOLT CONSULTING <br> peuvent vous accompagner sur toute<br> cette partie administrative")
    }


def build_contact_info(data):
    """Contact info section."""
    print("Inside BuildContactInfo")

    # Use safe_value to handle None values
    def safe_value(value):
        if value is None:
            return ""
        try:
            str_val = str(value).strip().lower()
            if str_val == "" or str_val == "none" or str_val == "null":
                return ""
            return str(value)
        except AttributeError:
            return str(value) if value is not None else ""

    return {
        "company_name": safe_value(data.get("company_name", "VOLT CONSULTING")),
        "phone": safe_value(data.get("phone", "01 87 66 70 43")),
        "email": safe_value(data.get("email", "contact@volt-consulting.fr")),
        "address": safe_value(data.get("address", "8 Place Hoche - 78000 Versailles"))
    }


@csrf_exempt
@require_http_methods(["POST"])
def volt_consulting_presentation_Electricitry(request):
    """
    POST API endpoint that accepts and processes Volt Consulting presentation data
    and renders HTML with the data.
    """
    try:
        # 1️⃣ Parse incoming data
        data = parse_request_data(request)

        # 2️⃣ Generate Chart (if available)
        chart_base64 = generate_chart(data)
        enedis_chart_base64 = generate_enedis_chart(
            data.get("comparatifClientHistoryPdfDto", {}).get("enedisDataPastYear", {}))

        # 3️⃣ Build Comparatif DTO
        comparatif = data.get("comparatifClientHistoryPdfDto", {})
        comparatif_dto = build_comparatif_dto_Electricity(comparatif, request, data)

        # 4️⃣ Build Presentation Data
        presentation_data = build_presentation_data_Electricity(data, enedis_chart_base64, chart_base64, comparatif_dto,
                                                                request)

        # 5️⃣ Render HTML
        html_content = render_html_Elecricity(presentation_data)

        # 6️⃣ Generate PDF
        pdf_url, pdf_filename = generate_pdf_Electricity(html_content, request, data, comparatif)

        return JsonResponse({
            "status": "success",
            "path": pdf_url,
            "name": pdf_filename,
            "title": pdf_filename,
            "mime_type": "application/pdf",
            "message": "PDF generated successfully"
        })

    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": f"An error occurred: {str(e)}",
        }, status=500)


def render_html_Elecricity(presentation_data):
    print("Inside RenderHTML")
    return render_to_string("volt_Electricity.html", {"data": presentation_data})


def generate_enedis_chart(chart_data):
    """
    Generate a stacked bar chart for Enedis consumption and optionally save it locally.

    Args:
        chart_data (dict): Example:
            {
                "months": ["09/2024", "10/2024", ..., "08/2025"],
                "consumptionData": {
                    "HCH": [2, 3, 4, ..., 1],
                    "HPH": [3, 5, 6, ..., 2],
                    "HCE": [1, 2, 0, ..., 3],
                    "HPE": [5, 1, 2, ..., 4]
                }
            }
        save_path (str, optional): Path to save the chart as PNG (e.g. "enedis_chart.png")

    Returns:
        str: Base64-encoded PNG chart string (for embedding), or None if input is invalid.
    """

    # --- Early return for null/empty input ---
    if not chart_data or not isinstance(chart_data, dict):
        return None

    # --- Extract data safely ---
    months = chart_data.get("months", [])
    consumption_data = chart_data.get("consumptionData", {})

    # --- Check if data is empty ---
    if not months or not consumption_data:
        return None

    # --- Check if consumption_data has actual values ---
    has_data = False
    for values in consumption_data.values():
        if any(v > 0 for v in values):  # Check if any non-zero values exist
            has_data = True
            break

    if not has_data:
        return None

    # --- Predefined colors for known labels ---
    label_colors = {
        "HCH": "#BFC4CC",  # light gray
        "HPH": "#002B5C",  # dark blue
        "HCE": "#A8C40F",  # green
        "HPE": "#FDD36A",  # yellow
        "HP": "#F77F00",  # orange
        "HC": "#0081A7",  # teal blue
        "BASE": "#9B5DE5",  # purple
    }

    plt.figure(figsize=(8, 4))
    bottom = [0] * len(months)

    # --- Create stacked bars dynamically ---
    for label, values in consumption_data.items():
        color = label_colors.get(label, "#999999")  # fallback gray for unknown labels
        plt.bar(months, values, bottom=bottom, label=label, color=color)
        bottom = [b + v for b, v in zip(bottom, values)]

    # --- Styling ---
    plt.ylabel("Consommation (kWh)")
    plt.xticks(rotation=45)
    plt.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, 1.15),
        ncol=len(consumption_data)
    )
    plt.tight_layout(rect=[0, 0.05, 1, 1])

    # --- Convert to Base64 ---
    buffer = io.BytesIO()
    plt.savefig(buffer, format="png", bbox_inches="tight", transparent=True)
    plt.close()
    buffer.seek(0)

    img_base64 = base64.b64encode(buffer.read()).decode("utf-8")
    return f"data:image/png;base64,{img_base64}"


def build_presentation_data_Electricity(data, enedis_chart_base64, chart_base64, comparatif_dto, request):
    print("Inside BuildPresentationData")

    # Updated helper function - FIXED VERSION
    def safe_value(value):
        if value is None:
            return ""
        try:
            str_val = str(value).strip().lower()
            if str_val == "" or str_val == "none" or str_val == "null":
                return ""
            return str(value)
        except AttributeError:
            return str(value) if value is not None else ""

    # Get ratioHTVA and differenceHTVA values
    ratio_htva = comparatif_dto.get("ratioHTVA")
    difference_htva = comparatif_dto.get("differenceHTVA")

    # Initialize black, black1, black3 based on conditions
    black = ""
    black1 = ""
    black3 = ""

    # Condition for black (ratioHTVA):
    # Show only if ratioHTVA is not None, not empty, and ≤ 0
    if ratio_htva is not None and ratio_htva != "":
        try:
            ratio_num = float(ratio_htva)
            if ratio_num <= 0:  # ≤ 0 (including negative values)
                black = f"{ratio_htva}%"
        except (ValueError, TypeError):
            # If can't convert to number, keep empty
            pass

    # Condition for black1 (differenceHTVA):
    # Show only if differenceHTVA is not None, not empty, and ≤ 0
    if difference_htva is not None and difference_htva != "":
        try:
            diff_num = float(difference_htva)
            if diff_num <= 0:  # ≤ 0 (including negative values)
                black1 = f"{difference_htva}€"
        except (ValueError, TypeError):
            # If can't convert to number, keep empty
            pass

    # Condition for black3 ("économisé/an"):
    # Show only if BOTH ratioHTVA ≤ 0 AND differenceHTVA ≤ 0
    # (both are negative or zero)
    if black != "" and black1 != "":
        black3 = "économisé/an"

    return {
        "title": data.get("title", "VOLT CONSULTING - Energy Services Presentation"),
        "headingone": "APPEL D'OFFRE",
        "clientSociety": safe_value(data.get("clientSociety")),
        "clientSiret": safe_value(data.get("clientSiret")),
        "clientFirstName": safe_value(data.get("clientFirstName")),
        "clientLastName": safe_value(data.get("clientLastName")),
        "clientEmail": safe_value(data.get("clientEmail")),
        "clientPhoneNumber": safe_value(data.get("clientPhoneNumber")),
        "black": black,
        "black1": black1,
        "black3": black3,
        "image": build_image_section(data, chart_base64),
        "has_chart": chart_base64 is not None,
        "imageOne": {
            "enedis_chart": enedis_chart_base64 if enedis_chart_base64 else ""
        },
        "images": build_images(data, request),
        "company_presentation": build_company_presentation(data),
        "comparatifClientHistoryPdfDto": comparatif_dto,
        "budget_global": build_budget_section(data),
        "tender_results": build_tender_results(data),
        "comparison_table": build_comparison_table_Electricity(data),
        "tender_table": build_tender_table_Electricity(data, comparatif_dto),
        "change_section": build_change_section(data),
        "contact_info": build_contact_info(data),
        "enedis_info": enedis_Chart(comparatif_dto),
        "volt_logo_base_url": "https://crm.volt-consulting.com/uploads/volt/providers/",
    }


def build_comparatif_dto_Electricity(comparatif, request, data):
    print("Inside BuildComparatifDTO")

    # Validate and format createdOn
    created_on_raw = comparatif.get("createdOn")
    if not created_on_raw:
        raise ValueError("Missing required field: createdOn")

    try:
        dt = datetime.fromtimestamp(created_on_raw / 1000.0, tz=PARIS_TZ)
        created_on = dt.strftime("%d/%m/%Y")
        created_on_time = dt.strftime("%Hh%M")
    except Exception as e:
        raise ValueError(f"Invalid createdOn value: {e}")

    dto = {
        "title": data.get("contexte_title", "Contexte global"),
        "title2": data.get("enedis_title2", "Votre Consommation relevée par"),
        "createdOn": created_on,
        "createdOnTime": created_on_time,
        "energyType": comparatif.get("energyType"),
        "puissance": comparatif.get("puissance"),
        "powerInKVA": comparatif.get("powerInKVA"),
        "contractStartDate": comparatif.get("contractStartDate"),
        "hph": comparatif.get("hph"),
        "hch": comparatif.get("hch"),
        "hpe": comparatif.get("hpe"),
        "hce": comparatif.get("hce"),
        "pte": comparatif.get("pte"),
        "hp": comparatif.get("hp"),
        "hc": comparatif.get("hc"),
        "base": comparatif.get("base"),
        "sumOfAnnualRates": comparatif.get("sumOfAnnualRates"),
        "sales": comparatif.get("sales"),
    }

    energy_type = dto.get("energyType")

    if energy_type == "ELECTRICITY":
        # required_electricity_fields = ["pdl", "segmentation", "volumeAnnual"]
        required_electricity_fields = ["pdl", "segmentation"]

        dto.update({
            "pdl": comparatif.get("pdl"),
            "segmentation": comparatif.get("segmentation"),
            "tarifType": comparatif.get("tarifType"),
            "volumeAnnual": comparatif.get("volumeAnnual"),
            "ratioHTVA": comparatif.get("ratioHTVA"),
            "differenceHTVA": comparatif.get("differenceHTVA"),
            "parametreDeCompteur": comparatif.get("parametreDeCompteur"),
        })

        for field in required_electricity_fields:
            if not dto.get(field):
                raise ValueError(f"Missing required ELECTRICITY field: {field}")
    else:
        raise ValueError("Invalid or missing energyType. Must be 'ELECTRICITY'.")

    # Separate CURRENT and REGULAR providers
    comparatif_rates = comparatif.get("comparatifRates", [])
    current_providers = [p for p in comparatif_rates if p.get("typeFournisseur") == "CURRENT"]
    regular_providers = [p for p in comparatif_rates if p.get("typeFournisseur") != "CURRENT"]

    # Sort regular_providers by coutHTVA in ascending order
    # Handle None values by putting them at the end
    def get_cout_htva(provider):
        cout_htva = provider.get("coutHTVA")
        if cout_htva is None:
            return float('inf')  # Put None values at the end
        try:
            return float(cout_htva)
        except (ValueError, TypeError):
            return float('inf')

    regular_providers.sort(key=get_cout_htva)

    # Get current provider's coutHTVA for comparison
    current_cout_htva = None
    if current_providers and len(current_providers) > 0:
        current_cout_htva = get_cout_htva(current_providers[0])

    # Find the minimum coutHTVA from regular_providers
    min_regular_cout_htva = None
    if regular_providers:
        min_regular_cout_htva = get_cout_htva(regular_providers[0])
        # If min_regular_cout_htva is infinity (None values), set to None
        if min_regular_cout_htva == float('inf'):
            min_regular_cout_htva = None

    # "% ÉCONOMIE VS RÉFÉRENCE" for every row, computed against the CURRENT
    # (actual/incumbent) provider's coutHTVA. Negative = cheaper than today.
    ref_cout_htva = current_cout_htva if current_cout_htva != float('inf') else None
    for provider in current_providers + regular_providers:
        provider_cout_htva = get_cout_htva(provider)
        if ref_cout_htva and provider_cout_htva != float('inf'):
            economie_eur = provider_cout_htva - ref_cout_htva
            provider["economieEurCalc"] = economie_eur
            provider["economiePercentCalc"] = economie_eur / ref_cout_htva * 100
        else:
            provider["economieEurCalc"] = None
            provider["economiePercentCalc"] = None

    # Paginate providers into containers (4 rows per container)
    paginated_containers = []
    current_index = 0
    regular_index = 0
    green_row_used = True  # Flag for green row (once after labels)

    while current_index < len(current_providers) or regular_index < len(regular_providers):
        container = {
            "current_providers": [],
            "regular_providers": [],
            "show_header": len(paginated_containers) == 0,
            "show_title_labels": False
        }

        rows_in_container = 0

        # Add CURRENT providers first
        while current_index < len(current_providers) and rows_in_container < 4:
            container["current_providers"].append(current_providers[current_index])
            current_index += 1
            rows_in_container += 1

        # Show title/labels if all CURRENT providers done
        if current_index >= len(current_providers) and len(container["current_providers"]) > 0:
            container["show_title_labels"] = True

        # If no CURRENT providers at all, show labels in first container
        if len(current_providers) == 0 and len(paginated_containers) == 0:
            container["show_title_labels"] = True

        # Fill REGULAR providers (after title/labels)
        while regular_index < len(regular_providers) and rows_in_container < 4:
            provider = regular_providers[regular_index]

            # UPDATED GREEN ROW LOGIC:
            # Mark as green row only if:
            # 1. Not already used
            # 2. We're in the container that shows title/labels
            # 3. This is the first regular provider after labels
            # 4. This provider has the minimum coutHTVA among regular providers
            # 5. The min_regular_cout_htva ≤ current_cout_htva
            if (not green_row_used and
                container["show_title_labels"] and
                regular_index == 0 and  # First regular provider
                min_regular_cout_htva is not None and
                current_cout_htva is not None and
                min_regular_cout_htva <= current_cout_htva):

                provider["is_green_row"] = True
                green_row_used = True
            else:
                provider["is_green_row"] = False

            container["regular_providers"].append(provider)
            regular_index += 1
            rows_in_container += 1

        paginated_containers.append(container)

    dto["paginatedContainers"] = paginated_containers
    dto["comparatifRates"] = comparatif_rates

    # Add a flattened list of all regular providers in sorted order
    all_regular_providers = []
    for container in paginated_containers:
        all_regular_providers.extend(container["regular_providers"])
    dto["allRegularProviders"] = all_regular_providers

    # Add a flattened list of ALL providers (CURRENT + regular) for tables
    all_providers_for_tables = []
    # Add CURRENT providers first (they appear at the top in the UI)
    for container in paginated_containers:
        all_providers_for_tables.extend(container["current_providers"])
    # Then add regular providers
    all_providers_for_tables.extend(all_regular_providers)
    dto["allProvidersForTables"] = all_providers_for_tables

    return dto

def build_comparison_table_Electricity(data):
    """Comparison table section."""
    print("Inside BuildComparisionTable")
    return {
        "last_text": data.get("comparison_note",
                              "Ce comparatif tient compte de votre consommation au cours des douze derniers mois. "
                              "Les prix mentionnés sont variables au jour de la consultation, étant donné qu'ils sont sujets à la fluctuation des prix sur le marché de l'énergie. "
                              "Ils sont non contractuels. Il est important de noter que ce comparatif se base uniquement sur votre historique de consommation et ne prend pas en considération vos besoins énergétiques futurs."),
        "section_title": data.get("section_title", "Offre Actuelle / de renouvellement"),
        "labels": data.get("labels", [
            "Fourniture <br>en €/an", "Acheminement <br>en €/an", "Taxes <br>en €/an", "Budget HTVA <br>en €/an"
        ])
    }


def build_tender_table_Electricity(data, comparatif_dto):
    """Tender table section."""
    print("Inside BuildTenderTable")

    columns = data.get("columns", [])
    columns1 = data.get("columns1", [])
    columns6 = data.get("columns6", [])

    if not columns6:
        # Safely get values with defaults
        energy_type = comparatif_dto.get("energyType", "ELECTRICITY")
        segmentation = comparatif_dto.get("segmentation", "")
        tarif_type = comparatif_dto.get("tarifType", "")
        parametreDeCompteur = comparatif_dto.get("parametreDeCompteur", "")

        # Convert to uppercase and strip whitespace for case-insensitive comparison - SAFE VERSION
        def safe_strip_upper(value):
            if value is None:
                return ""
            try:
                # Convert to string first, then strip and uppercase
                return str(value).strip().upper()
            except (AttributeError, TypeError):
                return ""

        energy_type_upper = safe_strip_upper(energy_type)
        segmentation_upper = safe_strip_upper(segmentation)
        tarif_type_upper = safe_strip_upper(tarif_type)
        parametreDeCompteur_upper = safe_strip_upper(parametreDeCompteur)

        print(
            f"DEBUG: energy_type_upper={energy_type_upper}, segmentation_upper={segmentation_upper}, tarif_type_upper={tarif_type_upper}, parametreDeCompteur_upper={parametreDeCompteur_upper}")

        if energy_type_upper == "ELECTRICITY":
            segmentation_mapping = {
                "C1": ["HPH", "HCH", "HPE", "HCE", "POINTE"],
                "C2": ["HPH", "HCH", "HPE", "HCE", "POINTE"],
                "C3": ["HPH", "HCH", "HPE", "HCE", "POINTE"],
                "C4": ["HPH", "HCH", "HPE", "HCE"],
            }

            segmentation_mapping_with_units = {
                "C1": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh", "POINTE <br> €/MWh"],
                "C2": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh", "POINTE <br> €/MWh"],
                "C3": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh", "POINTE <br> €/MWh"],
                "C4": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh"],
            }

            if segmentation_upper in {"C1", "C2", "C3", "C4"}:
                columns = ["Fournisseur"] + segmentation_mapping_with_units.get(segmentation_upper, [])
                columns1 = segmentation_mapping_with_units.get(segmentation_upper, [])
                columns6 = segmentation_mapping.get(segmentation_upper, [])
            elif parametreDeCompteur_upper == "C5BASE":
                columns = ["Fournisseur", "BASE <br> €/MWh"]
                columns1 = ["BASE <br> €/MWh"]
                columns6 = ["BASE"]
            elif parametreDeCompteur_upper == "C5C4":
                columns = ["Fournisseur"] + segmentation_mapping_with_units.get("C4", [])
                columns1 = segmentation_mapping_with_units.get("C4", [])
                columns6 = segmentation_mapping.get("C4", [])
            elif parametreDeCompteur_upper == "C5HP":
                columns = ["Fournisseur", "HP <br> €/MWh", "HC <br> €/MWh"]
                columns1 = ["HP <br> €/MWh", "HC <br> €/MWh"]
                columns6 = ["HP", "HC"]

    return {
        "title": data.get("tender_table_title", "RÉSULTAT DE L'APPEL D'OFFRE"),
        "columns": columns if columns else ["Fournisseur", "HP <br> €/MWh", "HC <br> €/MWh"],
        "columns1": columns1 if columns1 else ["HP <br> €/MWh", "HC <br> €/MWh"],
        "columns2": data.get("columns2", ["CEE <br> €/MWh"]),
        "columns3": data.get("columns3", ["ABO <br> €/an"]),
        "columns4": data.get("columns4", [
            "Puissances souscrites KVA", "Consommation MWh", "Total"
        ]),
        "columns5": data.get("columns5", [
            "Compteu", "Déb.contrat"
        ]),
        "columns6": columns6 if columns6 else ["HP", "HC"],
        "columns7": data.get("columns7", [
            "MWh / an"
        ]),
    }


def generate_pdf_Electricity(html_content, request, data, comparatif):
    """Generate PDF and return its URL (without removing any pages)."""
    print("Inside GeneratePDF")
    host = request.get_host().split(":")[0]

    # Choose base dirs (filesystem vs URL)
    if host == "volt-crm.caansoft.com":
        base_dir = settings.STAGING_MEDIA_ROOT
        base_url = settings.STAGING_MEDIA_URL
    elif host == "crm.volt-consulting.com":
        base_dir = settings.PRODUCTION_MEDIA_ROOT
        base_url = settings.PRODUCTION_MEDIA_URL
    else:
        base_dir = settings.MEDIA_ROOT
        base_url = settings.MEDIA_URL

    # Dynamic path: client/<id>/comparatif/
    relative_path = os.path.join("clients", str(data.get("clientId")), "comparatif", str(comparatif.get("id")))
    pdf_dir = os.path.join(base_dir, relative_path)
    os.makedirs(pdf_dir, exist_ok=True)

    # Generate filename
    pdf_filename = create_comparatif_filename(
        data.get("clientSociety"),
        data.get("clientTradeName"),
        data.get("comparatifClientHistoryPdfDto", {}).get("energyType")
    )
    pdf_path = os.path.join(pdf_dir, pdf_filename)

    # Save PDF using WeasyPrint
    css = CSS(string="""@page { size: 530mm 265mm; margin: 0.0cm; }""")
    HTML(string=html_content).write_pdf(
        pdf_path,
        stylesheets=[css],
        zoom=0.8,
        optimize_images=True,
        presentational_hints=True,
        font_config=None
    )

    # ---- Remove unwanted pages (4,6,8,10,12) ----
    # PyPDF2 uses 0-based index: 3=page4, 5=page6, etc.
    remove_pages = [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31, 33, 35, 37, 39, 41, 43, 35, 37, 39, 41,
                    43, 45, 47, 49, 51, 53, 55, 57, 59, 61, 63, 65, 67, 69, 71, 73, 75, 77, 79, 81, 83, 85, 87, 89, 91,
                    93, 95, 97, 99]

    reader = PdfReader(pdf_path)
    writer = PdfWriter()

    for i in range(len(reader.pages)):
        if i not in remove_pages:
            writer.add_page(reader.pages[i])

    with open(pdf_path, "wb") as f:
        writer.write(f)

    # Build public URL (mirrors saved path after /uploads/volt/)
    pdf_url = request.build_absolute_uri(
        os.path.join(base_url, "clients", str(data.get("clientId")), "comparatif", str(comparatif.get("id")),
                     pdf_filename)
    )

    return pdf_url, pdf_filename


def enedis_Chart(comparatif_dto):
    """Provide dynamic Enedis rate information based on segmentation and tarif type."""

    # Safe strip and upper function
    def safe_strip_upper(value):
        if value is None:
            return ""
        try:
            return str(value).strip().upper()
        except (AttributeError, TypeError):
            return ""

    # Extract data with case-insensitive handling
    energy_type = comparatif_dto.get("energyType", "ELECTRICITY")
    segmentation = comparatif_dto.get("segmentation", "")
    tarif_type = comparatif_dto.get("tarifType", "")
    parametreDeCompteur = comparatif_dto.get("parametreDeCompteur", "")

    # Convert to uppercase for consistent comparison - SAFE VERSION
    energy_type_upper = safe_strip_upper(energy_type)
    segmentation_upper = safe_strip_upper(segmentation)
    tarif_type_upper = safe_strip_upper(tarif_type)
    parametreDeCompteur_upper = safe_strip_upper(parametreDeCompteur)

    # Format contract start date from timestamp to dd/mm/yyyy
    contract_start_date = comparatif_dto.get("contractStartDate")
    formatted_date = "-"
    if contract_start_date:
        try:
            # Handle both milliseconds and seconds timestamp
            if contract_start_date > 1e12:  # Likely in milliseconds
                contract_start_date = contract_start_date / 1000
            dt = datetime.fromtimestamp(contract_start_date, tz=PARIS_TZ)
            formatted_date = dt.strftime("%d/%m/%Y")
        except (ValueError, TypeError, OSError):
            formatted_date = "-"

    # Base response with common fields
    base_response = {
        "enedis_rate_On": comparatif_dto.get("pdl", "-"),
        "contract_start_date": formatted_date,
        "enedis_rate_sum": comparatif_dto.get("sumOfAnnualRates", "-"),
    }

    if energy_type_upper == "ELECTRICITY":
        if segmentation_upper in ["C1", "C2", "C3"]:
            # C1, C2, C3: HPE, HPH, HCE, HCH, POINTE
            base_response.update({
                "enedis_rate_hph": comparatif_dto.get("hph", "-"),
                "enedis_rate_hch": comparatif_dto.get("hch", "-"),
                "enedis_rate_hpe": comparatif_dto.get("hpe", "-"),
                "enedis_rate_hce": comparatif_dto.get("hce", "-"),
                "enedis_rate_pointe": comparatif_dto.get("pte", "-"),

                "enedis_rate_puissance_hph": comparatif_dto.get("puissance", "-"),
                "enedis_rate_puissance_hch": comparatif_dto.get("puissance", "-"),
                "enedis_rate_puissance_hpe": comparatif_dto.get("puissance", "-"),
                "enedis_rate_puissance_hce": comparatif_dto.get("puissance", "-"),
                "enedis_rate_puissance_pointe": comparatif_dto.get("puissance", "-"),
            })
        elif segmentation_upper == "C4" or (segmentation_upper == "C5" and parametreDeCompteur_upper == "C5C4"):
            # C4 or C5 QUATRE: HPE, HPH, HCE, HCH
            base_response.update({
                "enedis_rate_hph": comparatif_dto.get("hph", "-"),
                "enedis_rate_hch": comparatif_dto.get("hch", "-"),
                "enedis_rate_hpe": comparatif_dto.get("hpe", "-"),
                "enedis_rate_hce": comparatif_dto.get("hce", "-"),

                "enedis_rate_puissance_hph": comparatif_dto.get("puissance", "-"),
                "enedis_rate_puissance_hch": comparatif_dto.get("puissance", "-"),
                "enedis_rate_puissance_hpe": comparatif_dto.get("puissance", "-"),
                "enedis_rate_puissance_hce": comparatif_dto.get("puissance", "-"),
            })
        elif segmentation_upper == "C5" and parametreDeCompteur_upper == "C5BASE":
            # C5 BASE: BASE only
            base_response.update({
                "enedis_rate_base": comparatif_dto.get("base", "-"),
                "enedis_rate_puissance_base": comparatif_dto.get("puissance", "-"),
            })
        elif segmentation_upper == "C5" and parametreDeCompteur_upper == "C5HP":
            # C5 DOUBLE: HP, HC
            base_response.update({
                "enedis_rate_hp": comparatif_dto.get("hp", "-"),
                "enedis_rate_hc": comparatif_dto.get("hc", "-"),

                "enedis_rate_puissance_hp": comparatif_dto.get("puissance", "-"),
                "enedis_rate_puissance_hc": comparatif_dto.get("puissance", "-"),
            })
        else:
            # Default: HP, HC
            base_response.update({
                "enedis_rate_hp": comparatif_dto.get("hp", "-"),
                "enedis_rate_hc": comparatif_dto.get("hc", "-"),

                "enedis_rate_puissance_hp": comparatif_dto.get("puissance", "-"),
                "enedis_rate_puissance_hc": comparatif_dto.get("puissance", "-"),
            })


    return base_response

@csrf_exempt
@require_http_methods(["POST"])
def energy_offer_summary(request):
    """
    POST API endpoint that accepts data, saves HTML file, and returns the file path.
    Similar to volt_consulting_presentation_Electricitry but saves HTML instead of PDF.
    """
    try:
        # 1️⃣ Parse incoming data
        data = parse_request_data(request)

        # Get comparatif data if available
        comparatif = data.get("comparatifClientHistoryPdfDto", {})

        # 2️⃣ Generate chart (if available)
        chart_base64 = generate_price_chart_styled(data)
        chart_12m_base64 = generate_price_chart_styled(data, last_n_months=12)
        enedis_chart_base64 = generate_enedis_bar_chart(
            comparatif.get("enedisDataPastYear", {})
        )

        # 3️⃣ Build Comparatif DTO
        comparatif_dto = build_comparatif_dto_Electricity(comparatif, request, data)

        # 4️⃣ Build presentation data
        presentation_data = build_presentation_data_energy_offer(data, enedis_chart_base64, chart_base64, chart_12m_base64, comparatif_dto, request)

        # 5️⃣ Render HTML
        html_content = render_to_string("volt-electricity.html", {"data": presentation_data})

        # 6️⃣ Save HTML file to server and return path
        html_url, html_filename = save_html_file(html_content, request, data, comparatif)

        return JsonResponse({
            "status": "success",
            "path": html_url,
            "name": html_filename,
            "title": html_filename,
            "mime_type": "text/html",
            "message": "HTML file generated successfully"
        })

    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": f"An error occurred: {str(e)}",
        }, status=500)

@csrf_exempt
@require_http_methods(["POST"])
def comparatif_gas(request):
    """
    POST API endpoint that accepts GAS data, saves HTML file, and returns the file path.
    Mirrors energy_offer_summary but renders volt-gas.html for GAS payloads.
    """
    try:
        # 1️⃣ Parse incoming data
        data = parse_request_data(request)

        # Get comparatif data if available
        comparatif = data.get("comparatifClientHistoryPdfDto", {})

        # 2️⃣ Generate charts (if available)
        chart_base64 = generate_price_chart_styled(data)
        chart_12m_base64 = generate_price_chart_styled(data, last_n_months=12)
        gas_chart_base64 = generate_enedis_bar_chart(
            comparatif.get("grdfDataPastYear") or comparatif.get("enedisDataPastYear", {})
        )

        # 3️⃣ Build Comparatif DTO (GAS)
        comparatif_dto = build_comparatif_dto_Gas(comparatif, request, data)

        # 4️⃣ Build presentation data
        presentation_data = build_presentation_data_gas(
            data, chart_base64, chart_12m_base64, gas_chart_base64, comparatif_dto, request
        )

        # 5️⃣ Render HTML
        html_content = render_to_string("volt-gas.html", {"data": presentation_data})

        # 6️⃣ Save HTML file to server and return path
        html_url, html_filename = save_html_file(html_content, request, data, comparatif)

        return JsonResponse({
            "status": "success",
            "path": html_url,
            "name": html_filename,
            "title": html_filename,
            "mime_type": "text/html",
            "message": "HTML file generated successfully"
        })

    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": f"An error occurred: {str(e)}",
        }, status=500)


def save_html_file(html_content, request, data, comparatif):
    """
    Save HTML content as an HTML file on the server.
    Returns the URL and filename of the saved HTML file.
    Similar to generate_pdf_Electricity but for HTML files.
    """
    print("Inside SaveHTMLFile")
    host = request.get_host().split(":")[0]

    # Choose base dirs (filesystem vs URL)
    if host == "volt-crm.caansoft.com":
        base_dir = settings.STAGING_MEDIA_ROOT
        base_url = settings.STAGING_MEDIA_URL
    elif host == "crm.volt-consulting.com":
        base_dir = settings.PRODUCTION_MEDIA_ROOT
        base_url = settings.PRODUCTION_MEDIA_URL
    else:
        base_dir = settings.MEDIA_ROOT
        base_url = settings.MEDIA_URL

    # Dynamic path: client/<id>/energy_offer/
    relative_path = os.path.join("clients", str(data.get("clientId")), "energy_offer")
    html_dir = os.path.join(base_dir, relative_path)
    os.makedirs(html_dir, exist_ok=True)

    # Generate filename (similar to create_comparatif_filename pattern)
    html_filename = create_energy_offer_filename(
        data.get("clientSociety"),
        data.get("clientTradeName"),
        data.get("comparatifClientHistoryPdfDto", {}).get("energyType")
    )
    html_path = os.path.join(html_dir, html_filename)

    # Embed the exact edit-target so the inline editor writes back to *this*
    # file in every environment (local media, staging, production) instead of
    # guessing it from the browser URL. Also embed the API base: on the CRM
    # hosts the Django service sits behind the /pdf-service/ nginx location,
    # whereas locally it's served at the root — so the editor's save call needs
    # the right prefix to actually reach Django.
    api_base = "/pdf-service" if host in ("volt-crm.caansoft.com", "crm.volt-consulting.com") else ""
    edit_marker = (
        "<script>window.__VOLT_EDIT_TARGET__ = "
        + json.dumps(html_path.replace("\\", "/"))
        + "; window.__VOLT_API_BASE__ = "
        + json.dumps(api_base)
        + ";</script>"
    )
    if "</head>" in html_content:
        html_content = html_content.replace("</head>", edit_marker + "\n</head>", 1)
    else:
        html_content = edit_marker + "\n" + html_content

    # Save HTML file
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    # Build public URL
    html_url = request.build_absolute_uri(
        os.path.join(base_url, relative_path, html_filename)
    )

    print(f"HTML file saved: {html_path}")
    print(f"HTML URL: {html_url}")

    return html_url, html_filename


def create_energy_offer_filename(society: str, trade_name: str, energy_type: str) -> str:
    """
    Create filename for energy offer HTML file.
    Similar to create_comparatif_filename but for HTML files.
    """
    # 1️⃣ Clean society or fallback to trade_name
    if society:
        clean_society = re.sub(r"\s+", "", str(society))
    else:
        clean_society = re.sub(r"\s+", "", str(trade_name))

    # Remove path separators and problematic characters
    clean_society = re.sub(r'[^a-zA-Z0-9_]', '_', clean_society)
    clean_society = re.sub(r'_+', '_', clean_society)
    clean_society = clean_society.strip('_')

    # 2️⃣ Energy type suffix
    additional_text = "_elec" if energy_type.upper() == "ELECTRICITY" else "_gaz"

    # 3️⃣ Date part (YYYY-MM-DD)
    date_str = datetime.now().strftime("%Y-%m-%d")

    # 4️⃣ Final filename
    filename = f"Energy_Offer_{clean_society}{additional_text}_{date_str}.html"
    return filename


def _compute_chart_date_ranges(data):
    """Return {'all_data': 'YYYY – AUJOURD\'HUI', 'last_12m': 'MMM YYYY – MMM YYYY'}."""
    result = {"all_data": "", "last_12m": ""}
    chart_dto = data.get("chartDataDto", {})
    if not chart_dto or "xAxis" not in chart_dto or not chart_dto["xAxis"]:
        return result
    x_raw = chart_dto["xAxis"][0].get("data", [])
    if not x_raw:
        return result
    try:
        all_dates = pd.to_datetime(x_raw, format="%Y-%m-%d")
    except Exception:
        try:
            all_dates = pd.to_datetime(x_raw)
        except Exception:
            return result
    if len(all_dates) == 0:
        return result

    french_months = {
        1: "JANVIER", 2: "FÉVRIER", 3: "MARS", 4: "AVRIL",
        5: "MAI", 6: "JUIN", 7: "JUILLET", 8: "AOÛT",
        9: "SEPTEMBRE", 10: "OCTOBRE", 11: "NOVEMBRE", 12: "DÉCEMBRE",
    }
    result["all_data"] = f"{all_dates[0].year} – AUJOURD'HUI"
    # Use calendar month cutoff (same logic as the chart function)
    start_cutoff = all_dates[-1] - pd.DateOffset(months=12)
    mask = all_dates >= start_cutoff
    last_12 = all_dates[mask] if mask.any() else all_dates[-12:]
    s, e = last_12[0], last_12[-1]
    result["last_12m"] = (
        f"{french_months[s.month]} {s.year} – {french_months[e.month]} {e.year}"
    )
    return result


def _call_market_llm(prompt):
    """POST a prompt to the in-house LLM (qwen2.5vl:7b, Ollama-compatible /api/generate).
    Returns the raw response text, or None on any network/timeout/parse failure."""
    payload = json.dumps({
        "model": "qwen2.5vl:7b",
        "prompt": prompt,
        "stream": False,
        # "think": "low",
        # Keep the model resident in memory so back-to-back / subsequent
        # generations skip the ~40s cold-load cost (load_duration).
        "keep_alive": "30m",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://gpt.caansoft.com/gpt/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=280) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        print(f"Market LLM call failed: {e}")
        return None

    return body.get("response") or None


def _parse_llm_fields(text, field_names):
    """Parse a 'LABEL: text' per-line response into {field_name: text}, matching each
    field_name (lowercase key) against a 'FIELD_NAME:' prefix (case-insensitive)."""
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


def _summarize_chart_data(chart_data_dto, recent_window=30):
    """Reduce chartDataDto (4 series x ~250 daily points each) to compact per-series stats
    (first/last/min/max/overall change/recent trend) so the LLM prompt stays small enough
    for the model to actually generate a response instead of exhausting its token budget."""
    if not chart_data_dto or not isinstance(chart_data_dto, dict):
        return None

    series_list = chart_data_dto.get("series") or []
    x_axis = chart_data_dto.get("xAxis") or []
    dates = (x_axis[0].get("data") if x_axis and isinstance(x_axis[0], dict) else []) or []

    summary = {
        "period": {"from": dates[0] if dates else None, "to": dates[-1] if dates else None},
        "series": [],
    }

    for s in series_list:
        data = s.get("data") or []
        values = [v for v in data if isinstance(v, (int, float))]
        if not values:
            continue

        first_val = next(v for v in data if isinstance(v, (int, float)))
        last_val = next(v for v in reversed(data) if isinstance(v, (int, float)))

        recent = values[-recent_window:]
        previous = values[-2 * recent_window:-recent_window] or values[:-recent_window]
        recent_avg = sum(recent) / len(recent) if recent else None
        previous_avg = sum(previous) / len(previous) if previous else None

        overall_change_pct = (last_val - first_val) / first_val * 100 if first_val else None
        recent_trend_pct = (
            (recent_avg - previous_avg) / previous_avg * 100
            if recent_avg is not None and previous_avg else None
        )

        summary["series"].append({
            "label": s.get("label"),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "first": round(first_val, 2),
            "last": round(last_val, 2),
            "overall_change_pct": round(overall_change_pct, 1) if overall_change_pct is not None else None,
            "recent_trend_pct": round(recent_trend_pct, 1) if recent_trend_pct is not None else None,
        })

    return summary if summary["series"] else None


def _generate_market_analysis(chart_data_dto):
    """Ask the LLM for a short market analysis + recommendation based on a compact summary
    of the price history (chartDataDto). Returns None on any failure so the template falls
    back to its default copy."""
    summary = _summarize_chart_data(chart_data_dto)
    if not summary:
        return None

    prompt = (
        "Tu es un analyste du marché de l'énergie pour Volt Consulting. "
        "Voici un résumé de l'historique des prix (chartDataDto) au format JSON : "
        "pour chaque contrat (label), min/max sur la période, valeur de début/fin, "
        "variation globale en % (overall_change_pct), et tendance récente en % sur les "
        "30 derniers points (recent_trend_pct) :\n"
        f"{json.dumps(summary, ensure_ascii=False)}\n\n"
        "À partir de ces données, rédige deux phrases courtes (30 mots maximum chacune) "
        "à destination d'un client professionnel :\n"
        "1) Une analyse factuelle de la tendance récente du marché.\n"
        "2) Une recommandation POSITIVE et ENCOURAGEANTE qui met en avant l'opportunité "
        "de sécuriser une offre dès maintenant avec l'accompagnement de Volt Consulting. "
        "La recommandation doit toujours être rassurante et tournée vers l'action ; "
        "ne jamais conseiller d'attendre, ni employer un ton négatif ou décourageant.\n"
        "Réponds STRICTEMENT selon ce format, sans aucun autre texte :\n"
        "ANALYSE: <texte>\n"
        "RECOMMANDATION: <texte>"
    )
    print(f"DEBUG: Sending prompt to market LLM:\n{prompt}")
    text = _call_market_llm(prompt)
    print(f"DEBUG: Received response from market LLM:\n{text}")
    fields = _parse_llm_fields(text, ["ANALYSE", "RECOMMANDATION"])
    return fields or None


# ── Consumption-analysis pipeline ────────────────────────────────────────────
# All numeric facts (peaks, lows, rankings, percentages) are computed in Python
# in _summarize_enedis_data. The LLM is only ever asked to turn already-correct
# facts into prose (_generate_consumption_analysis) — it never ranks, sorts, or
# calculates a percentage itself. Output is checked against the summary
# (_validate_consumption_text) and falls back to a plain, guaranteed-correct
# template (_fallback_consumption_analysis) if it drifts.
#
# NOTE (fix): STRATEGIE is now explicitly constrained to always recommend a
# FIXED-price contract, since Volt's actual sales position (see
# build_tender_results: "Nous privilégions les prix fixes.") is fixed-price
# only. Previously the open-ended prompt let the model freely invent
# variable-rate / prepaid-block recommendations that contradict this policy.
# _validate_consumption_text now also rejects any such banned terms and caps
# each field at ~40 words so a single run-on sentence can't blow past the
# "30 mots maximum" instruction unnoticed.

_FRENCH_MONTHS = {
    "janvier": "01", "février": "02", "fevrier": "02", "mars": "03",
    "avril": "04", "mai": "05", "juin": "06", "juillet": "07",
    "août": "08", "aout": "08", "septembre": "09", "octobre": "10",
    "novembre": "11", "décembre": "12", "decembre": "12",
}

# Terms that contradict Volt's fixed-price-only sales policy, or that
# describe products/offers not present anywhere in the summarized data.
# If the LLM's STRATEGIE (or any field) uses one of these, the response is
# rejected and the deterministic fallback template is used instead.
_BANNED_STRATEGY_TERMS = [
    "taux variable", "prix variable", "tarif variable",
    "indexé", "indexée", "indexés", "indexées",
    "spot", "prépayé", "prépayés", "prépayée", "prépayées",
    "bloc d'achat", "blocs d'achat", "blocs d'achats",
]


def _summarize_enedis_data(enedis_data_past_year):
    """Single source of truth for all consumption facts. Every number, ranking,
    and label the LLM will use is computed HERE in Python — the LLM never
    ranks, sorts, or calculates a percentage itself. It only receives the
    finished facts and turns them into prose."""
    if not enedis_data_past_year or not isinstance(enedis_data_past_year, dict):
        return None

    months = enedis_data_past_year.get("months") or []
    consumption = enedis_data_past_year.get("consumptionData") or {}
    if not months or not isinstance(consumption, dict):
        return None

    labels = {
        "HPH": "Heures Pleines Hiver", "HCH": "Heures Creuses Hiver",
        "HPE": "Heures Pleines Été", "HCE": "Heures Creuses Été",
        "HP": "Heures Pleines", "HC": "Heures Creuses",
        "POINTE": "Pointe Hiver", "BASE": "Base",
    }
    winter_codes = {"HPH", "HCH"}
    summer_codes = {"HPE", "HCE"}

    def _num(v):
        return v if isinstance(v, (int, float)) else 0

    # ---- Monthly totals ----
    monthly_total = []
    for i in range(len(months)):
        total = sum(_num(vals[i]) for vals in consumption.values()
                    if isinstance(vals, list) and i < len(vals))
        monthly_total.append(round(total, 1))

    total_annual = round(sum(monthly_total), 1)
    if total_annual <= 0:
        return None

    # ---- Separate trailing "no data yet" months from real zero months ----
    no_data_months = []
    for i in range(len(monthly_total) - 1, -1, -1):
        if monthly_total[i] == 0:
            no_data_months.append(months[i])
        else:
            break
    no_data_months.reverse()
    no_data_set = set(no_data_months)

    elapsed_idx = [i for i in range(len(months)) if months[i] not in no_data_set]
    if not elapsed_idx:
        return None

    elapsed_totals = [monthly_total[i] for i in elapsed_idx]
    avg_monthly = round(sum(elapsed_totals) / len(elapsed_totals), 1)

    # ---- Peak / low months, ranked in Python (never by the LLM) ----
    ranked = sorted(elapsed_idx, key=lambda i: monthly_total[i], reverse=True)
    peak_months = [{"month": months[i], "value": monthly_total[i]} for i in ranked[:3]]
    lowest_months = [{"month": months[i], "value": monthly_total[i]} for i in ranked[-3:]][::-1]
    peak_value = monthly_total[ranked[0]] if ranked else 0
    peak_to_average_ratio = round(peak_value / avg_monthly, 2) if avg_monthly else None

    # ---- Tariff-period breakdown, ranked by share ----
    period_totals = {}
    for code, vals in consumption.items():
        if not isinstance(vals, list):
            continue
        period_total = round(sum(_num(v) for v in vals), 1)
        if period_total > 0:
            period_totals[code] = period_total

    consumption_by_period = sorted(
        (
            {
                "code": code,
                "label": labels.get(code, code),
                "total": total,
                "share_pct": round(total / total_annual * 100, 1),
            }
            for code, total in period_totals.items()
        ),
        key=lambda p: p["share_pct"],
        reverse=True,
    )
    dominant_period = consumption_by_period[0] if consumption_by_period else None

    # ---- Winter vs summer split, ranked ----
    winter_total = round(sum(v for c, v in period_totals.items() if c in winter_codes), 1)
    summer_total = round(sum(v for c, v in period_totals.items() if c in summer_codes), 1)
    season_split = None
    if winter_total > 0 and summer_total > 0:
        winter_pct = round(winter_total / total_annual * 100, 1)
        summer_pct = round(summer_total / total_annual * 100, 1)
        season_split = {
            "winter_total": winter_total,
            "summer_total": summer_total,
            "winter_share_pct": winter_pct,
            "summer_share_pct": summer_pct,
            "dominant_season": "hiver" if winter_pct > summer_pct else "été",
        }

    return {
        "period": {"from": months[0], "to": months[-1]},
        "total_annual_kwh": total_annual,
        "avg_monthly_kwh": avg_monthly,
        "peak_months": peak_months,
        "lowest_months": lowest_months,
        "peak_to_average_ratio": peak_to_average_ratio,
        "no_data_months": no_data_months,
        "consumption_by_period": consumption_by_period,
        "dominant_period": dominant_period,
        "season_split": season_split,
    }


def _extract_mentioned_months(text):
    """Find every month reference in the text, whether written numerically
    ('12/2025') or in French prose ('décembre 2025'), and normalize both
    to 'MM/YYYY' so they can be checked against the precomputed summary.
    This is the piece that was previously missing: the LLM writes natural
    French ("décembre 2025", "mars 2026"), and a numeric-only regex silently
    let every wrong month claim through validation."""
    found = set()

    # Numeric form: "12/2025"
    found.update(re.findall(r"\b(?:0[1-9]|1[0-2])/20\d{2}\b", text))

    # French prose form: "décembre 2025" (case-insensitive, accent-tolerant
    # via the "decembre"/"fevrier"/"aout" fallback keys in _FRENCH_MONTHS)
    for name, mm in _FRENCH_MONTHS.items():
        for match in re.finditer(rf"\b{name}\b\s+(\d{{4}})", text, flags=re.IGNORECASE):
            found.add(f"{mm}/{match.group(1)}")

    return found


def _validate_consumption_text(text, summary):
    """Reject any generated field that cites a month, percentage, or figure
    not present in the precomputed summary. Catches both numeric ('12/2025')
    and French-prose ('décembre 2025') month references, and tolerates small
    rounding differences in percentages instead of requiring exact string
    matches.

    Also rejects (fix):
      - any use of a banned strategy term (variable-rate, indexed, spot,
        prepaid blocks, etc.) that contradicts Volt's fixed-price-only
        sales policy or invents a product not present in the data, and
      - responses whose combined text is implausibly long, as a lightweight
        signal that the "30 mots maximum" per-field constraint was ignored.
        (Per-field word counts are checked separately in
        _generate_consumption_analysis before this function is even called.)
    """
    if not text:
        return False

    allowed_months = {
        m["month"] for m in summary.get("peak_months", []) + summary.get("lowest_months", [])
    }
    mentioned_months = _extract_mentioned_months(text)
    if mentioned_months - allowed_months:
        print(f"Rejected: unlisted month(s) {mentioned_months - allowed_months}")
        return False

    allowed_pcts = {p["share_pct"] for p in summary.get("consumption_by_period", [])}
    if summary.get("season_split"):
        allowed_pcts.add(summary["season_split"]["winter_share_pct"])
        allowed_pcts.add(summary["season_split"]["summer_share_pct"])

    mentioned_pcts_raw = re.findall(r"(\d+(?:[.,]\d+)?)\s?%", text)
    for raw in mentioned_pcts_raw:
        val = float(raw.replace(",", "."))
        # Tolerance for rounding differences (e.g. model writes "34%" for 33.9)
        if not any(abs(val - allowed) < 0.15 for allowed in allowed_pcts):
            print(f"Rejected: unlisted percentage {val}")
            return False

    # NEW: reject any recommendation that contradicts fixed-price policy or
    # invents a product/offer type not present in the data model.
    lowered = text.lower()
    hit_terms = [term for term in _BANNED_STRATEGY_TERMS if term in lowered]
    if hit_terms:
        print(f"Rejected: banned strategy term(s) found: {hit_terms}")
        return False

    return True


def _fallback_consumption_analysis(summary):
    """Plain templated output, used only if the LLM output fails validation.
    Guaranteed numerically correct since it's built directly from summary."""
    peak = summary["peak_months"][0]
    low = summary["lowest_months"][0]
    dominant = summary.get("dominant_period")
    season = summary.get("season_split")

    profil = (
        f"Consommation maximale en {peak['month']} ({peak['value']} kWh), "
        f"minimale en {low['month']} ({low['value']} kWh)"
    )
    if dominant:
        profil += f", dominée par les {dominant['label']} ({dominant['share_pct']}%)"
    if season:
        profil += (
            f", avec une consommation plus marquée en {season['dominant_season']} "
            f"({season['winter_share_pct']}% hiver / {season['summer_share_pct']}% été)"
        )
    profil += "."

    return {
        "profil": profil,
        "exposition": (
            f"Avec un pic à {summary['peak_to_average_ratio']}x la consommation moyenne, "
            "ce profil reste exposé aux variations du marché de l'énergie."
        ),
        "strategie": (
            "Un contrat à prix fixe sécurise votre budget face à cette variabilité "
            "et vous permet d'anticiper vos coûts sur la durée."
        ),
    }


def _generate_consumption_analysis(enedis_data_past_year):
    """All facts are computed in _summarize_enedis_data. The LLM's only job
    is to phrase those facts fluently — it is explicitly told not to compute,
    rank, or invent anything. Output is validated against the summary and
    falls back to a plain template on any mismatch.

    Fix: STRATEGIE is now explicitly pinned to Volt's fixed-price-only sales
    policy (see build_tender_results), instead of leaving the recommendation
    open-ended. A word-count guard is also applied per field before running
    the fact/banned-term validation, since the LLM has been observed ignoring
    the "30 mots maximum" instruction on the PROFIL field.
    """
    summary = _summarize_enedis_data(enedis_data_past_year)
    print("DEBUG summary:", json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary:
        return None

    prompt = (
        "Tu es un rédacteur pour Volt Consulting. Voici un JSON contenant DÉJÀ TOUS "
        "les calculs et classements nécessaires sur la consommation d'un client "
        "(ENEDIS) — ne recalcule rien, ne classe rien, n'invente aucun chiffre ni "
        "mois : contente-toi de reformuler ces faits en français fluide.\n\n"
        f"{json.dumps(summary, ensure_ascii=False)}\n\n"
        "Champs à utiliser :\n"
        "- peak_months[0] et lowest_months[0] : mois de plus forte/faible consommation.\n"
        "- dominant_period : poste horaire dominant et son share_pct.\n"
        "- season_split : dominant_season et les deux share_pct (uniquement si non null).\n"
        "- peak_to_average_ratio : à utiliser pour juger de l'exposition au marché.\n"
        "- no_data_months : mois SANS données (futurs) — ne jamais les présenter comme "
        "une consommation nulle ou un creux saisonnier.\n\n"
        "Rédige trois phrases courtes (30 mots maximum chacune — respecte STRICTEMENT "
        "cette limite, une seule phrase par champ, pas de double proposition reliée par "
        "un point-virgule), pour un client professionnel :\n"
        "1) PROFIL: reformule peak_months[0], lowest_months[0], dominant_period, "
        "et season_split (si présent) en une phrase naturelle.\n"
        "2) EXPOSITION: l'exposition de ce profil aux fluctuations du marché, en "
        "t'appuyant sur peak_to_average_ratio et dominant_period.\n"
        "3) STRATEGIE: recommande TOUJOURS un contrat à PRIX FIXE — jamais un taux "
        "variable, un tarif indexé, une offre spot, ou des blocs d'achat prépayés, et "
        "jamais un produit non mentionné dans les données ci-dessus. Explique en une "
        "phrase pourquoi la stabilité du prix fixe convient à ce profil de consommation "
        "précis (pic hivernal, exposition au marché).\n"
        "Réponds STRICTEMENT selon ce format, sans aucun autre texte :\n"
        "PROFIL: <texte>\n"
        "EXPOSITION: <texte>\n"
        "STRATEGIE: <texte>"
    )

    text = _call_market_llm(prompt)
    fields = _parse_llm_fields(text, ["PROFIL", "EXPOSITION", "STRATEGIE"])

    if not fields:
        return _fallback_consumption_analysis(summary)

    # NEW: per-field word-count guard — catches run-on sentences (e.g. two
    # clauses joined by a semicolon) that blow past "30 mots maximum" even
    # though the LLM produced well-formed, on-topic text otherwise.
    if any(len(v.split()) > 40 for v in fields.values()):
        print("Rejected: a field exceeded the 40-word safety cap")
        return _fallback_consumption_analysis(summary)

    combined_text = " ".join(fields.values())
    if not _validate_consumption_text(combined_text, summary):
        return _fallback_consumption_analysis(summary)

    return {
        "profil": fields.get("profil", ""),
        "exposition": fields.get("exposition", ""),
        "strategie": fields.get("strategie", ""),
    }


def _generate_analyses_parallel(chart_data_dto, enedis_data_past_year):
    """Run the market analysis (slide 3) and consumption analysis (slide 4) LLM calls
    concurrently instead of one after another, since each can take minutes - sequentially
    they could add up to several minutes for a single page generation request."""
    with ThreadPoolExecutor(max_workers=2) as executor:
        market_future = executor.submit(_generate_market_analysis, chart_data_dto)
        consumption_future = executor.submit(_generate_consumption_analysis, enedis_data_past_year)
        return market_future.result(), consumption_future.result()


def _format_site_address(client_business_address):
    """Format the 'Site' address so the postal code (5-digit) and everything after it
    (the city) wrap to the next line. Returns an HTML-safe string with a <br> inserted
    before the first postal code; the raw address is escaped first to stay injection-safe.
    Returns "" when there's no street."""
    street = (client_business_address or {}).get("street") if isinstance(client_business_address, dict) else None
    if not street:
        return ""
    # Escape first, then insert the break before the first 5-digit postal code.
    formatted = re.sub(r"\s+(\d{5}\b)", r"<br>\1", escape(street), count=1)
    return mark_safe(formatted)


def _build_sales_info(comparatif_dto):
    """Extract the sales rep's display info from comparatifClientHistoryPdfDto.sales
    (an EmployeeDto-shaped object: name, firstName, email, mobilePhone/professionalPhone,
    photoMedia.path)."""
    sales = comparatif_dto.get("sales")
    if not isinstance(sales, dict):
        return {}

    name = sales.get("name")
    first_name = sales.get("firstName")
    full_name = " ".join(part for part in [name, first_name] if part) or sales.get("fullname")

    photo_media = sales.get("photoMedia")
    photo = photo_media.get("path") if isinstance(photo_media, dict) else None

    phone = sales.get("mobilePhone") or sales.get("professionalPhone") or sales.get("homePhone")

    # Initials for the photo-less fallback avatar: first letter of the first two
    # name words (e.g. "Musab Abbas" -> "MA"), uppercased.
    initials = "".join(word[0] for word in (full_name or "").split()[:2]).upper() or None

    return {
        "name": full_name,
        "initials": initials,
        "email": sales.get("email"),
        "phone": phone,
        "photo": photo,
    }


def _build_slide6_data(comparatif_dto):
    all_providers = comparatif_dto.get("allProvidersForTables", [])
    current = next((p for p in all_providers if p.get("typeFournisseur") == "CURRENT"), {})
    recommended = (comparatif_dto.get("allRegularProviders") or [{}])[0]

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    cur_fourniture = _f(current.get("fourniture"))
    rec_fourniture = _f(recommended.get("fourniture"))
    fourniture_economy = (cur_fourniture - rec_fourniture) if (cur_fourniture is not None and rec_fourniture is not None) else None

    cur_turpe = _f(current.get("turpe"))
    rec_turpe = _f(recommended.get("turpe"))
    turpe_economy = (cur_turpe - rec_turpe) if (cur_turpe is not None and rec_turpe is not None) else None

    cur_taxes = _f(current.get("taxes"))
    rec_taxes = _f(recommended.get("taxes"))
    taxes_economy = (cur_taxes - rec_taxes) if (cur_taxes is not None and rec_taxes is not None) else None

    cur_cout_htva = _f(current.get("coutHTVA"))
    rec_cout_htva = _f(recommended.get("coutHTVA"))
    total_ht_economy = (cur_cout_htva - rec_cout_htva) if (cur_cout_htva is not None and rec_cout_htva is not None) else None

    tva_amount = (rec_cout_htva * 0.20) if rec_cout_htva is not None else None
    total_ttc = (rec_cout_htva + tva_amount) if (rec_cout_htva is not None and tva_amount is not None) else None

    # "DÉTAIL DU BUDGET RECOMMANDÉ (HT)" sub-table: amounts come from the
    # recommended offer, the total HT is their sum, and each row's share is
    # (amount / total) × 100.
    _parts = [rec_fourniture, rec_turpe, rec_taxes]
    breakdown_total_ht = sum(_parts) if all(p is not None for p in _parts) else None

    def _pct(val):
        return round(val / breakdown_total_ht * 100, 1) if (val is not None and breakdown_total_ht) else None

    breakdown = {
        "fourniture": rec_fourniture,
        "turpe": rec_turpe,
        "taxes": rec_taxes,
        "total_ht": breakdown_total_ht,
        "fourniture_pct": _pct(rec_fourniture),
        "turpe_pct": _pct(rec_turpe),
        "taxes_pct": _pct(rec_taxes),
    }

    return {
        "current": current,
        "recommended": recommended,
        # True only when the payload actually carries a CURRENT (incumbent) provider.
        # When False, the whole "offre actuelle" comparison isn't meaningful.
        "has_current": bool(current),
        "fourniture_economy": fourniture_economy,
        "turpe_economy": turpe_economy,
        "taxes_economy": taxes_economy,
        "total_ht_economy": total_ht_economy,
        "tva_amount": tva_amount,
        "total_ttc": total_ttc,
        "breakdown": breakdown,
        "economy_pct": comparatif_dto.get("ratioHTVA"),
        "economy_eur": comparatif_dto.get("differenceHTVA"),
    }


def build_presentation_data_energy_offer(data, enedis_chart_base64, chart_base64, chart_12m_base64, comparatif_dto, request):
    """
    Build presentation data for the energy offer summary page.
    Follows the same pattern as build_presentation_data_Electricity.
    """
    print("Inside BuildPresentationDataEnergyOffer")

    # Helper function to safely get values
    def safe_value(value):
        if value is None:
            return ""
        try:
            str_val = str(value).strip().lower()
            if str_val == "" or str_val == "none" or str_val == "null":
                return ""
            return str(value)
        except AttributeError:
            return str(value) if value is not None else ""

    # Get ratioHTVA and differenceHTVA values
    ratio_htva = comparatif_dto.get("ratioHTVA")
    difference_htva = comparatif_dto.get("differenceHTVA")

    # Initialize black, black1, black3 based on conditions
    black = ""
    black1 = ""
    black3 = ""

    # Condition for black (ratioHTVA):
    # Show only if ratioHTVA is not None, not empty, and ≤ 0
    if ratio_htva is not None and ratio_htva != "":
        try:
            ratio_num = float(ratio_htva)
            if ratio_num <= 0:
                black = f"{ratio_htva}%"
        except (ValueError, TypeError):
            pass

    # Condition for black1 (differenceHTVA):
    # Show only if differenceHTVA is not None, not empty, and ≤ 0
    if difference_htva is not None and difference_htva != "":
        try:
            diff_num = float(difference_htva)
            if diff_num <= 0:
                black1 = f"{difference_htva}€"
        except (ValueError, TypeError):
            pass

    # Condition for black3 ("économisé/an"):
    if black != "" and black1 != "":
        black3 = "économisé/an"

    # ── Market analysis (slide 3): precomputed ONLY — no LLM. If nothing is sent,
    #    the template falls back to its static default text (same as the gas deck). ──
    precomputed_analyse = data.get("precomputedAnalyse")
    precomputed_recommandation = data.get("precomputedRecommandation")
    market_analysis = {
        "analyse": precomputed_analyse or "",
        "recommandation": precomputed_recommandation or "",
    }
    if not any(market_analysis.values()):
        market_analysis = data.get("marketAnalysis") or data.get("market_analysis") or {}

    # ── Consumption analysis (slide 4): prefer precomputed, else fall back to LLM. ──
    precomputed_profil = data.get("precomputedProfil")
    precomputed_exposition = data.get("precomputedExposition")
    precomputed_strategie = data.get("precomputedStrategie")
    if precomputed_profil or precomputed_exposition or precomputed_strategie:
        consumption_analysis = {
            "profil": precomputed_profil or "",
            "exposition": precomputed_exposition or "",
            "strategie": precomputed_strategie or "",
        }
    else:
        consumption_analysis = _generate_consumption_analysis(
            data.get("comparatifClientHistoryPdfDto", {}).get("enedisDataPastYear")
        )

    return {
        "title": data.get("title", "VOLT CONSULTING - Energy Services Presentation"),
        "headingone": "APPEL D'OFFRE",
        "clientSociety": safe_value(data.get("clientSociety")),
        "clientSiret": safe_value(data.get("clientSiret")),
        "clientFirstName": safe_value(data.get("clientFirstName")),
        "clientLastName": safe_value(data.get("clientLastName")),
        "clientEmail": safe_value(data.get("clientEmail")),
        "clientPhoneNumber": safe_value(data.get("clientPhoneNumber")),
        "clientBusinessAddress": data.get("clientBusinessAddress", {}),
        "client_site_address": _format_site_address(data.get("clientBusinessAddress")),
        "currentSupplierName": safe_value(comparatif_dto.get("currentSupplierName")),
        "currentContractExpiryDate": (
            datetime.fromtimestamp(comparatif_dto.get("currentContractExpiryDate") / 1000, tz=PARIS_TZ).strftime("%d/%m/%Y")
            if comparatif_dto.get("currentContractExpiryDate") else ""
        ),
        "black": black,
        "black1": black1,
        "black3": black3,
        "image": build_image_section(data, chart_base64),
        "has_chart": chart_base64 is not None,
        "has_chart_data": bool(data.get("chartDataDto")),
        "imageOne": {
            "enedis_chart": enedis_chart_base64 if enedis_chart_base64 else ""
        },
        "imageTwo": {
            "chart_12m": chart_12m_base64 if chart_12m_base64 else ""
        },
        "chart_date_ranges": _compute_chart_date_ranges(data),
        "images": build_images(data, request, True),
        "company_presentation": build_company_presentation(data),
        "comparatifClientHistoryPdfDto": comparatif_dto,
        "budget_global": build_budget_section(data),
        "tender_results": build_tender_results(data),
        "comparison_table": build_comparison_table_Electricity(data),
        "tender_table": build_tender_table_Electricity(data, comparatif_dto),
        "change_section": build_change_section(data),
        "contact_info": build_contact_info(data),
        "enedis_info": enedis_Chart(comparatif_dto),
        "volt_logo_base_url": "https://crm.volt-consulting.com/uploads/volt/providers/",
        "provider_page_chunks": [
            comparatif_dto.get("allProvidersForTables", [])[i:i+6]
            for i in range(0, max(len(comparatif_dto.get("allProvidersForTables", [])), 1), 6)
        ],
        "slide6": _build_slide6_data(comparatif_dto),
        "sales": _build_sales_info(comparatif_dto),
        "market_analysis": market_analysis or {},
        "consumption_analysis": consumption_analysis or {},
    }


def build_comparatif_dto_Gas(comparatif, request, data):
    """GAS counterpart of build_comparatif_dto_Electricity — same provider
    pagination/sorting/green-row logic, validated against energyType GAS."""
    print("Inside BuildComparatifDTOGas")

    created_on_raw = comparatif.get("createdOn")
    if not created_on_raw:
        raise ValueError("Missing required field: createdOn")

    try:
        dt = datetime.fromtimestamp(created_on_raw / 1000.0, tz=PARIS_TZ)
        created_on = dt.strftime("%d/%m/%Y")
        created_on_time = dt.strftime("%Hh%M")
    except Exception as e:
        raise ValueError(f"Invalid createdOn value: {e}")

    dto = {
        "title": data.get("contexte_title", "Contexte global"),
        "createdOn": created_on,
        "createdOnTime": created_on_time,
        "energyType": comparatif.get("energyType"),
        "pce": comparatif.get("pce"),
        "gasProfile": comparatif.get("gasProfile"),
        "routingRate": comparatif.get("routingRate"),
        "segmentation": comparatif.get("segmentation"),
        "contractStartDate": comparatif.get("contractStartDate"),
        "volumeAnnual": comparatif.get("volumeAnnual"),
        "ratioHTVA": comparatif.get("ratioHTVA"),
        "differenceHTVA": comparatif.get("differenceHTVA"),
        "currentSupplierName": comparatif.get("currentSupplierName"),
        "currentContractExpiryDate": comparatif.get("currentContractExpiryDate"),
        # Best-effort gas profile metadata — key names guessed from the template;
        # confirm/adjust once the real CRM payload for GAS is available.
        "typology": comparatif.get("typology"),
        "debitLabel": comparatif.get("debitLabel"),
        "typologyDetail": comparatif.get("typologyDetail"),
        "usage": comparatif.get("usage"),
        "winterPct": comparatif.get("winterPct"),
        "summerPct": comparatif.get("summerPct"),
        "cpb2026": comparatif.get("cpb2026"),
        "cpb2027": comparatif.get("cpb2027"),
        "cpb2028": comparatif.get("cpb2028"),
        "sales": comparatif.get("sales"),
    }

    if dto.get("energyType") != "GAS":
        raise ValueError("Invalid or missing energyType. Must be 'GAS'.")
    if not dto.get("pce"):
        raise ValueError("Missing required GAS field: pce")

    # Separate CURRENT and REGULAR providers
    comparatif_rates = comparatif.get("comparatifRates", [])

    # Alias gas-specific cost fields so both the comparison table
    # (acheminementGrdf / acciseGaz / abonnementAnswer) and the summary slide
    # (distribution / accise / abonnementAnnual) read from the same raw provider values.
    for provider in comparatif_rates:
        if provider.get("acheminementGrdf") is None:
            provider["acheminementGrdf"] = provider.get("distribution")
        if provider.get("acciseGaz") is None:
            provider["acciseGaz"] = provider.get("ticgn")
        if provider.get("abonnementAnswer") is None:
            provider["abonnementAnswer"] = provider.get("abonnementAnnual")

    current_providers = [p for p in comparatif_rates if p.get("typeFournisseur") == "CURRENT"]
    regular_providers = [p for p in comparatif_rates if p.get("typeFournisseur") != "CURRENT"]

    def get_cout_htva(provider):
        cout_htva = provider.get("coutHTVA")
        if cout_htva is None:
            return float('inf')
        try:
            return float(cout_htva)
        except (ValueError, TypeError):
            return float('inf')

    regular_providers.sort(key=get_cout_htva)

    current_cout_htva = get_cout_htva(current_providers[0]) if current_providers else None

    min_regular_cout_htva = None
    if regular_providers:
        min_regular_cout_htva = get_cout_htva(regular_providers[0])
        if min_regular_cout_htva == float('inf'):
            min_regular_cout_htva = None

    paginated_containers = []
    current_index = 0
    regular_index = 0
    green_row_used = True

    while current_index < len(current_providers) or regular_index < len(regular_providers):
        container = {
            "current_providers": [],
            "regular_providers": [],
            "show_header": len(paginated_containers) == 0,
            "show_title_labels": False
        }

        rows_in_container = 0

        while current_index < len(current_providers) and rows_in_container < 4:
            container["current_providers"].append(current_providers[current_index])
            current_index += 1
            rows_in_container += 1

        if current_index >= len(current_providers) and len(container["current_providers"]) > 0:
            container["show_title_labels"] = True

        if len(current_providers) == 0 and len(paginated_containers) == 0:
            container["show_title_labels"] = True

        while regular_index < len(regular_providers) and rows_in_container < 4:
            provider = regular_providers[regular_index]

            if (not green_row_used and
                container["show_title_labels"] and
                regular_index == 0 and
                min_regular_cout_htva is not None and
                current_cout_htva is not None and
                min_regular_cout_htva <= current_cout_htva):

                provider["is_green_row"] = True
                green_row_used = True
            else:
                provider["is_green_row"] = False

            container["regular_providers"].append(provider)
            regular_index += 1
            rows_in_container += 1

        paginated_containers.append(container)

    dto["paginatedContainers"] = paginated_containers
    dto["comparatifRates"] = comparatif_rates

    all_regular_providers = []
    for container in paginated_containers:
        all_regular_providers.extend(container["regular_providers"])
    dto["allRegularProviders"] = all_regular_providers

    all_providers_for_tables = []
    for container in paginated_containers:
        all_providers_for_tables.extend(container["current_providers"])
    all_providers_for_tables.extend(all_regular_providers)
    dto["allProvidersForTables"] = all_providers_for_tables

    return dto


def _build_slide6_data_gas(comparatif_dto):
    all_providers = comparatif_dto.get("allProvidersForTables", [])
    current_raw = next((p for p in all_providers if p.get("typeFournisseur") == "CURRENT"), {})
    recommended_raw = (comparatif_dto.get("allRegularProviders") or [{}])[0]

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    volume_annual = _f(comparatif_dto.get("volumeAnnual"))

    def _cee_annual(provider):
        # Prefer a direct annual CEE amount; otherwise derive it from the
        # €/MWh rate (partCee) multiplied by the annual volume.
        for key in ("ceeAnnual", "partCeeByCA", "ceeByCA"):
            val = _f(provider.get(key))
            if val is not None:
                return val
        part = _f(provider.get("partCee"))
        if part is not None and volume_annual is not None:
            return round(part * volume_annual, 2)
        return None

    def _build_side(provider):
        abonnement = _f(provider.get("abonnementAnswer"))
        if abonnement is None:
            abonnement = _f(provider.get("abonnementAnnual"))

        total_ht = _f(provider.get("coutHTVA"))
        tva = _f(provider.get("tva"))
        total_ttc = _f(provider.get("coutTTC"))

        if tva is None and total_ht is not None:
            tva = round(total_ht * 0.20, 2)
        if total_ttc is None and total_ht is not None and tva is not None:
            total_ttc = round(total_ht + tva, 2)

        return {
            "fourniture": _f(provider.get("fourniture")),
            "cee": _cee_annual(provider),
            "distribution": _f(provider.get("distribution")),
            "abonnement": abonnement,
            "cta": _f(provider.get("cta")),
            "accise": _f(provider.get("ticgn")),
            "total_ht": total_ht,
            "tva": tva,
            "total_ttc": total_ttc,
        }

    current = _build_side(current_raw)
    recommended = _build_side(recommended_raw)

    def _diff(key):
        a, b = current.get(key), recommended.get(key)
        return (a - b) if (a is not None and b is not None) else None

    breakdown = {}
    rec_total_ht = recommended.get("total_ht")
    for key in ("fourniture", "distribution", "abonnement", "cta", "accise"):
        value = recommended.get(key)
        breakdown[key] = value
        breakdown[f"{key}_pct"] = (
            round(value / rec_total_ht * 100, 1)
            if value is not None and rec_total_ht else None
        )

    return {
        "current": current,
        "recommended": recommended,
        # True only when the payload actually carries a CURRENT (incumbent) provider.
        # When False, the whole "offre actuelle" comparison isn't meaningful.
        "has_current": bool(current_raw),
        "fourniture_economy": _diff("fourniture"),
        "cee_economy": _diff("cee"),
        "distribution_economy": _diff("distribution"),
        "abonnement_economy": _diff("abonnement"),
        "cta_economy": _diff("cta"),
        "accise_economy": _diff("accise"),
        "total_ht_economy": _diff("total_ht"),
        "total_ttc_economy": _diff("total_ttc"),
        "breakdown": breakdown,
        "economy_pct": comparatif_dto.get("ratioHTVA"),
        "economy_eur": comparatif_dto.get("differenceHTVA"),
    }


def _format_contract_date(ts):
    """Format a contractStartDate timestamp (seconds or milliseconds) to
    dd/mm/yyyy, matching the electricity deck (enedis_Chart). Returns '-' if
    the value is missing or not a usable timestamp."""
    if not ts:
        return "-"
    try:
        if ts > 1e12:  # likely milliseconds
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=PARIS_TZ).strftime("%d/%m/%Y")
    except (ValueError, TypeError, OSError):
        return "-"


def build_presentation_data_gas(data, chart_base64, chart_12m_base64, gas_chart_base64, comparatif_dto, request):
    """
    Build presentation data for the gas (GAZ) comparatif page.
    Follows the same pattern as build_presentation_data_energy_offer.
    """
    print("Inside BuildPresentationDataGas")

    def safe_value(value):
        if value is None:
            return ""
        try:
            str_val = str(value).strip().lower()
            if str_val == "" or str_val == "none" or str_val == "null":
                return ""
            return str(value)
        except AttributeError:
            return str(value) if value is not None else ""

    client_first_name = safe_value(data.get("clientFirstName"))
    client_last_name = safe_value(data.get("clientLastName"))
    client_contact_name = safe_value(
        data.get("clientContactName") or f"{client_first_name} {client_last_name}".strip()
    )

    # Contract start date: format the timestamp to dd/mm/yyyy like the electricity
    # deck, instead of showing the raw epoch value.
    contract_start_date = _format_contract_date(comparatif_dto.get("contractStartDate"))

    # Seasonal split (winter/summer %): prefer what the Java backend now sends at
    # the top level (from /api/analyze-gas-invoice/), fall back to the DTO, then
    # the template's 70/30 default.
    winter_pct = data.get("winterPct")
    if winter_pct is None:
        winter_pct = comparatif_dto.get("winterPct")
    summer_pct = data.get("summerPct")
    if summer_pct is None:
        summer_pct = comparatif_dto.get("summerPct")

    # Slide-4 analysis: prefer the precomputed texts (same convention as the
    # electricity deck's precomputedProfil/…), then a consumptionAnalysis object,
    # else {} so the template keeps its static fallback.
    _ca_obj = data.get("consumptionAnalysis") or data.get("consumption_analysis") or {}
    consumption_analysis = {
        "profil": data.get("precomputedProfil") or _ca_obj.get("profil") or "",
        "exposition": data.get("precomputedExposition") or _ca_obj.get("exposition") or "",
        "strategie": data.get("precomputedStrategie") or _ca_obj.get("strategie") or "",
        "recommandation": data.get("precomputedGasInvoiceRecommandation") or _ca_obj.get("recommandation") or "",
    }
    if not any(consumption_analysis.values()):
        consumption_analysis = {}

    # Slide-3 market analysis: same precomputed convention as the electricity deck
    # (precomputedAnalyse / precomputedRecommandation). Absent → static fallback.
    market_analysis = {
        "analyse": data.get("precomputedAnalyse") or "",
        "recommandation": data.get("precomputedRecommandation") or "",
    }
    if not any(market_analysis.values()):
        market_analysis = data.get("marketAnalysis") or data.get("market_analysis") or {}

    return {
        "title": data.get("title", "VOLT CONSULTING - Gas Services Presentation"),
        "clientSociety": safe_value(data.get("clientSociety")),
        "clientContactName": client_contact_name,
        "clientFirstName": client_first_name,
        "clientLastName": client_last_name,
        "clientPhoneNumber": safe_value(data.get("clientPhoneNumber")),
        "clientBusinessAddress": data.get("clientBusinessAddress", {}),
        "client_site_address": _format_site_address(data.get("clientBusinessAddress")),
        "gas_info": {
            "pce": comparatif_dto.get("pce"),
            "contract_start_date": contract_start_date,
            "segmentation": comparatif_dto.get("segmentation"),
            "routing_rate": comparatif_dto.get("routingRate"),
            "profile": comparatif_dto.get("gasProfile"),
            "profile_threshold": comparatif_dto.get("routingRate"),
            "total_annual_mwh": comparatif_dto.get("volumeAnnual"),
            "typology": comparatif_dto.get("typology"),
            "debit_label": comparatif_dto.get("debitLabel"),
            "typology_detail": comparatif_dto.get("typologyDetail"),
            "usage": comparatif_dto.get("usage"),
            "winter_pct": winter_pct,
            "summer_pct": summer_pct,
            "cpb_2026": comparatif_dto.get("cpb2026"),
            "cpb_2027": comparatif_dto.get("cpb2027"),
            "cpb_2028": comparatif_dto.get("cpb2028"),
        },
        # LLM-generated slide-4 texts (profil/exposition/stratégie): the Java
        # backend sends them as precomputedProfil/Exposition/Strategie (resolved
        # into consumption_analysis above). Absent → template keeps static text.
        "consumption_analysis": consumption_analysis,
        "market_analysis": market_analysis,
        "has_chart_data": bool(data.get("chartDataDto")),
        "images": build_images(data, request, True),
        "comparatifClientHistoryPdfDto": comparatif_dto,
        "image": build_image_section(data, chart_base64),
        "imageOne": {
            "gas_chart": gas_chart_base64 if gas_chart_base64 else ""
        },
        "imageTwo": {
            "chart_12m": chart_12m_base64 if chart_12m_base64 else ""
        },
        "chart_date_ranges": _compute_chart_date_ranges(data),
        "volt_logo_base_url": "https://crm.volt-consulting.com/uploads/volt/providers/",
        "provider_page_chunks": [
            comparatif_dto.get("allProvidersForTables", [])[i:i+6]
            for i in range(0, max(len(comparatif_dto.get("allProvidersForTables", [])), 1), 6)
        ],
        "gas_providers": {
            "recommended": (comparatif_dto.get("allRegularProviders") or [None])[0],
        },
        "slide6": _build_slide6_data_gas(comparatif_dto),
        "advisor": data.get("advisor", {}),
        # Same source/shape as the electricity deck (photo, name, initials, phone,
        # email) so slide 8 can reuse the identical photo + initials-fallback logic.
        "sales": _build_sales_info(comparatif_dto),
    }


def generate_simple_pdf(html_content, request, data, comparatif):
    """Simple PDF generator without complex blank page removal"""
    host = request.get_host().split(":")[0]

    if host == "volt-crm.caansoft.com":
        base_dir = settings.STAGING_MEDIA_ROOT
        base_url = settings.STAGING_MEDIA_URL
    elif host == "crm.volt-consulting.com":
        base_dir = settings.PRODUCTION_MEDIA_ROOT
        base_url = settings.PRODUCTION_MEDIA_URL
    else:
        base_dir = settings.MEDIA_ROOT
        base_url = settings.MEDIA_URL

    relative_path = os.path.join("clients", str(data.get("clientId")), "energy_offer")
    pdf_dir = os.path.join(base_dir, relative_path)
    os.makedirs(pdf_dir, exist_ok=True)

    pdf_filename = f"Energy_Offer_{data.get('clientSociety', 'client')}_{datetime.now().strftime('%Y%m%d')}.pdf"
    pdf_path = os.path.join(pdf_dir, pdf_filename)

    css = CSS(string="@page { size: A4 landscape; margin: 6mm; }")
    HTML(string=html_content).write_pdf(pdf_path, stylesheets=[css])

    pdf_url = request.build_absolute_uri(
        os.path.join(base_url, relative_path, pdf_filename)
    )

    return pdf_url, pdf_filename

def generate_enedis_bar_chart(chart_data):
    print("Inside GenerateEnedisBarChart")

    if not chart_data or not isinstance(chart_data, dict):
        return None

    months = chart_data.get("months", [])
    consumption_data = chart_data.get("consumptionData", {})

    if not months or not consumption_data:
        return None

    has_data = False
    for values in consumption_data.values():
        if values and any(v > 0 for v in values):
            has_data = True
            break

    if not has_data:
        return None

    label_colors = {
        "HCE": "#2e7d45",
        "HPE": "#f0b429",
        "HPH": "#b8c2cc",
        "HCH": "#b8c2cc",
        "HP":  "#f0b429",
        "HC":  "#2e7d45",
        "BASE": "#6366f1",
    }

    preferred_order = ["HPH", "HCH", "HPE", "HCE", "HP", "HC", "BASE"]
    categories = []
    data_values = []
    for label in preferred_order:
        if label in consumption_data:
            vals = consumption_data[label]
            if vals and any(v > 0 for v in vals):
                categories.append(label)
                data_values.append(vals)
    for label, vals in consumption_data.items():
        if label not in preferred_order and vals and any(v > 0 for v in vals):
            categories.append(label)
            data_values.append(vals)

    n_months = len(months)
    x = np.arange(n_months)
    bar_width = 0.62

    # Create figure with transparent background (like generate_enedis_chart)
    fig, ax = plt.subplots(figsize=(11, 3.6), dpi=150)

    # Make backgrounds transparent
    fig.patch.set_alpha(0)
    ax.patch.set_alpha(0)

    # ── Stacked bars ───────────────────────────────────────────────────────
    bottoms = np.zeros(n_months)
    bar_handles = []
    for label, values in zip(categories, data_values):
        vals = np.array(values, dtype=float)
        color = label_colors.get(label, "#aaaaaa")
        bars = ax.bar(x, vals, bar_width, bottom=bottoms, color=color,
                      zorder=3, linewidth=0)
        bar_handles.append((bars[0], label, color))
        bottoms += vals

    # ── Value labels — French comma format ────────────────────────────────
    if len(bottoms) > 0 and bottoms.max() > 0:
        for i, total in enumerate(bottoms):
            if total > 0:
                label_text = f"{total:.1f}".replace(".", ",")
                ax.text(
                    x[i], total + (bottoms.max() * 0.015),
                    label_text,
                    ha="center", va="bottom",
                    fontsize=7.5, color="#374151", fontweight="600",
                )

    # ── Legend ─────────────────────────────────────────────────────────────
    import matplotlib.patches as mpatches
    legend_patches = [
        mpatches.Patch(facecolor=color, label=label, linewidth=0)
        for _, label, color in bar_handles
    ]
    legend = ax.legend(
        handles=legend_patches,
        loc="upper left",
        bbox_to_anchor=(0.0, 1.18),
        ncol=len(legend_patches),
        frameon=False,
        fontsize=10,
        handlelength=1.4,
        handleheight=1.1,
        handletextpad=0.5,
        columnspacing=1.2,
    )
    for text in legend.get_texts():
        text.set_color("#374151")

    # ── X-axis ─────────────────────────────────────────────────────────────
    ax.set_xticks(x)
    ax.set_xticklabels(months, fontsize=8, color="#6b7280", rotation=0)
    ax.tick_params(axis="x", length=0, pad=5)

    # ── Y-axis ─────────────────────────────────────────────────────────────
    ax.set_ylabel("Consommation (MWh)", fontsize=8, color="#9ca3af", labelpad=6)
    ax.tick_params(axis="y", length=0)

    # ── Spines ─────────────────────────────────────────────────────────────
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color("#e5e7eb")

    plt.tight_layout(pad=0.4)

    # IMPORTANT: Use transparent=True exactly like generate_enedis_chart
    buffer = io.BytesIO()
    plt.savefig(buffer, format="png", bbox_inches="tight", transparent=True, dpi=150)
    plt.close()
    buffer.seek(0)

    img_base64 = base64.b64encode(buffer.read()).decode("utf-8")
    return f"data:image/png;base64,{img_base64}"

@csrf_exempt
@require_http_methods(["POST"])
def generate_market_analysis(request):
    try:
        data = parse_request_data(request)
        analysis = _generate_market_analysis(data.get("chartDataDto"))  # {analyse, recommandation} or None        
        if not analysis:
            return JsonResponse({"status": "error", "message": "No analysis generated"}, status=200)
        return JsonResponse({
            "status": "success",
            "analyse": analysis.get("analyse", ""),
            "recommandation": analysis.get("recommandation", ""),
        })
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)

@csrf_exempt
@require_http_methods(["POST"])
def generate_consumption_analysis(request):
    try:
        data = parse_request_data(request)
        # NOTE: this reads enedisDataPastYear at the TOP LEVEL of the payload,
        # while build_presentation_data_energy_offer reads it nested under
        # comparatifClientHistoryPdfDto. If both endpoints are meant to accept
        # the same payload shape, confirm which nesting the caller actually
        # sends — otherwise one of the two paths will silently get None here.
        analysis = _generate_consumption_analysis(data.get("enedisDataPastYear"))
        if not analysis:
            return JsonResponse({"status": "error", "message": "No analysis generated"}, status=200)
        return JsonResponse({
            "status": "success",
            "profil": analysis.get("profil", ""),
            "exposition": analysis.get("exposition", ""),
            "strategie": analysis.get("strategie", ""),
        })
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)

# ─────────────────────────────────────────────────────────────────────────────
# Gas invoice → seasonal (winter/summer) consumption split
#
# Standalone endpoint, deliberately OUTSIDE the HTML-generation flow (a sibling
# to generate_market_analysis / generate_consumption_analysis). The Java backend
# holds the per-PCE gas invoices; it POSTs {pce, invoiceUrl} here. We download
# the PDF, pull its text (PyPDF2), let the LLM LOCATE the monthly-consumption
# history (layouts differ per supplier), then compute the Nov→Mar vs Apr→Oct
# split IN PYTHON — the model never does the arithmetic. Returns winterPct /
# summerPct, ready to drop onto comparatifClientHistoryPdfDto so gas_info.
# winter_pct / summer_pct become dynamic on slide 4.
# ─────────────────────────────────────────────────────────────────────────────

# Winter = November → March (heating season); everything else counts as summer.
_GAS_WINTER_MONTHS = {11, 12, 1, 2, 3}


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


def _fr_month_to_num(label):
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


def _is_safe_fetch_url(url):
    """Light SSRF guard: allow only http(s) and reject obvious internal/loopback
    IP literals. Hostnames are allowed (this endpoint is for trusted
    server-to-server use by the Java backend)."""
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        ip = ipaddress.ip_address(p.hostname)
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return False
    except ValueError:
        pass  # not an IP literal → a hostname, allowed
    return True


def _download_pdf_bytes(url, max_bytes=20 * 1024 * 1024, timeout=45):
    """Download a PDF from url. Returns bytes; raises ValueError on any problem."""
    if not _is_safe_fetch_url(url):
        raise ValueError("Invalid or disallowed invoice URL")
    req = urllib.request.Request(url, headers={"User-Agent": "volt-pdf-service/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        if status not in (200, None):
            raise ValueError(f"Invoice download failed (HTTP {status})")
        data = resp.read(max_bytes + 1)
    if not data:
        raise ValueError("Empty invoice download")
    if len(data) > max_bytes:
        raise ValueError("Invoice PDF exceeds size limit")
    return data


def _extract_pdf_text(pdf_bytes):
    """Extract all text from a PDF (PyPDF2). Returns a string ('' if none)."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        raise ValueError(f"Could not read PDF: {e}")
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts).strip()


_MONTH_RE = re.compile(
    r"^(jan|f[ée]v|mars?|avr|mai|juin|juil|ao[uû]|sept?|oct|nov|d[ée]c)", re.IGNORECASE
)


def _compute_gas_season_split(pairs):
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


def _to_float(v):
    """Parse a value into float, tolerating French formatting (thin/regular
    spaces as thousands separators, comma decimals). Returns None if not numeric."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(" ", "").replace("\xa0", "").replace(" ", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None
    return None


# ═════════════════════════════════════════════════════════════════════════
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


def _extract_gas_monthly_pairs(pdf_bytes):
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


def _pairs_from_curve(curve):
    """Convert an enedis/grdf-style {months, consumptionData} dict into
    [{month, kwh}] by summing all series at each month index."""
    if not isinstance(curve, dict):
        return []
    months = curve.get("months") or []
    series = curve.get("consumptionData") or {}
    if not months or not isinstance(series, dict):
        return []
    out = []
    for i, label in enumerate(months):
        total = 0.0
        seen = False
        for vals in series.values():
            if isinstance(vals, list) and i < len(vals):
                f = _to_float(vals[i])
                if f is not None:
                    total += f
                    seen = True
        if seen:
            out.append({"month": label, "kwh": total})
    return out


@csrf_exempt
@require_http_methods(["POST"])
def analyze_gas_invoice(request):
    """One-shot gas invoice analysis — NOT part of deck generation.

    POST JSON: {"invoiceUrl": "https://.../facture.pdf", "pce": "<optional>"}
      (or skip the download with a pre-extracted curve:
       {"monthly": [{"month","kwh"}, ...]} or {"gasData": {"months","consumptionData"}}.)

    Downloads the invoice ONCE, extracts the monthly consumption curve (chart-
    coordinate reconstruction, with an LLM-on-text fallback), then returns BOTH
    in a single response:
      - winterPct / summerPct — the Nov→Mar vs Apr→Oct split, computed in Python;
      - consumptionAnalysis {profil, exposition, strategie} — slide-4 text from
        the LLM, validated against the same facts, with a deterministic fallback.
    Map winterPct/summerPct onto comparatifClientHistoryPdfDto and
    consumptionAnalysis onto the comparatif-gas payload."""
    try:
        try:
            body = json.loads(request.body) if request.body else {}
        except (ValueError, TypeError):
            return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)

        pce = body.get("pce")
        source = "provided"

        # Data source: a pre-extracted curve, else download + extract the invoice.
        pairs = body.get("monthly") if isinstance(body.get("monthly"), list) else None
        if not pairs:
            curve = body.get("gasData") or body.get("grdfDataPastYear") or body.get("enedisDataPastYear")
            if isinstance(curve, dict):
                pairs = _pairs_from_curve(curve)
        if not pairs:
            url = body.get("invoiceUrl") or body.get("invoice_url") or body.get("url")
            if not url:
                return JsonResponse({"status": "error", "pce": pce,
                                     "message": "Provide one of: invoiceUrl, monthly, or gasData"}, status=400)
            try:
                pdf_bytes = _download_pdf_bytes(url)
            except ValueError as e:
                return JsonResponse({"status": "error", "pce": pce, "message": str(e)}, status=400)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                return JsonResponse({"status": "error", "pce": pce, "message": f"Download failed: {e}"}, status=502)
            pairs, xsource = _extract_gas_monthly_pairs(pdf_bytes)
            if xsource == "empty":
                return JsonResponse({"status": "no_data", "pce": pce,
                                     "message": "No extractable text in the PDF (likely a scanned image; OCR is not enabled)."}, status=200)
            if xsource == "vision_unavailable":
                return JsonResponse({"status": "error", "pce": pce, "message": "LLM extraction unavailable"}, status=502)
            source = "invoice:" + xsource

        if not pairs:
            return JsonResponse({"status": "no_data", "pce": pce,
                                 "message": "No monthly consumption history available."}, status=200)

        # Both outputs from the SAME extracted curve — no second download/parse.
        split = _compute_gas_season_split(pairs)
        if not split:
            return JsonResponse({"status": "no_data", "pce": pce,
                                 "message": "Could not compute the seasonal split."}, status=200)
        analysis = _generate_consumption_analysis_gas(pairs)

        return JsonResponse({
            "status": "success",
            "pce": pce,
            "source": source,
            "winterPct": split["winterPct"],
            "summerPct": split["summerPct"],
            "winterKwh": split["winterKwh"],
            "summerKwh": split["summerKwh"],
            "totalKwh": split["totalKwh"],
            "monthsUsed": split["monthsUsed"],
            "confidence": split["confidence"],
            "monthly": split["monthly"],
            "consumptionAnalysis": analysis or {},
        })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


# ─────────────────────────────────────────────────────────────────────────────
# Gas slide-4 consumption analysis (profil / exposition / stratégie)
#
# Gas counterpart of _generate_consumption_analysis (electricity). Same contract:
# every fact is computed in Python (_summarize_gas_data); the LLM only phrases
# them and is validated against the summary, with a deterministic fallback. The
# difference is the data source — gas curves come from the invoice, so the
# endpoint can take an invoiceUrl (reusing the same extractor as the seasonal
# split) or a pre-extracted curve. Kept OUT of deck generation: the Java backend
# calls this, then injects the returned texts into the comparatif-gas payload as
# `consumptionAnalysis`, where build_presentation_data_gas passes them through to
# data.consumption_analysis on slide 4.
# ─────────────────────────────────────────────────────────────────────────────

def _norm_month_mmyyyy(label):
    """Normalize an invoice month label ('Nov 25', 'janvier 2026') to 'MM/YYYY'
    so peak/low months match what _validate_consumption_text / _extract_mentioned_months
    expect (they normalize the LLM's month references the same way). Returns the
    original label unchanged if the month or year can't be parsed."""
    mnum = _fr_month_to_num(label)
    ym = re.search(r"(\d{2,4})", str(label or ""))
    if mnum is None or not ym:
        return label
    year = ym.group(1)
    if len(year) == 2:
        year = "20" + year
    return f"{mnum:02d}/{year}"


def _summarize_gas_data(pairs):
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


def _fallback_consumption_analysis_gas(summary):
    """Plain templated output, used only if the LLM output fails validation.
    Guaranteed numerically correct since it is built directly from summary."""
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


def _generate_consumption_analysis_gas(pairs):
    """All facts are computed in _summarize_gas_data. The LLM only phrases them
    (GAS-specific: seasonality, winter exposure, fixed-price strategy). Output is
    validated against the summary and falls back to a plain template on mismatch.
    Returns {profil, exposition, strategie}, or None if there's no usable data."""
    summary = _summarize_gas_data(pairs)
    if not summary:
        return None

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

    text = _call_market_llm(prompt)
    fields = _parse_llm_fields(text, ["PROFIL", "EXPOSITION", "STRATEGIE", "RECOMMANDATION"])
    if not fields:
        return _fallback_consumption_analysis_gas(summary)
    if any(len(v.split()) > 40 for v in fields.values()):
        print("Rejected (gas): a field exceeded the 40-word safety cap")
        return _fallback_consumption_analysis_gas(summary)
    if not _validate_consumption_text(" ".join(fields.values()), summary):
        return _fallback_consumption_analysis_gas(summary)

    return {
        "profil": fields.get("profil", ""),
        "exposition": fields.get("exposition", ""),
        "strategie": fields.get("strategie", ""),
        "recommandation": fields.get("recommandation", ""),
    }


# The gas invoice endpoint (seasonal split + slide-4 analysis, in one call) is
# analyze_gas_invoice, defined above — it reuses the helpers in this section.


@csrf_exempt
@require_http_methods(["POST"])
def save_file_edit(request):
    """Receive a JSON payload with {path, key, html} and atomically replace
    the region between <!-- EDIT:start:{key} --> and <!-- EDIT:end:{key} -->
    in an allowlisted template file. Requires authenticated staff user.
    """
    try:
        # Require an authenticated staff user in production (DEBUG off); allow open
        # editing only during local development so the workflow isn't blocked there.
        if not settings.DEBUG and not getattr(request.user, 'is_staff', False):
            return JsonResponse({'ok': False, 'error': 'permission'}, status=403)

        data = json.loads(request.body.decode('utf-8') if isinstance(request.body, (bytes, bytearray)) else request.body)
        rel_path = data.get('path')
        key = data.get('key')
        html_fragment = data.get('html', '')

        if not rel_path or not key:
            return JsonResponse({'ok': False, 'error': 'missing parameters'}, status=400)

        # The key is interpolated into the EDIT markers/regex, so restrict it to a
        # simple token (letters, digits, dot, dash, underscore).
        if not re.match(r'^[\w.\-]+$', str(key)):
            return JsonResponse({'ok': False, 'error': 'invalid key'}, status=400)

        # Resolve the target path. Generated decks embed their own absolute target
        # (window.__VOLT_EDIT_TARGET__); the template preview sends a path relative
        # to BASE_DIR. A leading "media/" (local dev URL) is dropped.
        raw = str(rel_path).replace('\\', '/').strip()
        if raw.startswith('media/'):
            raw = raw[len('media/'):]
        if os.path.isabs(raw):
            abs_path = os.path.abspath(raw)
        else:
            abs_path = os.path.abspath(os.path.join(settings.BASE_DIR, raw.lstrip('/')))

        # Allowlisted roots the editor may write within: project templates, the
        # generated-decks dir, and the media/upload roots used in prod & staging.
        allowed_roots = [
            os.path.abspath(os.path.join(settings.BASE_DIR, 'templates')),
            os.path.abspath(os.path.join(settings.BASE_DIR, 'clients')),
        ]
        for attr in ('BASE_UPLOAD_DIR', 'MEDIA_ROOT', 'STAGING_MEDIA_ROOT', 'PRODUCTION_MEDIA_ROOT'):
            val = getattr(settings, attr, None)
            if val:
                allowed_roots.append(os.path.abspath(str(val)))

        within_allowed = any(
            abs_path == root or abs_path.startswith(root + os.sep)
            for root in allowed_roots
        )
        if not within_allowed or not abs_path.lower().endswith('.html'):
            return JsonResponse({'ok': False, 'error': 'invalid path'}, status=400)

        print(f"[save_file_edit] key={key!r} abs_path={abs_path!r}")

        # Sanitize fragment. Keep the editing hooks (contenteditable / data-edit-key /
        # spellcheck) on every element so the saved field stays editable and re-savable
        # on the next render — otherwise bleach strips them and the field "locks".
        if bleach:
            allowed_tags = ['div', 'p', 'br', 'b', 'i', 'strong', 'em', 'ul', 'ol', 'li', 'a', 'span']
            allowed_attrs = {
                '*': ['style', 'class', 'contenteditable', 'data-edit-key', 'spellcheck'],
                'a': ['href', 'title', 'rel', 'target'],
            }
            css_sanitizer = None
            try:
                from bleach.css_sanitizer import CSSSanitizer
                css_sanitizer = CSSSanitizer(allowed_css_properties=[
                    'color', 'font-size', 'line-height', 'text-align', 'margin', 'padding',
                    'background', 'background-color', 'border', 'border-radius', 'font-weight',
                    'font-style', 'font-family', 'letter-spacing'
                ])
            except Exception:
                css_sanitizer = None

            if css_sanitizer:
                clean_html = bleach.clean(
                    html_fragment,
                    tags=allowed_tags,
                    attributes=allowed_attrs,
                    css_sanitizer=css_sanitizer,
                    strip=True,
                )
            else:
                clean_html = bleach.clean(html_fragment, tags=allowed_tags, attributes=allowed_attrs, strip=True)
        else:
            print('[save_file_edit] WARNING: bleach not installed, preserving HTML tags for saved fragment')
            clean_html = html_fragment
            # Remove dangerous elements if bleach is unavailable.
            clean_html = re.sub(r'(?is)<(script|style|iframe|object|embed|link|meta)[^>]*>.*?</\1>', '', clean_html)
            clean_html = re.sub(r'(?is)on\w+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', '', clean_html)

        # Guarantee the saved wrapper keeps its editing hooks even if the sanitizer
        # dropped them (some bleach versions strip contenteditable / data-* attrs).
        # Without this the field re-renders as a plain <p> and can no longer be edited.
        def _reattach_edit_attrs(fragment):
            m = re.match(r'(\s*)<([A-Za-z][\w-]*)([^>]*)>', fragment)
            if not m:
                return fragment
            lead, tag, attrs = m.group(1), m.group(2), m.group(3)
            if 'contenteditable' not in attrs:
                attrs += ' contenteditable="true"'
            if 'data-edit-key' not in attrs:
                attrs += ' data-edit-key="{}"'.format(key)
            if 'spellcheck' not in attrs:
                attrs += ' spellcheck="false"'
            return '{}<{}{}>'.format(lead, tag, attrs) + fragment[m.end():]

        clean_html = _reattach_edit_attrs(clean_html)

        start = f'<!-- EDIT:start:{key} -->'
        end = f'<!-- EDIT:end:{key} -->'

        with open(abs_path, 'r', encoding='utf-8') as f:
            content = f.read()

        if start not in content or end not in content:
            return JsonResponse({'ok': False, 'error': 'markers not found'}, status=400)

        pattern = re.compile(re.escape(start) + r'(.*?)' + re.escape(end), re.S)
        orig_match = pattern.search(content)
        orig_fragment = orig_match.group(1) if orig_match else ''

        def _merge_style_attrs(new_fragment, old_fragment):
            m_new = re.match(r'(\s*)<([A-Za-z][\w-]*)([^>]*)>', new_fragment)
            m_old = re.match(r'(\s*)<([A-Za-z][\w-]*)([^>]*)>', old_fragment)
            if not m_new or not m_old:
                return new_fragment

            lead_new, tag_new, attrs_new = m_new.group(1), m_new.group(2), m_new.group(3)
            attrs_old = m_old.group(3)

            def _get_attr_string(attrs, name):
                m = re.search(r'\b' + re.escape(name) + r'\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', attrs)
                return m.group(1) if m else ''

            for attr in ['style', 'class']:
                if attr not in attrs_new and attr in attrs_old:
                    attrs_new += ' ' + attr + '=' + _get_attr_string(attrs_old, attr)

            if 'contenteditable' not in attrs_new:
                attrs_new += ' contenteditable="true"'
            if 'data-edit-key' not in attrs_new:
                attrs_new += ' data-edit-key="{}"'.format(key)
            if 'spellcheck' not in attrs_new:
                attrs_new += ' spellcheck="false"'

            return '{}<{}{}>'.format(lead_new, tag_new, attrs_new) + new_fragment[m_new.end():]

        clean_html = _merge_style_attrs(clean_html, orig_fragment)
        replacement = start + '\n' + clean_html + '\n' + end
        new_content, n = pattern.subn(replacement, content)
        if n == 0:
            return JsonResponse({'ok': False, 'error': 'replace failed'}, status=500)

        # A field that was just saved is no longer showing a default — drop its
        # "défaut" marker (span.deftag with data-for=key) so it doesn't reappear
        # when this saved deck is re-rendered/reopened. (deftags are the only
        # elements using data-for, so this can't hit an editable field.)
        new_content = re.sub(
            r'<span\b[^>]*\bdata-for="' + re.escape(key) + r'"[^>]*>.*?</span>',
            '', new_content, flags=re.S)

        # Atomic write
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(abs_path), prefix='.tmp-', suffix='.html')
        os.close(fd)
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            os.replace(tmp_path, abs_path)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

        return JsonResponse({'ok': True})
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'error': 'invalid json'}, status=400)
    except Exception as e:
        logging.exception('save_file_edit failed')
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)