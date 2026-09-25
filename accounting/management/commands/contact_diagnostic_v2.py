# accounting/management/commands/contact_diagnostic_v2.py
"""
Deep Ledger Diagnostic — shows EVERY entry & line, including soft-deleted.
Reveals duplicates hiding behind soft-deleted entries.

Usage:
    python manage.py contact_diagnostic_v2 "Mukta"
"""

from decimal import Decimal
from collections import defaultdict

from django.core.management.base import BaseCommand

from accounting.models import (
    Contact, LedgerEntry, LedgerLine,
    Payment, Purchase, Invoice,
)


def money(x):
    if x is None:
        return "0.00"
    return f"{Decimal(x).quantize(Decimal('0.01')):>12,.2f}"


class Command(BaseCommand):
    help = "Show every ledger entry (live + deleted) for a contact"

    def add_arguments(self, parser):
        parser.add_argument('name', type=str)

    def handle(self, *args, **options):
        name = options['name']
        contacts = Contact.objects.filter(name__icontains=name)
        if not contacts.exists():
            self.stdout.write(self.style.ERROR(f"No contact: '{name}'"))
            return

        for contact in contacts:
            self._dump(contact)

    def _dump(self, contact):
        line = "=" * 100
        self.stdout.write(self.style.MIGRATE_HEADING(line))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"CONTACT: {contact.name} (ID={contact.pk})  "
            f"type={contact.contact_type}"
        ))
        self.stdout.write(self.style.MIGRATE_HEADING(line))
        self.stdout.write("")

        # ── Show every line for this contact, WITH entry.is_deleted flag ──
        self.stdout.write(self.style.MIGRATE_HEADING(
            "── LINES (all entries, live + deleted) ──"
        ))
        self.stdout.write(
            f"{'Entry#':>6} {'E.del':>5} {'Line#':>6} {'L.del':>5} "
            f"{'Date':<12} {'Type':<16} {'Acct':<6} {'Sub':<10} "
            f"{'Debit':>12} {'Credit':>12}"
        )
        self.stdout.write("-" * 100)

        # Use all_objects to include soft-deleted entries
        all_lines = (
            LedgerLine.all_objects
            .filter(contact=contact)
            .select_related('ledger_entry', 'account')
            .order_by('ledger_entry__date', 'ledger_entry__id', 'pk')
        )

        # Group by entry
        by_entry = defaultdict(list)
        for ll in all_lines:
            by_entry[ll.ledger_entry_id].append(ll)

        for entry_id, lines in by_entry.items():
            entry = lines[0].ledger_entry
            for ll in lines:
                self.stdout.write(
                    f"{entry_id:>6} "
                    f"{'Y' if entry.is_deleted else 'N':>5} "
                    f"{ll.pk:>6} "
                    f"{'Y' if ll.is_deleted else 'N':>5} "
                    f"{str(entry.date):<12} "
                    f"{entry.entry_type:<16} "
                    f"{(ll.account.code if ll.account else '?'):<6} "
                    f"{(ll.subledger_type or '—'):<10} "
                    f"{money(ll.debit)} "
                    f"{money(ll.credit)}"
                )
        self.stdout.write("")

        # ── Show duplicate payment entries (including deleted) ──
        self.stdout.write(self.style.MIGRATE_HEADING(
            "── PAYMENTS & THEIR LEDGER ENTRIES ──"
        ))
        for pay in Payment.all_objects.filter(contact=contact).order_by('date'):
            self.stdout.write(
                f"  Payment PMT-{pay.pk:04d}  date={pay.date}  "
                f"amt={money(pay.amount)}  dir={pay.direction}  "
                f"is_deleted={'Y' if pay.is_deleted else 'N'}"
            )
            entries = LedgerEntry.all_objects.filter(
                reference_id=pay.pk,
                entry_type__in=[
                    'payment', 'advance_received', 'advance_paid',
                ],
            )
            for e in entries:
                live_lines = LedgerLine.all_objects.filter(ledger_entry=e)
                debit = sum(
                    (l.debit or 0) for l in live_lines if not l.is_deleted
                )
                credit = sum(
                    (l.credit or 0) for l in live_lines if not l.is_deleted
                )
                self.stdout.write(
                    f"     Entry #{e.pk}  "
                    f"entry_del={'Y' if e.is_deleted else 'N'}  "
                    f"amt={money(e.total_amount)}  "
                    f"lines_total_dr={money(debit)}  "
                    f"lines_total_cr={money(credit)}  "
                    f"date={e.date}"
                )
        self.stdout.write("")

        # ── Show purchases & their ledger entries ──
        self.stdout.write(self.style.MIGRATE_HEADING(
            "── PURCHASES & THEIR LEDGER ENTRIES ──"
        ))
        for p in Purchase.all_objects.filter(vendor=contact).order_by('date'):
            self.stdout.write(
                f"  Purchase {p.purchase_number}  ID={p.pk}  "
                f"date={p.date}  amt={money(p.grand_total)}  "
                f"deleted={'Y' if p.is_deleted else 'N'}"
            )
            entries = LedgerEntry.all_objects.filter(
                reference_id=p.pk, entry_type='purchase'
            )
            for e in entries:
                self.stdout.write(
                    f"     Entry #{e.pk}  "
                    f"entry_del={'Y' if e.is_deleted else 'N'}  "
                    f"amt={money(e.total_amount)}"
                )
        self.stdout.write("")

        # ── Show orphan purchase entries ──
        self.stdout.write(self.style.MIGRATE_HEADING(
            "── ORPHAN PURCHASE ENTRIES (entry's ref_id has no Purchase) ──"
        ))
        live_purchase_ids = set(
            Purchase.all_objects.values_list('pk', flat=True)
        )
        orphan_count = 0
        for e in LedgerEntry.all_objects.filter(entry_type='purchase'):
            if e.reference_id and e.reference_id not in live_purchase_ids:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Entry #{e.pk}  ref_id={e.reference_id}  "
                        f"deleted={'Y' if e.is_deleted else 'N'}  "
                        f"date={e.date}  desc={e.description}"
                    )
                )
                for ll in LedgerLine.all_objects.filter(ledger_entry=e):
                    self.stdout.write(
                        f"     Line #{ll.pk}  "
                        f"deleted={'Y' if ll.is_deleted else 'N'}  "
                        f"contact={ll.contact_id}  "
                        f"dr={money(ll.debit)}  cr={money(ll.credit)}"
                    )
                orphan_count += 1
        if orphan_count == 0:
            self.stdout.write("  (none)")
        self.stdout.write("")

        # ── Show live lines whose parent entry is deleted ──
        self.stdout.write(self.style.MIGRATE_HEADING(
            "── LIVE LINES WITH DELETED PARENT ENTRY ──"
        ))
        bad = LedgerLine.all_objects.filter(
            ledger_entry__is_deleted=True,
            is_deleted=False,
            contact=contact,
        )
        if bad.exists():
            for ll in bad:
                self.stdout.write(
                    self.style.ERROR(
                        f"  Line #{ll.pk}  entry#{ll.ledger_entry_id}  "
                        f"dr={money(ll.debit)}  cr={money(ll.credit)}  "
                        f"desc={ll.ledger_entry.description}"
                    )
                )
        else:
            self.stdout.write("  (none)")
        self.stdout.write("")