import re, io, base64
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.template.loader import render_to_string
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
from weasyprint import HTML, CSS
from datetime import datetime
from django.templatetags.static import static
from PyPDF2 import PdfReader, PdfWriter


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


def build_comparatif_dto(comparatif, request, data):
    print("Inside BuildComparatifDTO")

    # Validate and format createdOn
    created_on_raw = comparatif.get("createdOn")
    if not created_on_raw:
        raise ValueError("Missing required field: createdOn")

    try:
        dt = datetime.fromtimestamp(created_on_raw / 1000.0)  # convert ms → seconds
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
    
    # NEW: Sort regular_providers by coutHTVA in ascending order
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

    # NEW: Find the minimum coutHTVA from regular_providers
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
    green_row_used = False  # Flag for green row (only once after labels)

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

    # 2️⃣ Energy type suffix
    additional_text = "_elec" if energy_type.upper() == "ELECTRICITY" else "_gaz"

    # 3️⃣ Date part (YYYY-MM-DD)
    date_str = datetime.now().strftime("%Y-%m-%d")

    # 4️⃣ Final filename
    filename = f"Comparatif_{clean_society}{additional_text}_{date_str}.pdf"
    return filename


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
        "currentSupplierName": safe_value(comparatif_dto.get("currentSupplierName")),
        "currentContractExpiryDate": (
            datetime.fromtimestamp(comparatif_dto.get("currentContractExpiryDate") / 1000).strftime("%d/%m/%Y")
            if comparatif_dto.get("currentContractExpiryDate") else ""
        ),
        "black": black,
        "black1": black1,
        "black3": black3,
        "image": build_image_section(data, chart_base64),
        "images": build_images(data, request),
        "company_presentation": build_company_presentation(data),
        "comparatifClientHistoryPdfDto": comparatif_dto,
        "budget_global": build_budget_section(data),
        "tender_results": build_tender_results(data),
        "tender_table": build_tender_table(data),
        "comparison_table": build_comparison_table(data),
        "change_section": build_change_section(data),
        "contact_info": build_contact_info(data),
    }


def build_image_section(data, chart_base64):
    """Build image dictionary with dynamic chart."""
    return {**data.get("images", {}), "chart": chart_base64}


def build_images(data, request):
    """Build static & dynamic image paths."""
    print("Inside BuildImages")
    return data.get("images", {
        "left": build_static_url(request, "image/side2-removebg-preview.png"),
        "right": build_static_url(request, "image/side-removebg-preview.png"),
        "logo": build_static_url(request, "image/volt1-removebg-preview.png"),
        "side333": data.get("side3", build_static_url(request, "image/side333-removebg-preview.png")),
        "volt_image1": build_static_url(request, "image/volt_image1.png"),
        "icon": data.get("icon", build_static_url(request, "image/buld-removebg-preview.png")),
        "Screenshot1": data.get("Screenshot1",
                                build_static_url(request, "image/Screenshot_2025-08-18_135847-removebg-preview.png")),
        "Screenshot2": data.get("Screenshot2",
                                 build_static_url(request, "image/Screenshot_2025-08-18_131641-removebg-preview.png")),
        "black": build_static_url(request, "image/black-removebg-preview.png"),
        "zero": data.get("zero", build_static_url(request, "image/zero-removebg-preview.png")),
        "icon1": data.get("icon1", build_static_url(request, "image/icon-removebg-preview.png")),
        "whitee": data.get("whitee", build_static_url(request, "image/whiteee.png")),
        "con": data.get("con", build_static_url(request, "image/Screenshot_2025-08-18_164713-removebg-preview.png")),
        "con5": data.get("con5", build_static_url(request, "image/Screenshot_2025-08-18_164344-removebg-preview.png")),
        "Hmm": data.get("Hmm", build_static_url(request, "image/Hmm-removebg-preview.png")),
        "last": data.get("last", build_static_url(request, "image/circle-black-removebg-preview.png")),
        "double": data.get("double", build_static_url(request, "image/double-removebg-preview.png")),
        "enedis": data.get("enedis", build_static_url(request, "image/enedis-removebg-preview.png")),
    })


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
        "enedis_info": enedis_Chart(comparatif_dto)
    }


