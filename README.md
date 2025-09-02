#!/bin/bash
sudo apt update
sudo apt install python3 python3-venv python3-pip wkhtmltopdf libpango-1.0-0 libgdk-pixbuf2.0-0 libcairo2 -y

cd ~/api
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python manage.py makemigrations
python manage.py migrate
python manage.py runserver
