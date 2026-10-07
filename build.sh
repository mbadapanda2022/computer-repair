#!/usr/bin/env bash
set -o errexit

echo "=========================================="
echo "  Build Started: $(date)"
echo "=========================================="

echo "→ Upgrading pip..."
pip install --upgrade pip

echo "→ Installing dependencies..."
pip install -r requirements.txt

echo "→ Creating cache table..."
python manage.py createcachetable

echo "→ Resetting database..."
python manage.py reset_database

echo "→ Running migrations..."
python manage.py migrate --no-input --verbosity 2

echo "→ Collecting static files..."
python manage.py collectstatic --no-input --clear

echo "=========================================="
echo "  Build Completed: $(date)"
echo "=========================================="