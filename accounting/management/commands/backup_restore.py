#!/usr/bin/env python
"""Professional database backup and restore system.
Works with both SQLite and PostgreSQL.
Uses JSON format for maximum portability.
"""
import json
import os
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError
from django.apps import apps
from django.db import connection


class Command(BaseCommand):
    help = "Backup or restore database (SQLite + PostgreSQL compatible, JSON format)"

    def add_arguments(self, parser):
        parser.add_argument(
            'action',
            choices=['backup', 'restore'],
            help='backup or restore',
        )
        parser.add_argument(
            '--file',
            default=None,
            help='Backup file path (default: auto-generated)',
        )

    def handle(self, *args, **options):
        action = options['action']
        file_path = options.get('file')

        if action == 'backup':
            self._backup(file_path)
        elif action == 'restore':
            self._restore(file_path)

    def _get_backup_filename(self):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        return f'backup_{timestamp}.json'

    def _backup(self, file_path):
        if not file_path:
            file_path = self._get_backup_filename()

        self.stdout.write(f"→ Starting database backup to: {file_path}")

        all_models = apps.get_models()
        backup_data = {
            'metadata': {
                'timestamp': datetime.now().isoformat(),
                'database_engine': connection.vendor,
                'total_models': len(all_models),
            },
            'data': {}
        }

        total_records = 0
        for model in all_models:
            model_name = model._meta.object_name
            app_label = model._meta.app_label
            table_name = model._meta.db_table

            self.stdout.write(f"  Backing up {app_label}.{model_name}...")

            records = []
            for obj in model.objects.all().iterator():
                record = {}
                for field in model._meta.get_fields():
                    if hasattr(field, 'attname') and not field.is_relation:
                        value = getattr(obj, field.attname, None)
                        record[field.attname] = self._serialize_value(value)
                    elif field.is_relation and field.many_to_many:
                        m2m_values = list(getattr(obj, field.name).values_list('id', flat=True))
                        record[field.name] = m2m_values
                records.append(record)

            backup_data['data'][f"{app_label}.{model_name}"] = {
                'table': table_name,
                'records': records,
            }
            total_records += len(records)
            self.stdout.write(f"    {len(records)} records")

        backup_data['metadata']['total_records'] = total_records

        json_data = json.dumps(backup_data, indent=2, default=str)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(json_data)

        file_size = os.path.getsize(file_path)
        self.stdout.write(
            self.style.SUCCESS(
                f"Backup complete: {file_path} ({file_size:,} bytes, {total_records} records)"
            )
        )

    def _restore(self, file_path):
        if not file_path:
            raise CommandError("--file is required for restore")

        if not os.path.exists(file_path):
            raise CommandError(f"Backup file not found: {file_path}")

        self.stdout.write(f"→ Starting restore from: {file_path}")

        with open(file_path, 'r', encoding='utf-8') as f:
            backup_data = json.load(f)

        metadata = backup_data['metadata']
        self.stdout.write(
            f"  Source: {metadata.get('database_engine', 'unknown')} | "
            f"Records: {metadata.get('total_records', 0)}"
        )

        # Clear existing data
        self.stdout.write("→ Clearing existing data...")
        with connection.cursor() as cursor:
            if connection.vendor == 'postgresql':
                cursor.execute("SET session_replication_role = 'replica';")
                for model in apps.get_models():
                    cursor.execute(
                        f'DELETE FROM "{model._meta.db_table}";'
                    )
                cursor.execute("SET session_replication_role = 'origin';")
            else:
                for model in apps.get_models():
                    model.objects.all().delete()

        # Restore data
        total_restored = 0
        for model_key, model_data in backup_data['data'].items():
            app_label, model_name = model_key.split('.')
            model = apps.get_model(app_label, model_name)
            records = model_data['records']

            self.stdout.write(f"  Restoring {model_key}... ({len(records)} records)")

            objs = []
            m2m_records = []
            for record in records:
                m2m_data = {}
                for key in list(record.keys()):
                    field = model._meta.get_field(key) if key in [f.name for f in model._meta.get_fields()] else None
                    if field and field.many_to_many:
                        m2m_data[key] = record.pop(key)

                obj = model(**record)
                objs.append(obj)
                if m2m_data:
                    m2m_records.append((obj, m2m_data))

            model.objects.bulk_create(objs, ignore_conflicts=True)

            # Restore M2M
            for obj, m2m_data in m2m_records:
                for m2m_field, ids in m2m_data.items():
                    if ids:
                        m2m_field_obj = model._meta.get_field(m2m_field)
                        related_model = m2m_field_obj.related_model
                        related_objs = related_model.objects.filter(id__in=ids)
                        getattr(obj, m2m_field).set(related_objs)

            total_restored += len(records)
            self.stdout.write(f"    Restored {len(records)} records")

        self.stdout.write(
            self.style.SUCCESS(
                f"Restore complete: {total_restored} records restored"
            )
        )

    def _serialize_value(self, value):
        if value is None:
            return None
        if isinstance(value, (int, float, str, bool)):
            return value
        return str(value)