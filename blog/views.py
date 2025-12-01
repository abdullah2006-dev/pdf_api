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
        pdf_url, pdf_filename = generate_pdf(html_content, request, data)

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
    # 🔹 Ensure chartDataDto exists
    if "chartDataDto" not in data or not data["chartDataDto"]:
        raise ValueError("Missing or empty field: chartDataDto")

    chart_data = data["chartDataDto"]

    # 🔹 Validate xAxis and series
    if "xAxis" not in chart_data or not chart_data["xAxis"]:
        raise ValueError("Missing or empty field: xAxis")

    if "series" not in chart_data or not chart_data["series"]:
        raise ValueError("Missing or empty field: series")

    # 🔹 Validate xAxis data
    if "data" not in chart_data["xAxis"][0] or not chart_data["xAxis"][0]["data"]:
        raise ValueError("Missing or empty field: xAxis[0].data")

    try:
        dates = pd.to_datetime(chart_data["xAxis"][0]["data"], format="%d/%m/%Y")
    except Exception as e:
        raise ValueError(f"Invalid date format in xAxis data: {e}")

    plt.figure(figsize=(12, 7))
    colors = ["black", "royalblue", "green", "red"]

    for idx, series in enumerate(chart_data["series"]):
        if "data" not in series or not series["data"]:
            raise ValueError(f"Missing or empty field: series[{idx}].data")

        try:
            y = np.array(series["data"], dtype=np.float64)
        except Exception as e:
            raise ValueError(f"Invalid numeric data in series[{idx}]: {e}")

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
    created_on_raw = comparatif.get("createdOn")
    if not created_on_raw:
        raise ValueError("Missing required field: createdOn")

    try:
        dt = datetime.fromtimestamp(created_on_raw / 1000.0)  # convert ms → seconds
        created_on = dt.strftime("%d/%m/%Y")  # format date
    except Exception as e:
        raise ValueError(f"Invalid createdOn value: {e}")

    dto = {
        "title": data.get("contexte_title", "Contexte global"),
        "createdOn": created_on,
        "energyType": comparatif.get("energyType"),
    }

    energy_type = dto.get("energyType")

    if energy_type == "GAS":
        required_gas_fields = ["pce", "gasProfile", "routingRate"]

        # GAS ke fields update karna
        dto.update({
            "pce": comparatif.get("pce"),
            "gasProfile": comparatif.get("gasProfile"),
            "routingRate": comparatif.get("routingRate"),
            "volumeAnnual": comparatif.get("volumeAnnual"),
            "ratioHTVA": comparatif.get("ratioHTVA"),
            "differenceHTVA": comparatif.get("differenceHTVA"),
        })

        # Validation: GAS ke saare required fields hone chahiye
        for field in required_gas_fields:
            if not dto.get(field):
                raise ValueError(f"Missing required GAS field: {field}")

        # Agar ELECTRICITY ke fields mistakenly bhej diye gaye hain toh error
        forbidden_electricity_fields = ["pdl", "segmentation"]
        for field in forbidden_electricity_fields:
            if comparatif.get(field):
                raise ValueError(f"Field '{field}' is not allowed for GAS energyType")

    else:
        raise ValueError("Invalid or missing energyType. Must be 'GAS'.")

    # Comparatif rate validation
    comparatif_rate = comparatif.get("comparatifRates", [])

    dto["comparatifRates"] = comparatif_rate
    return dto


def render_html(presentation_data):
    print("Inside RenderHTML")
    return render_to_string("volt.html", {"data": presentation_data})


