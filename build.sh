#!/bin/bash
echo "Installing dependencies..."
pip install -r requirements.txt

echo "Running Migrations..."
python manage.py migrate

echo "Collecting Static Files..."
python manage.py collectstatic --noinput

echo "Build completed!"