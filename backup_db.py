#!/usr/bin/env python
"""
Database Backup & Restore Utility
=================================
Works with both SQLite and PostgreSQL.
Usage:
    python backup_db.py backup [output_file]
    python backup_db.py restore [backup_file]
"""
import json
import os
import sys
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'computer_repair.settings')

import django
django.setup()

from django.apps import apps
from django.db import connection


def get_all_data():
    """Extract all data from all models."""
    backup_data = {
        'metadata': {
            'timestamp': datetime.now().isoformat(),
            'database_engine': connection.vendor,
        },
        'data': {}
    }

    for model in apps.get_models():
        model_key = f"{model._meta.app_label}.{model._meta.object_name}"
        records = []

        for obj in model.objects.all().iterator():
            record = {}
            for field in model._meta.get_fields():
                if hasattr(field, 'attname') and not field.is_relation:
                    value = getattr(obj, field.attname, None)
                    if value is None:
                        record[field.attname] = None
                    elif isinstance(value, (int, float, str, bool)):
                        record[field.attname] = value
                    else:
                        record[field.attname] = str(value)
                elif field.is_relation and field.many_to_many:
                    try:
                        ids = list(getattr(obj, field.name).values_list('id', flat=True))
                        record[field.name] = ids
                    except Exception:
                        record[field.name] = []
            records.append(record)

        backup_data['data'][model_key] = records
        print(f"  {model_key}: {len(records)} records")

    return backup_data


def backup_database(output_file=None):
    """Create a full database backup."""
    if not output_file:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = f'backup_{timestamp}.json'

    print(f"-> Creating backup: {output_file}")
    backup_data = get_all_data()

    total = sum(len(v) for v in backup_data['data'].values())
    backup_data['metadata']['total_records'] = total

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(backup_data, f, indent=2, default=str)

    size = os.path.getsize(output_file)
    print(f"[OK] Backup saved: {output_file} ({size:,} bytes, {total} records)")
    return output_file


def restore_database(backup_file):
    """Restore database from backup file."""
    if not os.path.exists(backup_file):
        print(f"[FAIL] Backup file not found: {backup_file}")
        return False

    print(f"-> Restoring from: {backup_file}")

    with open(backup_file, 'r', encoding='utf-8') as f:
        backup_data = json.load(f)

    metadata = backup_data['metadata']
    print(f"  Source: {metadata.get('database_engine', 'unknown')} | "
          f"Records: {metadata.get('total_records', 0)}")

    # Clear existing data
    print("-> Clearing existing data...")
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

    # Restore data
    total_restored = 0
    for model_key, payload in backup_data['data'].items():
        app_label, model_name = model_key.split('.')
        model = apps.get_model(app_label, model_name)

        # Support both backup formats:
        #   Format A (backup_db.py):     {"data": {"model": [record, ...]}}
        #   Format B (manage.py command): {"data": {"model": {"table": "...", "records": [...]}}}
        if isinstance(payload, dict) and 'records' in payload:
            records = payload['records']
        elif isinstance(payload, list):
            records = payload
        else:
            print(f"  [SKIP] {model_key}: unrecognized payload type {type(payload).__name__}")
            continue

        print(f"  Restoring {model_key}... ({len(records)} records)")

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

        model.objects.bulk_create(objs, ignore_conflicts=True)

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

    # Re-enable FK checks on SQLite (disabled above for the whole restore)
    if fk_off:
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys = ON;")

    print(f"[OK] Restore complete: {total_restored} records restored")
    return True


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python backup_db.py backup [output_file]")
        print("  python backup_db.py restore [backup_file]")
        sys.exit(1)

    action = sys.argv[1]
    file_arg = sys.argv[2] if len(sys.argv) > 2 else None

    if action == 'backup':
        backup_database(file_arg)
    elif action == 'restore':
        restore_database(file_arg)
    else:
        print(f"Unknown action: {action}")
        print("Usage:")
        print("  python backup_db.py backup [output_file]")
        print("  python backup_db.py restore [backup_file]")
        sys.exit(1)