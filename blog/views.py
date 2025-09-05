from django.shortcuts import render
from django.conf import settings
import json, re, io, base64
from django.http import HttpResponse, JsonResponse
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
import uuid
import pdfkit
from django.conf import settings
from django.http import JsonResponse
from weasyprint import HTML, CSS
from datetime import datetime
from django.templatetags.static import static


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
        # required_gas_fields = ["pce", "gasProfile", "routingRate", "fourgas"]
        required_gas_fields = ["pce", "gasProfile", "routingRate"]

        # GAS ke fields update karna
        dto.update({
            "pce": comparatif.get("pce"),
            "gasProfile": comparatif.get("gasProfile"),
            "routingRate": comparatif.get("routingRate"),
            # "fourgas": comparatif.get("fourgas"),
        })

        # Validation: GAS ke saare required fields hone chahiye
        for field in required_gas_fields:
            if not dto.get(field):
                raise ValueError(f"Missing required GAS field: {field}")

        # Agar ELECTRICITY ke fields mistakenly bhej diye gaye hain toh error
        # forbidden_electricity_fields = ["pdl", "segmentation", "fourelectricity"]
        forbidden_electricity_fields = ["pdl", "segmentation"]
        for field in forbidden_electricity_fields:
            if comparatif.get(field):
                raise ValueError(f"Field '{field}' is not allowed for GAS energyType")

    elif energy_type == "ELECTRICITY":
        # required_electricity_fields = ["pdl", "segmentation", "fourelectricity"]
        required_electricity_fields = ["pdl", "segmentation"]

        # ELECTRICITY ke fields update karna
        dto.update({
            "pdl": comparatif.get("pdl"),
            "segmentation": comparatif.get("segmentation"),
            # "fourelectricity": comparatif.get("fourelectricity"),
        })

        # Validation: ELECTRICITY ke saare required fields hone chahiye
        for field in required_electricity_fields:
            if not dto.get(field):
                raise ValueError(f"Missing required ELECTRICITY field: {field}")

        # Agar GAS ke fields mistakenly bhej diye gaye hain toh error
        # forbidden_gas_fields = ["pce", "gasProfile", "routingRate", "fourgas"]
        forbidden_gas_fields = ["pce", "gasProfile", "routingRate"]
        
        for field in forbidden_gas_fields:
            if comparatif.get(field):
                raise ValueError(f"Field '{field}' is not allowed for ELECTRICITY energyType")

    else:
        raise ValueError("Invalid or missing energyType. Must be 'GAS' or 'ELECTRICITY'.")

    # Comparatif rate validation
    comparatif_rate = comparatif.get("comparatifRates", [])

    required_rate_fields = [
        "partnerPhoto",
        # "rate2",
        "abonnement",
        "partCee",
        "cta",
        "ticgn",
        # "rate3",
        # "rate4",
        # "rate5",
        # "rate6",
        # "rate7",
    ]

    # for idx, item in enumerate(comparatif_rate, start=1):
    #     for field in required_rate_fields:
    #         if field not in item or item[field] in [None, ""]:
    #             raise ValueError(f"Missing or empty field '{field}' in comparatifRates item {idx}")

    dto["comparatifRates"] = comparatif_rate
    return dto


def render_html(presentation_data):
    print("Inside RenderHTML")
    return render_to_string("volt.html", {"data": presentation_data})


def generate_pdf(html_content, request, data):
    """Generate PDF and return its URL."""
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

    # Save PDF
    css = CSS(string="""@page { size: A1 landscape; margin: 0.0cm; }""")
    HTML(string=html_content).write_pdf(
        pdf_path,
        stylesheets=[css],
        zoom=0.8,
        optimize_images=True,
        presentational_hints=True,
        font_config=None
    )

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
    abs_path = os.path.join(settings.STATICFILES_DIRS[0], path)
    return f"file://{abs_path}"

def build_presentation_data(data, chart_base64, comparatif_dto, request):
    print("Inside BuildPresentationData")
    return {
        "title": data.get("title", "VOLT CONSULTING - Energy Services Presentation"),
        "document_type": comparatif_dto["energyType"],
        "clientSociety": data["clientSociety"],
        "clientSiret": data["clientSiret"],
        "clientFirstName": data["clientFirstName"],
        "clientLastName": data["clientLastName"],
        "clientEmail": data["clientEmail"],
        "clientPhoneNumber": data["clientPhoneNumber"],
        "black": "-36%",
        "black1": "21087&",
        "black3": "econmece&en",
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
        "Screenshot1": data.get("Screenshot1", build_static_url(request, "image/Screenshot_2025-08-18_135847-removebg-preview.png")),
        "Screenshot2": data.get("Screenshot2", build_static_url(request, "image/Screenshot_2025-08-18_131641-removebg-preview.png")),
        "black": data.get("black", build_static_url(request, "image/black-removebg-preview.png")),
        "zero": data.get("zero", build_static_url(request, "image/zero-removebg-preview.png")),
        "icon1": data.get("icon1", build_static_url(request, "image/icon-removebg-preview.png")),
        "whitee": data.get("whitee", build_static_url(request, "image/whiteee.png")),
        "con": data.get("con", build_static_url(request, "image/Screenshot_2025-08-18_164713-removebg-preview.png")),
        "con5": data.get("con5", build_static_url(request, "image/Screenshot_2025-08-18_164344-removebg-preview.png")),
        "Hmm": data.get("Hmm", build_static_url(request, "image/Hmm-removebg-preview.png")),
        "last": data.get("last", build_static_url(request, "image/circle-black-removebg-preview.png")),
        "double": data.get("double", build_static_url(request, "image/double-removebg-preview.png")),
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
        "quote": data.get("change_quote", "Les équipes de VOLT CONSULTING <br> peuvent vous accompagner sur toute<br> cette partie administrative")
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

