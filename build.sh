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


echo "→ Running migrations..."
python manage.py migrate --no-input --verbosity 2

# ⬇⬇⬇ YE NAYA SECTION ADD KARO ⬇⬇⬇
echo "→ Verifying blog tables..."
python manage.py shell -c "
from accounting.models import BlogCategory, BlogPost, BlogTag, BlogComment
print('BlogCategory:', BlogCategory.objects.count())
print('BlogPost:', BlogPost.objects.count())
print('BlogTag:', BlogTag.objects.count())
print('BlogComment:', BlogComment.objects.count())
"

echo "→ Verifying existing data integrity..."
python manage.py shell -c "
from accounting.models import Contact, Invoice, Purchase, RepairJob, Payment
print('Contacts:', Contact.objects.count())
print('Invoices:', Invoice.objects.count())
print('Purchases:', Purchase.objects.count())
print('Repairs:', RepairJob.objects.count())
print('Payments:', Payment.objects.count())
"
# ⬆⬆⬆ NAYA SECTION KHATAM ⬆⬆⬆

echo "→ Creating cache table..."
python manage.py createcachetable
# ... baaki build.sh waise hi rahega ...

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