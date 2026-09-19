"""
Find purchases with missing ledger entries and re-sync them.

Safe to run multiple times (idempotent).
Uses the existing `sync_purchase_ledger()` function.

Usage:
    python manage.py resync_purchase_ledgers --dry-run
    python manage.py resync_purchase_ledgers
    python manage.py resync_purchase_ledgers --purchase-id=18
"""
import logging

from django.core.management.base import BaseCommand

from accounting.models import Purchase, LedgerEntry, sync_purchase_ledger

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Find purchases missing ledger entries and re-sync them."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report without making any changes.',
        )
        parser.add_argument(
            '--purchase-id', type=int, default=None,
            help='Only process this specific purchase ID.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        pid = options['purchase_id']

        qs = Purchase.objects.all().order_by('id')
        if pid:
            qs = qs.filter(pk=pid)

        missing = []
        for p in qs:
            has_entry = LedgerEntry.objects.filter(
                entry_type='purchase', reference_id=p.id
            ).exists()
            if not has_entry:
                missing.append(p)

        self.stdout.write(f"Total purchases scanned: {qs.count()}")
        self.stdout.write(f"Missing ledger entries: {len(missing)}")
        self.stdout.write("")

        if not missing:
            self.stdout.write(self.style.SUCCESS("Nothing to fix. All good!"))
            return

        fixed = 0
        failed = 0

        for p in missing:
            self.stdout.write(
                f"  → {p.purchase_number} (id={p.id}, ₹{p.grand_total}, "
                f"office_use={'yes' if p.items.filter(is_office_use=True).exists() else 'no'})"
            )

            if dry_run:
                continue

            try:
                sync_purchase_ledger(p)
                fixed += 1
                self.stdout.write(self.style.SUCCESS(f"     ✓ Fixed"))
            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f"     ✗ FAILED: {e}"))
                logger.exception(f"Re-sync failed for {p.purchase_number}")

        self.stdout.write("")
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"DRY RUN — nothing changed. Would fix: {len(missing)}"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Done. Fixed: {fixed} | Failed: {failed}"
            ))