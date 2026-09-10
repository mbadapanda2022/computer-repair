#!/usr/bin/env bash
set -o errexit

echo "Upgrading pip..."
pip install --upgrade pip

echo "Installing dependencies..."
pip install -r requirements.txt

echo "Collecting Static Files..."
python manage.py collectstatic --no-input --clear

echo "Running Migrations..."
python manage.py migrate --verbosity 2

echo "Build completed!"