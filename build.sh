#!/usr/bin/env bash
# exit on error
set -o errexit

echo "Installing dependencies..."
pip install -r requirements.txt


echo "Collecting Static Files..."
python manage.py collectstatic --no-input --clear

echo "Running Migrations..."
python manage.py migrate

echo "Build completed!"