def build_comparatif_dto_Electricity(comparatif, request, data):
    print("Inside BuildComparatifDTO")

    # Validate and format createdOn
    created_on_raw = comparatif.get("createdOn")
    if not created_on_raw:
        raise ValueError("Missing required field: createdOn")

    try:
        dt = datetime.fromtimestamp(created_on_raw / 1000.0)
        created_on = dt.strftime("%d/%m/%Y")
    except Exception as e:
        raise ValueError(f"Invalid createdOn value: {e}")

    dto = {
        "title": data.get("contexte_title", "Contexte global"),
        "title2": data.get("enedis_title2", "Votre Consommation relevée par"),
        "createdOn": created_on,
        "energyType": comparatif.get("energyType"),
        "puissance": comparatif.get("puissance"),
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
    
    # NEW: Sort regular_providers by coutHTVA in ascending order
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

    # NEW: Find the minimum coutHTVA from regular_providers
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
    green_row_used = False  # Flag for green row (once after labels)

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

        # if energy_type_upper == "ELECTRICITY":
        #     # Define segmentation to columns6 mapping
        #     segmentation_mapping = {
        #         "C1": ["HPH", "HCH", "HPE", "HCE", "POINTE"],
        #         "C2": ["HPH", "HCH", "HPE", "HCE", "POINTE"],
        #         "C3": ["HPH", "HCH", "HPE", "HCE", "POINTE"],
        #         "C4": ["HPH", "HCH", "HPE", "HCE"],
        #     }

        #     segmentation_mapping1 = {
        #         "C1": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh", "POINTE <br> €/MWh"],
        #         "C2": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh", "POINTE <br> €/MWh"],
        #         "C3": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh", "POINTE <br> €/MWh"],
        #         "C4": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh"],
        #     }

        #     # Check for C5 with specific tarif types
        #     if segmentation_upper == "C5":
        #         tarif_mapping = {
        #             "QUATRE": ["HPH", "HCH", "HPE", "HCE"],
        #             "DOUBLE": ["HP", "HC"],
        #             "BASE": ["BASE"]
        #         }

        #         tarif_mapping1 = {
        #             "QUATRE": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh"],
        #             "DOUBLE": ["HP <br> €/MWh", "HC <br> €/MWh"],
        #             "BASE": ["BASE <br> €/MWh"]
        #         }

        #         # Safely get columns6 with fallback
        #         columns6 = tarif_mapping.get(tarif_type_upper, ["HP", "HC"])

        #         # Get the base columns without "Fournisseur"
        #         base_columns = tarif_mapping1.get(tarif_type_upper, ["HP <br> €/MWh", "HC <br> €/MWh"])

        #         # Add "Fournisseur" to columns (but not to columns1)
        #         columns = ["Fournisseur"] + base_columns
        #         columns1 = base_columns  # columns1 doesn't get "Fournisseur"
        #     else:
        #         # Use mapping for other segmentations
        #         columns6 = segmentation_mapping.get(segmentation_upper, ["HP", "HC"])

        #         # Get the base columns without "Fournisseur"
        #         base_columns = segmentation_mapping1.get(segmentation_upper, ["HP <br> €/MWh", "HC <br> €/MWh"])

        #         # Add "Fournisseur" to columns (but not to columns1)
        #         columns = ["Fournisseur"] + base_columns
        #         columns1 = base_columns  # columns1 doesn't get "Fournisseur"
        # else:
        #     # Default fallback for non-electricity or unknown energy types
        #     columns6 = ["HP", "HC"]
        #     columns = ["Fournisseur", "HP <br> €/MWh", "HC <br> €/MWh"]
        #     columns1 = ["HP <br> €/MWh", "HC <br> €/MWh"]

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
            dt = datetime.fromtimestamp(contract_start_date)
            formatted_date = dt.strftime("%d/%m/%Y")
        except (ValueError, TypeError, OSError):
            formatted_date = "-"

    # Base response with common fields
    base_response = {
        "enedis_rate_On": comparatif_dto.get("pdl", "-"),
        "contract_start_date": formatted_date,
        "enedis_rate_sum": comparatif_dto.get("sumOfAnnualRates", "-"),
    }

    # Apply exact same rules as build_tender_table_Electricity
    # if energy_type_upper == "ELECTRICITY":
    #     if segmentation_upper in ["C1", "C2", "C3"]:
    #         # C1, C2, C3: HPE, HPH, HCE, HCH, POINTE
    #         base_response.update({
    #             "enedis_rate_hph": comparatif_dto.get("hph", "-"),
    #             "enedis_rate_hch": comparatif_dto.get("hch", "-"),
    #             "enedis_rate_hpe": comparatif_dto.get("hpe", "-"),
    #             "enedis_rate_hce": comparatif_dto.get("hce", "-"),
    #             "enedis_rate_pointe": comparatif_dto.get("pte", "-"),

    #             "enedis_rate_puissance_hph": comparatif_dto.get("puissance", "-"),
    #             "enedis_rate_puissance_hch": comparatif_dto.get("puissance", "-"),
    #             "enedis_rate_puissance_hpe": comparatif_dto.get("puissance", "-"),
    #             "enedis_rate_puissance_hce": comparatif_dto.get("puissance", "-"),
    #             "enedis_rate_puissance_pointe": comparatif_dto.get("puissance", "-"),
    #         })
    #     elif segmentation_upper == "C4" or (segmentation_upper == "C5" and tarif_type_upper == "QUATRE"):
    #         # C4 or C5 QUATRE: HPE, HPH, HCE, HCH
    #         base_response.update({
    #             "enedis_rate_hph": comparatif_dto.get("hph", "-"),
    #             "enedis_rate_hch": comparatif_dto.get("hch", "-"),
    #             "enedis_rate_hpe": comparatif_dto.get("hpe", "-"),
    #             "enedis_rate_hce": comparatif_dto.get("hce", "-"),

    #             "enedis_rate_puissance_hph": comparatif_dto.get("puissance", "-"),
    #             "enedis_rate_puissance_hch": comparatif_dto.get("puissance", "-"),
    #             "enedis_rate_puissance_hpe": comparatif_dto.get("puissance", "-"),
    #             "enedis_rate_puissance_hce": comparatif_dto.get("puissance", "-"),
    #         })
    #     elif segmentation_upper == "C5" and tarif_type_upper == "BASE":
    #         # C5 BASE: BASE only
    #         base_response.update({
    #             "enedis_rate_base": comparatif_dto.get("base", "-"),
    #             "enedis_rate_puissance_base": comparatif_dto.get("puissance", "-"),
    #         })
    #     elif segmentation_upper == "C5" and tarif_type_upper == "DOUBLE":
    #         # C5 DOUBLE: HP, HC
    #         base_response.update({
    #             "enedis_rate_hp": comparatif_dto.get("hp", "-"),
    #             "enedis_rate_hc": comparatif_dto.get("hc", "-"),

    #             "enedis_rate_puissance_hp": comparatif_dto.get("puissance", "-"),
    #             "enedis_rate_puissance_hc": comparatif_dto.get("puissance", "-"),
    #         })
    #     else:
            # Default: HP, HC
            # base_response.update({
            #     "enedis_rate_hp": comparatif_dto.get("hp", "-"),
            #     "enedis_rate_hc": comparatif_dto.get("hc", "-"),

            #     "enedis_rate_puissance_hp": comparatif_dto.get("puissance", "-"),
            #     "enedis_rate_puissance_hc": comparatif_dto.get("puissance", "-"),
            # })

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
