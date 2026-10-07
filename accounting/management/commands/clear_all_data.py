#!/usr/bin/env python
"""Clear all data from all tables (truncate)."""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Clear all data from all tables (truncate)"

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.WARNING(
                "WARNING: This will DELETE ALL DATA from all tables!"
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
                self.stdout.write(f"→ Truncating {len(tables)} tables...")
                for table in tables:
                    cursor.execute(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE;')
                    self.stdout.write(f"  Truncated: {table}")
            else:
                self.stdout.write("  No tables found.")

            self.stdout.write("→ Re-enabling foreign key checks...")
            cursor.execute("SET session_replication_role = 'origin';")

        self.stdout.write(self.style.SUCCESS("All data cleared!"))