# accounting/management/commands/fix_ledger_orphans.py
"""
Fix orphan ledger entries — live entries/lines whose parent document
is deleted. These orphans cause statement/contact-list mismatches.

Usage:
    python manage.py fix_ledger_orphans              # dry-run
    python manage.py fix_ledger_orphans --apply      # actually fix
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from accounting.models import (
    Contact, LedgerEntry, LedgerLine,
    Payment, Purchase, Invoice, CreditNote,
)


class Command(BaseCommand):
    help = "Fix orphan ledger entries so statements match contact balances"

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', default=False)

    def handle(self, *args, **options):
        apply_changes = options['apply']

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "DRY-RUN — no changes. Add --apply to fix."
            ))
        else:
            self.stdout.write(self.style.ERROR(
                "APPLY MODE — changes WILL be made."
            ))
        self.stdout.write("")

        # ── Live parent ID sets ──
        live_payment_ids = set(
            Payment.objects.filter(is_deleted=False).values_list('pk', flat=True)
        )
        live_purchase_ids = set(
            Purchase.objects.filter(is_deleted=False).values_list('pk', flat=True)
        )
        live_invoice_ids = set(
            Invoice.objects.filter(is_deleted=False).values_list('pk', flat=True)
        )
        live_cn_ids = set(
            CreditNote.objects.filter(is_deleted=False).values_list('pk', flat=True)
        )

        # ═══════════════════════════════════════════════════════
        # PART 1 — Live entries whose parent doc is deleted
        # ═══════════════════════════════════════════════════════
        self.stdout.write(self.style.MIGRATE_HEADING(
            "── PART 1: Live entries with deleted parent ──"
        ))

        def _orphan_reason(entry):
            if entry.entry_type in ('payment', 'advance_received', 'advance_paid'):
                if entry.reference_id not in live_payment_ids:
                    return "Payment missing/deleted"
            elif entry.entry_type == 'purchase':
                if entry.reference_id not in live_purchase_ids:
                    return "Purchase missing/deleted"
            elif entry.entry_type == 'sales':
                if entry.reference_id not in live_invoice_ids:
                    return "Invoice missing/deleted"
            elif entry.entry_type == 'credit_note':
                if entry.reference_id not in live_cn_ids:
                    return "CreditNote missing/deleted"
            return None

        fixed_entries = 0
        for entry in LedgerEntry.objects.filter(is_deleted=False):
            if not entry.reference_id:
                continue
            reason = _orphan_reason(entry)
            if not reason:
                continue

            self.stdout.write(
                f"  ORPHAN Entry #{entry.pk} | {entry.entry_type} | "
                f"ref={entry.reference_id} | date={entry.date} | "
                f"{reason}"
            )

            if apply_changes:
                with transaction.atomic():
                    for line in list(entry.lines.all()):
                        line.delete()
                    entry.delete()
                fixed_entries += 1
                self.stdout.write(self.style.SUCCESS("     ✓ Deleted"))

        if fixed_entries == 0:
            self.stdout.write("  (none)")
        self.stdout.write("")

        # ═══════════════════════════════════════════════════════
        # PART 2 — Live lines whose parent entry is deleted
        # ═══════════════════════════════════════════════════════
        self.stdout.write(self.style.MIGRATE_HEADING(
            "── PART 2: Live lines with deleted parent entry ──"
        ))

        fixed_lines = 0
        orphan_lines = LedgerLine.objects.filter(
            is_deleted=False,
            ledger_entry__is_deleted=True,
        ).select_related('ledger_entry')

        for ll in orphan_lines:
            self.stdout.write(
                f"  ORPHAN Line #{ll.pk} | entry#{ll.ledger_entry_id} | "
                f"dr={ll.debit} cr={ll.credit} | "
                f"desc={ll.ledger_entry.description}"
            )
            if apply_changes:
                ll.delete()
                fixed_lines += 1
                self.stdout.write(self.style.SUCCESS("     ✓ Deleted"))

        if fixed_lines == 0:
            self.stdout.write("  (none)")
        self.stdout.write("")

        # ═══════════════════════════════════════════════════════
        # PART 3 — Recalculate contact balances
        # ═══════════════════════════════════════════════════════
        if apply_changes:
            self.stdout.write(self.style.MIGRATE_HEADING(
                "── PART 3: Recalculating contacts ──"
            ))
            for c in Contact.objects.all():
                try:
                    c.recalc_balance()
                    c.recalc_advance_balance()
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(f"  Error on {c.name}: {e}")
                    )
            self.stdout.write(
                self.style.SUCCESS(
                    f"  ✓ Recalculated {Contact.objects.count()} contacts"
                )
            )

        # ═══════════════════════════════════════════════════════
        # SUMMARY
        # ═══════════════════════════════════════════════════════
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("── SUMMARY ──"))
        self.stdout.write(f"  Live entries with deleted parent : {fixed_entries}")
        self.stdout.write(f"  Live lines with deleted entry    : {fixed_lines}")

        if not apply_changes:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Run again with --apply to actually fix."
            ))