def generate_pdf(html_content, request, data):
    """Generate PDF and return its URL without unwanted pages."""
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
    relative_path = os.path.join("clients", str(data.get("clientId")), "comparatif")
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
    remove_pages = [1,3,5,7,9,11,13,15,17,19,21,23,25,27,29,31,33,35,37,39,41,43,35,37,39,41,43,45,47,49,51,53,55,57,59,61,63,65,67,69,71,73,75,77,79,81,83,85,87,89,91,93,95,97,99]

    reader = PdfReader(pdf_path)
    writer = PdfWriter()

    for i in range(len(reader.pages)):
        if i not in remove_pages:
            writer.add_page(reader.pages[i])

    with open(pdf_path, "wb") as f:
        writer.write(f)
    # ---------------------------------------------

    # Build public URL (mirrors saved path after /uploads/volt/)
    pdf_url = request.build_absolute_uri(
        os.path.join(base_url, "clients", str(data.get("clientId")), "comparatif", pdf_filename)
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

    return {
        "title": data.get("title", "VOLT CONSULTING - Energy Services Presentation"),
        "headingone": "APPEL D’OFFRE",
        "clientSociety": safe_value(data.get("clientSociety")),
        "clientSiret": safe_value(data.get("clientSiret")),
        "clientFirstName": safe_value(data.get("clientFirstName")),
        "clientLastName": safe_value(data.get("clientLastName")),
        "clientEmail": safe_value(data.get("clientEmail")),
        "clientPhoneNumber": safe_value(data.get("clientPhoneNumber")),
        "black": (
            safe_value(comparatif_dto.get("ratioHTVA")) + "%" 
            if safe_value(comparatif_dto.get("ratioHTVA")) != "" 
            else ""
        ),
        "black1": (
            safe_value(comparatif_dto.get("differenceHTVA")) + "€" 
            if safe_value(comparatif_dto.get("differenceHTVA")) != "" 
            else ""
        ),
        "black3": "économisé/an",
        "image": build_image_section(data, chart_base64),
        "images": build_images(data, request),
        "company_presentation": build_company_presentation(data),
        "comparatifClientHistoryPdfDto": comparatif_dto,
        "budget_global": build_budget_section(data),
        "tender_results": build_tender_results(data),
        "comparison_table": build_comparison_table(data),
        "tender_table": build_tender_table(data),
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


def build_tender_table(data):
    """Tender table section."""
    print("Inside BuildTenderTable")
    return {
        "title": data.get("tender_table_title", "RÉSULTAT DE L’APPEL D’OFFRE"),
        "columns": data.get("columns", [
            "Fournisseur", "Molécule €/MWh", "Abonnement €/mois",
            "CEE €/MWh", "CTA €/an", "TICGN €/MWh", "TOTAL €/an"
        ]),
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
    return {
        "company_name": data.get("company_name", "VOLT CONSULTING"),
        "phone": data.get("phone", "01 87 66 70 43"),
        "email": data.get("email", "contact@volt-consulting.fr"),
        "address": data.get("address", "8 Place Hoche - 78000 Versailles")
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
        enedis_chart_base64 = generate_enedis_chart(data)

        # 3️⃣ Build Comparatif DTO
        comparatif = data.get("comparatifClientHistoryPdfDto", {})
        comparatif_dto = build_comparatif_dto_Electricity(comparatif, request, data)

        # 4️⃣ Build Presentation Data
        presentation_data = build_presentation_data_Electricity(data, enedis_chart_base64, chart_base64, comparatif_dto, request)

        # 5️⃣ Render HTML
        html_content = render_html_Elecricity(presentation_data)

        # 6️⃣ Generate PDF
        pdf_url, pdf_filename = generate_pdf(html_content, request, data)

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


def generate_enedis_chart(data):
    """Generate Enedis-style stacked bar chart from chartDataDto data."""
    print("Inside GenerateChart - Enedis Style")

    # 🔹 Extract chartDataDto
    if "chartDataDto" not in data or not data["chartDataDto"]:
        raise ValueError("Missing or empty field: chartDataDto")
    chart_data = data["chartDataDto"]

    # 🔹 Parse dates from xAxis
    try:
        dates = chart_data["xAxis"][0]["data"]
        date_labels = []
        for d in dates:
            try:
                date_labels.append(datetime.strptime(str(d), "%d/%m/%Y").strftime("%d/%m/%Y"))
            except Exception:
                date_labels.append(str(d))
    except Exception as e:
        print(f"Error parsing dates: {e}")
        date_labels = [f"Period {i + 1}" for i in range(12)]

    # 🔹 Prepare figure with Enedis style
    fig, ax = plt.subplots(figsize=(13, 7))
    fig.patch.set_facecolor('#f5f5f5')
    ax.set_facecolor('white')

    # 🔹 Define Enedis colors (cycle if series > 4)
    enedis_colors = ['#b8c5d6', '#1e4d7b', '#d4c34a', '#f5a623']

    # 🔹 Series data from chartDataDto
    series_data = chart_data["series"]

    x = np.arange(len(date_labels))
    width = 0.6
    bottom = np.zeros(len(date_labels))

    # 🔹 Plot each series as stacked bars
    for idx, series in enumerate(series_data):
        y = np.array(series["data"], dtype=np.float64)

        # Ensure correct length
        if len(y) != len(date_labels):
            print(f"Warning: Series {idx} length mismatch. Padding/truncating.")
            if len(y) < len(date_labels):
                y = np.pad(y, (0, len(date_labels) - len(y)), 'constant')
            else:
                y = y[:len(date_labels)]

        label = series.get("label", f"Series {idx + 1}")
        color = enedis_colors[idx % len(enedis_colors)]

        ax.bar(x, y, width, label=label, bottom=bottom, color=color)
        bottom += y

    # 🔹 Customize axes
    ax.set_xlabel('')
    ax.set_ylabel('€ consommation', fontsize=10, color='#666')
    ax.set_xticks(x)
    ax.set_xticklabels(date_labels, rotation=0, ha='center', fontsize=9)

    ax.yaxis.grid(True, linestyle='-', alpha=0.3, color='#ddd')
    ax.set_axisbelow(True)

    # 🔹 Remove spines
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#ddd')
    ax.spines['bottom'].set_color('#ddd')

    # 🔹 Y-axis formatting
    y_max = bottom.max()
    if y_max > 0:
        ax.set_ylim(0, y_max * 1.1)
        ax.set_yticks(np.linspace(0, y_max * 1.1, 5))
    else:
        ax.set_ylim(0, 10)

    # 🔹 Legend styling
    ax.legend(
        loc='upper center',
        bbox_to_anchor=(0.5, -0.08),
        ncol=len(series_data),
        frameon=False,
        fontsize=9,
        columnspacing=2,
        handlelength=1.5,
        handleheight=1.5
    )

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.15)

    # 🔹 Convert to base64
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=300, bbox_inches='tight', facecolor='#f5f5f5')
    plt.close()
    buf.seek(0)

    return f"data:image/png;base64,{base64.b64encode(buf.read()).decode('utf-8')}"


def build_presentation_data_Electricity(data, enedis_chart_base64, chart_base64, comparatif_dto, request):
    print("Inside BuildPresentationData")

    # Updated helper function
    def safe_value(value):
        if value is None:
            return ""
        str_val = str(value).strip().lower()
        if str_val == "" or str_val == "none":
            return ""
        return str(value)

    return {
        "title": data.get("title", "VOLT CONSULTING - Energy Services Presentation"),
        "headingone": "APPEL D’OFFRE",
        "clientSociety": safe_value(data.get("clientSociety")),
        "clientSiret": safe_value(data.get("clientSiret")),
        "clientFirstName": safe_value(data.get("clientFirstName")),
        "clientLastName": safe_value(data.get("clientLastName")),
        "clientEmail": safe_value(data.get("clientEmail")),
        "clientPhoneNumber": safe_value(data.get("clientPhoneNumber")),
        "black": (
            safe_value(comparatif_dto.get("ratioHTVA")) + "%"
            if safe_value(comparatif_dto.get("ratioHTVA")) != ""
            else ""
        ),
        "black1": (
            safe_value(comparatif_dto.get("differenceHTVA")) + "€"
            if safe_value(comparatif_dto.get("differenceHTVA")) != ""
            else ""
        ),
        "black3": "économisé/an",
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
    created_on_raw = comparatif.get("createdOn")
    if not created_on_raw:
        raise ValueError("Missing required field: createdOn")

    try:
        dt = datetime.fromtimestamp(created_on_raw / 1000.0)  # convert ms → seconds
        created_on = dt.strftime("%d/%m/%Y")  # format date
    except Exception as e:
        raise ValueError(f"Invalid createdOn value: {e}")

    dto = {
        "title": data.get("contexte_title", "Contexte global"),
        "title2": data.get("enedis_title2", "Votre Consommation relevée par"),
        "createdOn": created_on,
        "energyType": comparatif.get("energyType"),
    }

    energy_type = dto.get("energyType")

    if energy_type == "ELECTRICITY":
        required_electricity_fields = ["pdl", "segmentation", "volumeAnnual"]

        dto.update({
            "pdl": comparatif.get("pdl"),
            "segmentation": comparatif.get("segmentation"),
            "tarifType": comparatif.get("tarifType"),
            "volumeAnnual": comparatif.get("volumeAnnual"),
            "ratioHTVA": comparatif.get("ratioHTVA"),
            "differenceHTVA": comparatif.get("differenceHTVA"),
        })

        for field in required_electricity_fields:
            if not dto.get(field):
                raise ValueError(f"Missing required ELECTRICITY field: {field}")

    else:
        raise ValueError("Invalid or missing energyType. Must be 'ELECTRICITY'.")

    comparatif_rate = comparatif.get("comparatifRates", [])

    dto["comparatifRates"] = comparatif_rate
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
        energy_type = comparatif_dto.get("energyType", "ELECTRICITY")
        segmentation = comparatif_dto.get("segmentation", "")
        tarif_type = comparatif_dto.get("tarifType", "")

        # Convert to uppercase and strip whitespace for case-insensitive comparison
        energy_type_upper = energy_type.strip().upper()
        segmentation_upper = segmentation.strip().upper()
        tarif_type_upper = tarif_type.strip().upper()
        print(energy_type_upper, segmentation_upper, tarif_type_upper)

        if energy_type_upper == "ELECTRICITY":
            # Define segmentation to columns6 mapping
            segmentation_mapping = {
                "C1": ["HPH", "HCH", "HPE", "HCE", "POINTE"],
                "C2": ["HPH", "HCH", "HPE", "HCE", "POINTE"],
                "C3": ["HPH", "HCH", "HPE", "HCE", "POINTE"],
                "C4": ["HPH", "HCH", "HPE", "HCE"],
            }

            segmentation_mapping1 = {
                "C1": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh", "POINTE <br> €/MWh"],
                "C2": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh", "POINTE <br> €/MWh"],
                "C3": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh", "POINTE <br> €/MWh"],
                "C4": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh"],
            }
            
            # Check for C5 with specific tarif types
            if segmentation_upper == "C5":
                tarif_mapping = {
                    "QUATRE": ["HPH", "HCH", "HPE", "HCE"],
                    "DOUBLE": ["HP", "HC"],
                    "BASE": ["BASE"]
                }

                tarif_mapping1 = {
                    "QUATRE": ["HPH <br> €/MWh", "HCH <br> €/MWh", "HPE <br> €/MWh", "HCE <br> €/MWh"],
                    "DOUBLE": ["HP <br> €/MWh", "HC <br> €/MWh"],
                    "BASE": ["BASE <br> €/MWh"]
                }
                columns6 = tarif_mapping.get(tarif_type_upper, ["HP", "HC"])  # default to HP/HC
                
                # Get the base columns without "Fournisseur"
                base_columns = tarif_mapping1.get(tarif_type_upper, ["HP <br> €/MWh", "HC <br> €/MWh"])
                
                # Add "Fournisseur" to columns (but not to columns1)
                columns = ["Fournisseur"] + base_columns
                columns1 = base_columns  # columns1 doesn't get "Fournisseur"
            else:
                # Use mapping for other segmentations
                columns6 = segmentation_mapping.get(segmentation_upper, ["HP", "HC"])  # default
                
                # Get the base columns without "Fournisseur"
                base_columns = segmentation_mapping1.get(segmentation_upper, ["HP <br> €/MWh", "HC <br> €/MWh"])
                
                # Add "Fournisseur" to columns (but not to columns1)
                columns = ["Fournisseur"] + base_columns
                columns1 = base_columns  # columns1 doesn't get "Fournisseur"

    return {
        "title": data.get("tender_table_title", "RÉSULTAT DE L'APPEL D'OFFRE"),
        "columns": columns if columns else ["Fournisseur", "HP <br> €/MWh", "HC <br> €/MWh"],  # Fallback
        "columns1": columns1 if columns1 else ["HP <br> €/MWh", "HC <br> €/MWh"],  # Fallback
        "columns2": data.get("columns2", ["CEE <br> €/MWh"]),
        "columns3": data.get("columns3", ["TABO <br> €/an"]),
        "columns4": data.get("columns4", [
            "Puissances souscrites KVA", "Consommation MWh", "Total"
        ]),
        "columns5": data.get("columns5", [
            "Compteu", "Déb.contrat"
        ]),
        "columns6": columns6 if columns6 else ["HP", "HC"],  # Fallback
        "columns7": data.get("columns7", [
            "MWh / an"
        ]),
    }

def enedis_Chart(comparatif_dto):
    """Provide dynamic Enedis rate information based on segmentation and tarif type."""
    
    # Extract data with case-insensitive handling
    energy_type = comparatif_dto.get("energyType", "ELECTRICITY")
    segmentation = comparatif_dto.get("segmentation", "")
    tarif_type = comparatif_dto.get("tarifType", "")
    
    # Convert to uppercase for consistent comparison
    energy_type_upper = energy_type.strip().upper()
    segmentation_upper = segmentation.strip().upper()
    tarif_type_upper = tarif_type.strip().upper()
    
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
    if energy_type_upper == "ELECTRICITY":
        if segmentation_upper in ["C1", "C2", "C3"]:
            # C1, C2, C3: HPE, HPH, HCE, HCH, POINTE
            base_response.update({
                "enedis_rate_hph": comparatif_dto.get("hph", "-"),
                "enedis_rate_hch": comparatif_dto.get("hch", "-"),
                "enedis_rate_hpe": comparatif_dto.get("hpe", "-"),
                "enedis_rate_hce": comparatif_dto.get("hce", "-"),
                "enedis_rate_pointe": comparatif_dto.get("pte", "-"),

                "enedis_rate_puissance_hph": comparatif_dto.get("hph", "-"),
                "enedis_rate_puissance_hch": comparatif_dto.get("hch", "-"),
                "enedis_rate_puissance_hpe": comparatif_dto.get("hpe", "-"),
                "enedis_rate_puissance_hce": comparatif_dto.get("hce", "-"),
                "enedis_rate_puissance_pointe": comparatif_dto.get("pte", "-"),
            })
        elif segmentation_upper == "C4" or (segmentation_upper == "C5" and tarif_type_upper == "QUATRE"):
            # C4 or C5 QUATRE: HPE, HPH, HCE, HCH
            base_response.update({
                "enedis_rate_hph": comparatif_dto.get("hph", "-"),
                "enedis_rate_hch": comparatif_dto.get("hch", "-"),
                "enedis_rate_hpe": comparatif_dto.get("hpe", "-"),
                "enedis_rate_hce": comparatif_dto.get("hce", "-"),

                "enedis_rate_puissance_hph": comparatif_dto.get("hph", "-"),
                "enedis_rate_puissance_hch": comparatif_dto.get("hch", "-"),
                "enedis_rate_puissance_hpe": comparatif_dto.get("hpe", "-"),
                "enedis_rate_puissance_hce": comparatif_dto.get("hce", "-"),
            })
        elif segmentation_upper == "C5" and tarif_type_upper == "BASE":
            # C5 BASE: BASE only
            base_response.update({
                "enedis_rate_base": comparatif_dto.get("base", "-"),
                "enedis_rate_puissance_base": comparatif_dto.get("base", "-"),
            })
        elif segmentation_upper == "C5" and tarif_type_upper == "DOUBLE":
            # C5 DOUBLE: HP, HC
            base_response.update({
                "enedis_rate_hp": comparatif_dto.get("hp", "-"),
                "enedis_rate_hc": comparatif_dto.get("hc", "-"),

                "enedis_rate_puissance_hp": comparatif_dto.get("hp", "-"),
                "enedis_rate_puissance_hc": comparatif_dto.get("hc", "-"),
            })
        else:
            # Default: HP, HC
            base_response.update({
                "enedis_rate_hp": comparatif_dto.get("hp", "-"),
                "enedis_rate_hc": comparatif_dto.get("hc", "-"),

                "enedis_rate_puissance_hp": comparatif_dto.get("hp", "-"),
                "enedis_rate_puissance_hc": comparatif_dto.get("hc", "-"),
            })
    
    return base_response