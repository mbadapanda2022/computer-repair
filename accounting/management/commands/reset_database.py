#!/usr/bin/env python
"""Reset database: clear all data, drop migrations, recreate fresh."""
import os

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Reset database: clear all data, drop migrations, recreate fresh"

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.WARNING(
                "WARNING: This will DELETE ALL DATA and reset migrations!"
            )
        )
        confirm = input("Type 'yes' to continue: ")
        if confirm.lower() != "yes":
            self.stdout.write(self.style.ERROR("Aborted."))
            return

        with connection.cursor() as cursor:
            self.stdout.write("→ Disabling foreign key checks...")
            cursor.execute("SET session_replication_role = 'replica';")

            self.stdout.write("→ Getting all tables...")
            cursor.execute("""
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
            """)
            tables = [row[0] for row in cursor.fetchall()]

            if tables:
                self.stdout.write(f"→ Dropping {len(tables)} tables...")
                for table in tables:
                    cursor.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE;')
                    self.stdout.write(f"  Dropped: {table}")
            else:
                self.stdout.write("  No tables found.")

            self.stdout.write("→ Getting all sequences...")
            cursor.execute("""
                SELECT sequence_name
                FROM information_schema.sequences
                WHERE sequence_schema = 'public'
            """)
            sequences = [row[0] for row in cursor.fetchall()]

            for seq in sequences:
                cursor.execute(f'DROP SEQUENCE IF EXISTS "{seq}" CASCADE;')
                self.stdout.write(f"  Dropped sequence: {seq}")

            self.stdout.write("→ Getting all enums...")
            cursor.execute("""
                SELECT t.typname
                FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname = 'public' AND t.typtype = 'e'
            """)
            enums = [row[0] for row in cursor.fetchall()]

            for enum in enums:
                cursor.execute(f'DROP TYPE IF EXISTS "{enum}" CASCADE;')
                self.stdout.write(f"  Dropped enum: {enum}")

            self.stdout.write("→ Re-enabling foreign key checks...")
            cursor.execute("SET session_replication_role = 'origin';")

        self.stdout.write(
            self.style.SUCCESS("Database reset complete!")
        )
        self.stdout.write("→ Now run: python manage.py migrate")
        self.stdout.write("→ Then run: python manage.py createsuperuser_prod")