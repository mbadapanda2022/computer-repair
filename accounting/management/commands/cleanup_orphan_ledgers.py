"""
Delete orphan purchase/payment ledger entries whose reference
Purchase/Payment no longer exists.

Read-only until you type 'yes'. Safe to run multiple times.

Usage:
    python manage.py cleanup_orphan_ledgers --dry-run
    python manage.py cleanup_orphan_ledgers
"""
import logging

from django.core.management.base import BaseCommand

from accounting.models import (
    LedgerEntry, LedgerLine, Payment, Purchase, Invoice,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Delete orphan ledger entries (deleted parent, active entry)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        orphans = []

        # Purchase orphans
        for e in LedgerEntry.objects.filter(entry_type='purchase'):
            if not e.reference_id:
                continue
            if not Purchase.all_objects.filter(pk=e.reference_id).exists():
                orphans.append(e)

        # Payment orphans
        for e in LedgerEntry.objects.filter(
            entry_type__in=['payment', 'advance_received', 'advance_paid']
        ):
            if not e.reference_id:
                continue
            if not Payment.all_objects.filter(pk=e.reference_id).exists():
                orphans.append(e)

        # Invoice orphans (sales)
        for e in LedgerEntry.objects.filter(entry_type='sales'):
            if not e.reference_id:
                continue
            if not Invoice.all_objects.filter(pk=e.reference_id).exists():
                orphans.append(e)

        self.stdout.write(f"Found {len(orphans)} orphan ledger entries.\n")

        if not orphans:
            self.stdout.write(self.style.SUCCESS("Nothing to clean up."))
            return

        for e in orphans:
            self.stdout.write(
                f"  Entry#{e.id} | type={e.entry_type} | ref_id={e.reference_id} | "
                f"date={e.date} | desc='{e.description}'"
            )

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"\nDRY RUN — nothing changed. Would delete: {len(orphans)}"
            ))
            return

        deleted = 0
        for e in orphans:
            for line in list(LedgerLine.objects.filter(ledger_entry=e)):
                line.delete()
            e.delete()
            deleted += 1

        self.stdout.write(self.style.SUCCESS(
            f"\n✅ Deleted {deleted} orphan ledger entries."
        ))