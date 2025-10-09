from django.urls import path
from . import views

urlpatterns = [
    path('api/volt.html-consulting-presentation/', views.volt_consulting_presentation, name='volt_consulting_presentation'),
    path('api/volt.html-consulting-presentation-Electricity/', views.volt_consulting_presentation_Electricitry, name='volt_consulting_presentation_Electricity'),
]