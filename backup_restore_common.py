"""
Shared backup/restore logic used by both CLI (backup_db.py) and
the web admin view (accounting/views/settings.py).

Uses a custom JSON format that is compatible with BOTH SQLite and
PostgreSQL. Works regardless of which database engine the backup
was created from.
"""
import json
import logging
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
from django.db import DatabaseError, connection, transaction  # noqa: E402

logger = logging.getLogger(__name__)


def _serialize_value(value):
    """Convert a field value to a JSON-serialisable form.

    dict/list values must stay structured — JSONField columns (e.g. the
    IC ``categories`` field) would otherwise be restored as a Python-repr
    string and permanently corrupt the stored document.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, (dict, list)):
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


def _sqlite_set_foreign_keys(enabled):
    """Toggle SQLite FK enforcement. Must be called outside a transaction."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                'PRAGMA foreign_keys = %s;' % ('ON' if enabled else 'OFF')
            )
            cursor.execute('PRAGMA foreign_keys;')
            row = cursor.fetchone()
        return not row or bool(row[0]) == enabled
    except DatabaseError as exc:
        logger.warning("Could not toggle SQLite foreign_keys: %s", exc)
        return False


def _pg_disable_triggers():
    """
    Best-effort disable of FK triggers inside the current transaction.

    ``session_replication_role`` requires elevated privileges; on managed
    Postgres (Supabase) it may be rejected. Returning False simply means
    the restore falls back to dependency-ordered inserts with checks on.
    """
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL session_replication_role = 'replica';")
        return True
    except DatabaseError as exc:
        logger.warning("Could not disable FK triggers for restore: %s", exc)
        return False


def _payload_records(payload):
    """Accept both supported backup payload shapes, else return None."""
    if isinstance(payload, dict) and 'records' in payload:
        return payload['records']
    if isinstance(payload, list):
        return payload
    return None


