"""
Shared backup/restore logic used by both CLI (backup_db.py) and
the web admin view (accounting/views/settings.py).

Uses a custom JSON format that is compatible with BOTH SQLite and
PostgreSQL. Works regardless of which database engine the backup
was created from.
"""
import json
import os
import sys
from datetime import datetime

# Ensure project root is on sys.path when imported from a management
# command or view (both live outside the project root).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'computer_repair.settings')

import django  # noqa: E402
django.setup()

from django.apps import apps  # noqa: E402
from django.db import connection  # noqa: E402


def _serialize_value(value):
    """Convert a field value to a JSON-serialisable form."""
    if value is None:
        return None
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def backup_all_data():
    """Return a dict with 'metadata' and 'data' keys covering every model."""
    backup_data = {
        'metadata': {
            'timestamp': datetime.now().isoformat(),
            'database_engine': connection.vendor,
        },
        'data': {},
    }

    total = 0
    for model in apps.get_models():
        model_key = f"{model._meta.app_label}.{model._meta.object_name}"

        # Prefer all_objects (includes soft-deleted records) when available,
        # falling back to the default manager for models without soft-delete.
        manager = getattr(model, 'all_objects', None) or model.objects

        records = []
        for obj in manager.all().iterator():
            record = {}
            for field in model._meta.get_fields():
                if hasattr(field, 'attname') and not field.is_relation:
                    value = getattr(obj, field.attname, None)
                    record[field.attname] = _serialize_value(value)
                elif field.is_relation and field.many_to_many:
                    try:
                        ids = list(
                            getattr(obj, field.name).values_list('id', flat=True)
                        )
                        record[field.name] = ids
                    except Exception:
                        record[field.name] = []
            records.append(record)
        backup_data['data'][model_key] = records
        total += len(records)

    backup_data['metadata']['total_records'] = total
    return backup_data


def backup_to_json():
    """Return the backup as a JSON string."""
    return json.dumps(backup_all_data(), indent=2, default=str)


def restore_from_json(json_data):
    """
    Restore the database from a JSON string or dict.

    Handles BOTH backup formats:
      Format A (backup_db.py):      {"data": {"model": [record, ...]}}
      Format B (manage.py command): {"data": {"model": {"table": "...", "records": [...]}}}
    """
    if isinstance(json_data, (bytes, str)):
        backup_data = json.loads(json_data)
    else:
        backup_data = json_data

    metadata = backup_data.get('metadata', {})
    print(
        f"  Source: {metadata.get('database_engine', 'unknown')} | "
        f"Records: {metadata.get('total_records', 0)}"
    )

    # ── Clear existing data ──────────────────────────────────
    print("Clearing existing data...")
    fk_off = False
    with connection.cursor() as cursor:
        if connection.vendor == 'postgresql':
            cursor.execute("SET session_replication_role = 'replica';")
            for model in apps.get_models():
                cursor.execute(f'DELETE FROM "{model._meta.db_table}";')
            cursor.execute("SET session_replication_role = 'origin';")
        else:
            # SQLite: disable FK checks for the ENTIRE restore session
            # (bulk_create runs after this block, so we must keep PRAGMA OFF)
            cursor.execute("PRAGMA foreign_keys = OFF;")
            fk_off = True
            for model in apps.get_models():
                cursor.execute(f'DELETE FROM "{model._meta.db_table}";')

    # ── Restore data ─────────────────────────────────────────
    total_restored = 0
    for model_key, payload in backup_data['data'].items():
        try:
            app_label, model_name = model_key.split('.')
            model = apps.get_model(app_label, model_name)
        except LookupError:
            print(f"  [SKIP] {model_key}: model not found")
            continue

        # Support both backup formats
        if isinstance(payload, dict) and 'records' in payload:
            records = payload['records']
        elif isinstance(payload, list):
            records = payload
        else:
            print(
                f"  [SKIP] {model_key}: unrecognized payload type "
                f"{type(payload).__name__}"
            )
            continue

        print(f"  Restoring {model_key}... ({len(records)} records)")

        # Use all_objects (includes soft-deleted records) when available,
        # falling back to default manager.
        manager = getattr(model, 'all_objects', None) or model.objects

        objs = []
        m2m_data = []
        for record in records:
            if not isinstance(record, dict):
                print(f"    [WARN] Skipping non-dict record: {type(record).__name__}")
                continue
            m2m_fields = {}
            for key in list(record.keys()):
                try:
                    field = model._meta.get_field(key)
                    if field.many_to_many:
                        m2m_fields[key] = record.pop(key)
                except Exception:
                    pass

            obj = model(**record)
            objs.append(obj)
            if m2m_fields:
                m2m_data.append((obj, m2m_fields))

        manager.bulk_create(objs, ignore_conflicts=True)

        # Restore M2M relationships
        for obj, m2m_fields in m2m_data:
            for field_name, ids in m2m_fields.items():
                if ids:
                    try:
                        field = model._meta.get_field(field_name)
                        related_model = field.related_model
                        related_objs = related_model.objects.filter(id__in=ids)
                        getattr(obj, field.name).set(related_objs)
                    except Exception as e:
                        print(f"    Warning M2M {field_name}: {e}")

        total_restored += len(records)
        print(f"    [OK] Restored {len(records)} records")

    # Re-enable FK checks on SQLite
    if fk_off:
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys = ON;")

    print(f"Restore complete: {total_restored} records restored")
    return total_restored