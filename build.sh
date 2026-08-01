#!/bin/bash
echo "Installing dependencies..."
pip install -r requirements.txt

echo "Running Migrations..."
python manage.py migrate

echo "Collecting Static Files..."
python manage.py collectstatic --noinput

echo "Creating Superuser..."
python manage.py shell -c "from django.contrib.auth.models import User; User.objects.filter(username='admin').exists() or User.objects.create_superuser('admin', 'manoj@acs.com', 'acs@23456')"

echo "Build completed!"