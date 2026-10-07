#!/usr/bin/env bash
set -o errexit

echo "=========================================="
echo "  Build Started: $(date)"
echo "=========================================="

echo "→ Upgrading pip..."
pip install --upgrade pip

echo "→ Installing dependencies..."
pip install -r requirements.txt

# Only reset database if FORCE_DB_RESET env var is set to true
if [ "$FORCE_DB_RESET" = "true" ]; then
    echo "→ Resetting database..."
    python manage.py reset_database --force
else
    echo "→ Skipping database reset (set FORCE_DB_RESET=true to enable)"
fi

echo "→ Running migrations..."
python manage.py migrate --no-input --verbosity 2

echo "→ Creating cache table..."
python manage.py createcachetable

# Only create superuser if it doesn't already exist
echo "→ Ensuring superuser exists..."
python manage.py createsuperuser_prod

echo "→ Collecting static files..."
python manage.py collectstatic --no-input --clear

echo "=========================================="
echo "  Build Completed: $(date)"
echo "=========================================="