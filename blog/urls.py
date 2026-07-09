from django.urls import path
from . import views

urlpatterns = [
    path('api/volt.html-consulting-presentation/', views.volt_consulting_presentation, name='volt_consulting_presentation'),
    path('api/volt.html-consulting-presentation-Electricity/', views.volt_consulting_presentation_Electricitry, name='volt_consulting_presentation_Electricity'),

    path('api/comparatif-electricity/', views.energy_offer_summary, name='energy_offer_summary'),
    path('api/comparatif-gas/', views.comparatif_gas, name='comparatif_gas'),

    path('api/generate-market-analysis/', views.generate_market_analysis, name='generate_market_analysis'),
    path('api/generate-consumption-analysis/', views.generate_consumption_analysis, name='generate_consumption_analysis'),
    path('editor/save-file/', views.save_file_edit, name='save_file_edit'),
]