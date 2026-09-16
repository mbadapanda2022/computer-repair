#!/usr/bin/env bash

set -o errexit

echo "=========================================="
echo "  Build Started: $(date)"
echo "=========================================="

echo "→ Upgrading pip..."
pip install --upgrade pip

echo "→ Installing dependencies..."
pip install -r requirements.txt

echo "→ Creating cache table (for rate limiting)..."
python manage.py createcachetable

echo "→ Making migrations (if any pending)..."
python manage.py makemigrations --no-input

echo "→ Running migrations on production DB..."
python manage.py migrate --no-input --verbosity 2

echo "→ Collecting static files..."
python manage.py collectstatic --no-input --clear

echo "=========================================="
echo "  Build Completed: $(date)"
echo "=========================================="