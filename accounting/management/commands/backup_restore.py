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

        self.stdout.write(f"Starting database backup to: {file_path}")

        # Use the shared common module (same logic as backup_db.py and web view)
        from backup_restore_common import backup_all_data
        backup_data = backup_all_data()

        total_records = backup_data['metadata']['total_records']
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

        self.stdout.write(f"Starting restore from: {file_path}")

        with open(file_path, 'r', encoding='utf-8') as f:
            backup_data = json.load(f)

        # Use the shared common module (same logic as backup_db.py and web view)
        from backup_restore_common import restore_from_json
        restore_from_json(backup_data)

        self.stdout.write(
            self.style.SUCCESS(
                f"Restore complete: {backup_data['metadata'].get('total_records', 0)} records restored"
            )
        )