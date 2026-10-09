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
import contextlib
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


def _get_backup_fields(model):
    """
    Return (concrete_fields, m2m_fields) for a model.

    Uses _meta.fields (concrete fields — includes FK, O2O, AutoField)
    and _meta.many_to_many.  This is the canonical Django pattern
    (same one the built-in serializer uses) and correctly captures
    the ``_id`` columns for ForeignKey / OneToOne relations.
    """
    concrete_fields = model._meta.fields
    m2m_fields = model._meta.many_to_many
    return concrete_fields, m2m_fields


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

        concrete_fields, m2m_fields = _get_backup_fields(model)

        records = []
        for obj in manager.all().iterator():
            record = {}
            # Concrete fields — includes FK/O2O whose attname is ``<name>_id``
            for field in concrete_fields:
                value = getattr(obj, field.attname, None)
                record[field.attname] = _serialize_value(value)
            # Many-to-many — store related PKs by field name
            for field in m2m_fields:
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


@contextlib.contextmanager
def _preserve_auto_fields():
    """
    Temporarily disable ``auto_now`` / ``auto_now_add`` on every concrete
    field so that ``bulk_create`` writes the *backed-up* timestamp values
    instead of overwriting them with the current time.
    """
    saved = []
    for model in apps.get_models():
        for field in model._meta.fields:
            if getattr(field, 'auto_now', False) or getattr(field, 'auto_now_add', False):
                saved.append((field, field.auto_now, field.auto_now_add))
                field.auto_now = False
                field.auto_now_add = False
    try:
        yield
    finally:
        for field, auto_now, auto_now_add in saved:
            field.auto_now = auto_now
            field.auto_now_add = auto_now_add


def _sort_models_by_dependency():
    """
    Return a list of models roughly ordered so that referenced (parent)
    models come before models that reference them (children).

    With FK checks disabled the order is not strictly required, but
    sorting by dependency depth is cheap and makes the restore more
    robust — especially for M2M two-pass restoration.
    """
    model_deps = {}  # model -> set of models it depends on (via FK)

    def _resolve(model):
        """Recursively resolve a model and its FK dependencies."""
        if model in model_deps:
            return model_deps[model]
        deps = set()
        seen = {model}
        stack = [model]
        while stack:
            current = stack.pop()
            for field in current._meta.fields:
                if field.is_relation and hasattr(field, 'related_model'):
                    rel = field.related_model
                    if rel and rel not in seen:
                        seen.add(rel)
                        stack.append(rel)
                        deps.add(rel)
        model_deps[model] = deps
        return deps

    all_models = apps.get_models()
    # Resolve deps for every model (caches results)
    for m in all_models:
        _resolve(m)

    # Topological-ish sort: model with fewer unresolved deps first
    ordered = []
    remaining = set(all_models)
    while remaining:
        ready = [
            m for m in remaining
            if model_deps[m] <= set(ordered)
        ]
        if not ready:
            # Circular or self-referential remaining — just append them
            ready = list(remaining)
        ready.sort(key=lambda m: (len(model_deps[m]), m._meta.label))
        ordered.append(ready[0])
        remaining.discard(ready[0])

    return ordered


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

    models = _sort_models_by_dependency()

    # ── Clear existing data ──────────────────────────────────
    print("Clearing existing data...")
    with connection.cursor() as cursor:
        if connection.vendor == 'postgresql':
            cursor.execute("SET session_replication_role = 'replica.';")
        else:
            cursor.execute("PRAGMA foreign_keys = OFF;")

        for model in models:
            cursor.execute(f'DELETE FROM "{model._meta.db_table}";')

    # ── Restore data (two-pass: concrete fields, then M2M) ──
    # Pass 1 — insert every record with FK values set directly
    # Pass 2 — wire up M2M relationships after ALL models exist

    # Collect M2M data per model for the second pass
    m2m_queue = []  # list of (model, obj, field_name, ids)

    total_restored = 0

    with _preserve_auto_fields():
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
            for record in records:
                if not isinstance(record, dict):
                    print(f"    [WARN] Skipping non-dict record: {type(record).__name__}")
                    continue
                # Separate M2M data — will be restored in pass 2
                m2m_fields = {}
                record_copy = {}
                for key, val in record.items():
                    try:
                        field = model._meta.get_field(key)
                        if field.many_to_many:
                            m2m_fields[key] = val
                        else:
                            record_copy[key] = val
                    except Exception:
                        # key is not a real field — store it anyway
                        # (harmless for bulk_create which ignores unknown attrs)
                        record_copy[key] = val

                obj = model(**record_copy)
                objs.append(obj)
                for m2m_name, ids in m2m_fields.items():
                    m2m_queue.append((model, obj, m2m_name, ids))

            if objs:
                manager.bulk_create(objs, ignore_conflicts=True)
                total_restored += len(objs)

            print(f"    [OK] Restored {len(records)} records")

    # ── Pass 2: restore M2M relationships ─────────────────────
    print("Restoring M2M relationships...")
    if m2m_queue:
        m2m_count = 0
        for model, obj, field_name, ids in m2m_queue:
            if not ids:
                continue
            try:
                field = model._meta.get_field(field_name)
                related_model = field.related_model
                related_objs = related_model.objects.filter(id__in=ids)
                getattr(obj, field.name).set(related_objs)
                m2m_count += len(related_objs)
            except Exception as e:
                print(f"    Warning M2M {field_name}: {e}")
        print(f"  Restored {m2m_count} M2M relations")

    # ── Reset auto-increment sequences (PostgreSQL) ─────────
    # After bulk-inserting explicit PKs, the serial sequence may lag
    # behind.  Reset it so future INSERTs don't collide.
    if connection.vendor == 'postgresql':
        from django.db import transaction as _tx
        with _tx.atomic():
            with connection.cursor() as cursor:
                for model in models:
                    pk_col = model._meta.pk.attname
                    cursor.execute(
                        """
                        SELECT setval(
                            pg_get_serial_sequence(%s, %s),
                            COALESCE((SELECT MAX(%s) FROM %s), 1),
                            (SELECT MAX(%s) FROM %s) IS NOT NULL
                        )
                        """,
                        [
                            model._meta.db_table,
                            pk_col,
                            pk_col,
                            model._meta.db_table,
                            pk_col,
                            model._meta.db_table,
                        ],
                    )
        # Re-enable FK checks on PostgreSQL (session-level, not transactional)
        with connection.cursor() as cursor:
            cursor.execute("SET session_replication_role = 'origin.';")
    else:
        # Re-enable FK checks on SQLite
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA foreign_keys = ON;")

    print(f"Restore complete: {total_restored} records restored")
    return total_restored