def _reset_postgres_sequences(models):
    """Advance serial sequences past the explicitly restored PK values."""
    from django.db import connection as _connection

    for model in models:
        table = _connection.ops.quote_name(model._meta.db_table)
        column = _connection.ops.quote_name(model._meta.pk.column)
        try:
            with _connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT setval(
                        pg_get_serial_sequence(%s, %s),
                        COALESCE((SELECT MAX({column}) FROM {table}), 1),
                        (SELECT MAX({column}) FROM {table}) IS NOT NULL
                    )
                    """,
                    [model._meta.db_table, model._meta.pk.column],
                )
        except DatabaseError as exc:
            logger.warning(
                "Could not reset sequence for %s: %s", model._meta.db_table, exc
            )


def restore_from_json(json_data):
    """
    Restore the database from a JSON string or dict.

    Handles BOTH backup formats:
      Format A (backup_db.py):      {"data": {"model": [record, ...]}}
      Format B (manage.py command): {"data": {"model": {"table": "...", "records": [...]}}}

    The wipe and the insert run in a single transaction: any failure
    rolls the database back to its pre-restore state instead of leaving
    it half-empty.
    """
    if isinstance(json_data, (bytes, str)):
        backup_data = json.loads(json_data)
    else:
        backup_data = json_data

    if not isinstance(backup_data, dict) or not isinstance(
        backup_data.get('data'), dict
    ):
        raise ValueError("Invalid backup: missing 'data' section.")

    metadata = backup_data.get('metadata', {})
    print(
        f"  Source: {metadata.get('database_engine', 'unknown')} | "
        f"Records: {metadata.get('total_records', 0)}"
    )

    models = _sort_models_by_dependency()
    data = backup_data['data']

    # Resolve every payload BEFORE touching the database so a malformed
    # file cannot wipe live data.
    resolved = []
    total_records = 0
    for model in models:
        model_key = f"{model._meta.app_label}.{model._meta.object_name}"
        if model_key not in data:
            continue
        records = _payload_records(data[model_key])
        if records is None:
            print(
                f"  [SKIP] {model_key}: unrecognized payload type "
                f"{type(data[model_key]).__name__}"
            )
            continue
        resolved.append((model, records))
        total_records += len(records)

    known_keys = {
        f"{m._meta.app_label}.{m._meta.object_name}" for m in models
    }
    for model_key in data:
        if model_key not in known_keys:
            print(f"  [SKIP] {model_key}: model not found")

    if not resolved:
        raise ValueError(
            "Backup contains no recognizable model data — existing data "
            "was left untouched."
        )

    print(f"  Restoring {total_records} records across {len(resolved)} models")

    is_sqlite = connection.vendor == 'sqlite'
    fk_disabled = False
    if is_sqlite:
        # PRAGMA foreign_keys is a no-op inside a transaction, so it must
        # be toggled before starting one.
        fk_disabled = _sqlite_set_foreign_keys(False)
        if not fk_disabled:
            logger.warning(
                "SQLite FK enforcement is still on; relying on "
                "dependency ordering for this restore."
            )

    m2m_queue = []  # (model, obj, field_name, ids)
    total_restored = 0

    try:
        with transaction.atomic():
            if connection.vendor == 'postgresql':
                _pg_disable_triggers()

            # ── Clear existing data, children first ──────────
            print("Clearing existing data...")
            with connection.cursor() as cursor:
                # Children first: safe even when FK triggers are still on.
                for model in reversed(models):
                    cursor.execute(
                        f'DELETE FROM "{model._meta.db_table}";'
                    )

            # ── Pass 1: insert parents first ────────────────
            with _preserve_auto_fields():
                for model, records in resolved:
                    model_key = (
                        f"{model._meta.app_label}.{model._meta.object_name}"
                    )
                    print(
                        f"  Restoring {model_key}... ({len(records)} records)"
                    )

                    manager = (
                        getattr(model, 'all_objects', None) or model.objects
                    )
                    valid_names = set()
                    for field in model._meta.fields:
                        valid_names.add(field.attname)
                        valid_names.add(field.name)

                    objs = []
                    for record in records:
                        if not isinstance(record, dict):
                            print(
                                "    [WARN] Skipping non-dict record: "
                                f"{type(record).__name__}"
                            )
                            continue

                        m2m_fields = {}
                        record_copy = {}
                        for key, val in record.items():
                            if key not in valid_names:
                                continue
                            try:
                                field = model._meta.get_field(key)
                            except Exception:
                                field = None
                            if field is not None and field.many_to_many:
                                m2m_fields[key] = val
                            else:
                                record_copy[key] = val

                        try:
                            obj = model(**record_copy)
                        except (TypeError, ValueError) as exc:
                            print(
                                f"    [WARN] Skipping invalid record: {exc}"
                            )
                            continue
                        objs.append(obj)
                        for m2m_name, ids in m2m_fields.items():
                            m2m_queue.append((model, obj, m2m_name, ids))

                    # Chunked insert keeps memory flat on large ledgers.
                    for start in range(0, len(objs), 1000):
                        manager.bulk_create(objs[start:start + 1000])
                    total_restored += len(objs)
                    print(f"    [OK] Restored {len(objs)} records")

            # ── Pass 2: M2M relations ───────────────────────
            print("Restoring M2M relationships...")
            if m2m_queue:
                m2m_count = 0
                for model, obj, field_name, ids in m2m_queue:
                    if not ids:
                        continue
                    try:
                        field = model._meta.get_field(field_name)
                        related_model = field.related_model
                        related_manager = (
                            getattr(related_model, 'all_objects', None)
                            or related_model.objects
                        )
                        related_objs = related_manager.filter(pk__in=ids)
                        getattr(obj, field.name).set(related_objs)
                        m2m_count += related_objs.count()
                    except Exception as exc:
                        print(f"    Warning M2M {field_name}: {exc}")
                print(f"  Restored {m2m_count} M2M relations")

            if connection.vendor == 'postgresql':
                _reset_postgres_sequences(models)
    finally:
        if is_sqlite and fk_disabled:
            _sqlite_set_foreign_keys(True)

    print(f"Restore complete: {total_restored} records restored")
    return total_restored