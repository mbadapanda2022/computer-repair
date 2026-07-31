# accounting/management/commands/clean_zero_ledger_lines.py

from django.core.management.base import BaseCommand
from accounting.models import LedgerLine

class Command(BaseCommand):
    help = 'Delete all ledger lines with zero debit and zero credit (ghost entries)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show count without actually deleting',
        )

    def handle(self, *args, **options):
        dry_run = options.get('dry_run', False)
        qs = LedgerLine.objects.filter(debit=0, credit=0)
        count = qs.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS('✅ No zero-amount ledger lines found. Clean!'))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(f'🔍 Dry run: {count} zero-amount ledger lines would be deleted.'))
            return

        # Actually delete
        qs.delete()
        self.stdout.write(self.style.SUCCESS(f'✅ Deleted {count} zero-amount ledger lines permanently.'))