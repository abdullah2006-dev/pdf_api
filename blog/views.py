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
        # Parse incoming JSON data
        if request.content_type == 'application/json':
            data = json.loads(request.body)
        else:
            data = request.POST.dict()

        chart_base64 = None
        if "xAxis" in data and "series" in data:
            try:
                dates = pd.to_datetime(data["xAxis"][0]["data"], format="%d/%m/%Y")
                plt.figure(figsize=(12, 6))
                colors = ["black", "royalblue", "green", "red"]

                for idx, series in enumerate(data["series"]):
                    y = np.array(series["data"], dtype=np.float64)
                    plt.plot(dates[:len(y)], y, label=series.get("label", f"Series {idx + 1}"),
                             color=colors[idx % len(colors)])

                energy_type = data.get("comparatifClientHistoryPdfDto", {}).get("energyType", "").upper()

                # 🔹 Chart title based on energyType
                if energy_type == "GAS":
                    chart_title = "Évolution Gaz"
                elif energy_type == "ELECTRICITY":
                    chart_title = "Évolution Électricité"
                else:
                    chart_title = "Évolution des Prix"  # default fallback

                # Plot configuration
                plt.xlabel("")
                plt.ylabel("Prix €/MWh")
                plt.title(chart_title)

                # Update legend labels to use proper French
                legend_labels = []
                for series in data["series"]:
                    original_label = series.get("label", "")
                    # Fix common French text issues
                    if "prix marche anni" in original_label.lower():
                        corrected_label = original_label.replace("prix marche anni", "Prix marché année")
                        legend_labels.append(corrected_label)
                    else:
                        legend_labels.append(original_label)

                plt.legend(legend_labels, loc="upper right", frameon=False)

                ax = plt.gca()
                ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=[1, 4, 7, 10], bymonthday=1))
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%d-%m-%Y"))
                plt.xticks(rotation=45)
                plt.grid(True, linestyle="--", alpha=0.6)
                plt.tight_layout()

                buf = io.BytesIO()
                plt.savefig(buf, format="png", dpi=300)
                plt.close()
                buf.seek(0)
                chart_base64 = f"data:image/png;base64,{base64.b64encode(buf.read()).decode('utf-8')}"
            except Exception:
                chart_base64 = None

        def build_comparatif_dto(comparatif):
            dto = {
                "title": data.get("contexte_title", "Contexte global"),
                "createdOn": comparatif.get("createdOn"),
                "energyType": comparatif.get("energyType"),
            }

            if dto.get("energyType") == "GAS":
                dto["pce"] = comparatif.get("pce")
                dto["gasProfile"] = comparatif.get("gasProfile")
                dto["routingRate"] = comparatif.get("routingRate")
                dto["fourgas"] = comparatif.get("fourgas")
            elif dto.get("energyType") == "ELECTRICITY":
                dto["pdl"] = comparatif.get("pdl")
                dto["segmantation"] = comparatif.get("segmantation")
                dto["fourelectricity"] = comparatif.get("fourelectricity")

                # Dynamic array of objects
            comparatif_rate = comparatif.get("comparatifRate", [])
            for item in comparatif_rate:
                if "partnerPhoto" not in item:
                    # default partner photo if not provided
                    item["partnerPhoto"] = {"path": build_static_url(request, "image/default.png")}

            dto["comparatifRate"] = comparatif_rate

            return dto

        comparatif = data.get("comparatifClientHistoryPdfDto", {})
        comparatif_dto = build_comparatif_dto(comparatif)

        # convert long -> date
        dt = datetime.fromtimestamp(data["date"] / 1000.0)
        formatted_date = dt.strftime("%d/%m/%Y")

        # Hard-coded default data (will be used if keys missing in request)
        presentation_data = {
            "title": data.get("title", "VOLT CONSULTING - Energy Services Presentation"),
            "date": formatted_date,
            "document_type": data["document_type"],
            "clientSociety": data["clientSociety"],
            "clientSiret": data["clientSiret"],
            "clientFirstName": data["clientFirstName"],
            "clientLastName": data["clientLastName"],
            "clientEmail": data["clientEmail"],
            "clientPhoneNumber": data["clientPhoneNumber"],
            "black": data["black"],
            "black1": data["black1"],
            "black3": data["black3"],
             "image": {
                **data.get("images", {}),
                "chart": chart_base64  # fully dynamic
            },
            "images": data.get("images", {
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
                "last": data.get("last",build_static_url(request, "image/circle-black-removebg-preview.png")),
                "double": data.get("double", build_static_url(request, "image/double-removebg-preview.png")),
            }),
            "company_presentation": {
                "title": data.get("company_title", "L'ÉNERGIE DE VOTRE<br> ENTREPRISE, NOTRE EXPERTISE"),
                "description": data.get("description",
                    "<b>Volt Consulting</b> est votre partenaire de confiance dans la <b>gestion énergétique B2B</b>. "
                    "Notre proximité et notre engagement nous permettent de comprendre vos besoins <b>spécifiques</b>. "
                    "Nous vous accompagnons dans le choix du fournisseur d'énergie optimal, tout en maximisant l'efficacité énergétique. "
                    "Nos réussites parlent d'elles-mêmes, avec des <b>économies mesurables</b> pour nos clients."),
                "quote": data.get("quote", "Faites équipe avec nous pour un avenir énergétique plus efficace.")
            },
            "comparatifClientHistoryPdfDto": comparatif_dto,
            "budget_global": {
                "title": data.get("budget_title", "BUDGET GLOBAL"),
                "subtitle": data.get("budget_subtitle", "La synthèse")
            },
            "tender_results": {
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
            },
            "comparison_table": {
                "last_text": data.get("comparison_note",
                                      "Ce comparatif tient compte de votre consommation au cours des douze derniers mois. "
                                      "Les prix mentionnés sont variables au jour de la consultation, étant donné qu'ils sont sujets à la fluctuation des prix sur le marché de l'énergie. "
                                      "Ils sont non contractuels. Il est important de noter que ce comparatif se base uniquement sur votre historique de consommation et ne prend pas en considération vos besoins énergétiques futurs."),
                "section_title": data.get("section_title", "Offre Actuelle / de renouvellement"),
                "labels": data.get("labels", [
                    "Budget Énergétique <br>en €/an", "Distribution <br>en €/an", "Taxes <br>en €/an",
                    "Abonnement <br>en €/an", "CEE <br>en €/an", "CTA <br>en €/an", "Budget HTVA <br>en €/an"
                ])
            },
            "tender_table": {
                "title": data.get("tender_table_title", "RÉSULTAT DE L’APPEL D’OFFRE"),
                "columns": data.get("columns", [
                    "Fournisseur", "Molécule €/MWh", "Abonnement €/mois",
                    "CEE €/MWh", "CTA €/an", "TICGN €/MWh", "TOTAL €/an"
                ]),
            },
            "change_section": {
                "title": data.get("change_title", "LE CHANGEMENT SANS CONTRAINTE"),
                "text": data.get("change_text",
                                 "Contrairement à la téléphonie, rien ne change sur<br> "
                                 "l'installation. Vous conservez le même compteur, le<br> "
                                 "même numéro de dépannage en cas de problème. "
                                 "C'est <br> toujours GRDF & ENEDIS qui s'occupe de la relève du<br> compteur. "
                                 "Changer de fournisseur, c'est gratuit!"),
                "quote": data.get("change_quote", "Les équipes de VOLT CONSULTING <br> peuvent vous accompagner sur toute<br> cette partie administrative")
            },
            "contact_info": {
                "company_name": data.get("company_name", "VOLT CONSULTING"),
                "phone": data.get("phone", "01 87 66 70 43"),
                "email": data.get("email", "contact@volt-consulting.fr"),
                "address": data.get("address", "8 Place Hoche - 78000 Versailles")
            }
        }


        # Render HTML template with data
        html_content = render_to_string("volt.html", {"data": presentation_data})

        pdf_dir = os.path.join(settings.MEDIA_ROOT, "pdfs")
        os.makedirs(pdf_dir, exist_ok=True)
        pdf_filename = f"volt_{uuid.uuid4().hex}.pdf"
        pdf_path = os.path.join(pdf_dir, pdf_filename)

        # ✅ IMPROVED CSS FOR PDF - Matches HTML exactly
        css = CSS(string="""
                    @page { 
                        size: A1 landscape; 
                        margin: 0.0cm; 
                    }
                """)

        # ✅ PDF Generation with better settings for layout preservation
        HTML(string=html_content).write_pdf(
            pdf_path,
            stylesheets=[css],
            zoom=0.8,  # Reduced zoom for better fit
            optimize_images=True,
            presentational_hints=True,
            font_config=None  # Use system fonts
        )

        pdf_url = request.build_absolute_uri(os.path.join(settings.MEDIA_URL, "pdfs", pdf_filename))
        return JsonResponse({
            "status": "success",
            "pdf_url": pdf_url,
            "message": "PDF generated successfully"
        })

    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({
            "status": "error",
            "message": f"An error occurred: {str(e)}",
            "html_content": html_content if 'html_content' in locals() else ""
        }, status=500)



def build_static_url(request, path):
    return request.build_absolute_uri(settings.STATIC_URL + path)




