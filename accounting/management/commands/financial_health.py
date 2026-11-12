"""
Read-only financial consistency check for invoices, purchase bills,
payments and ledger entries.

The cached totals on Invoice/Purchase/Contact are derived from allocation
rows and ledger lines. Old bugs could leave those caches stale, which shows
up as a wrong "Due" figure even though the underlying rows are intact.

Usage:
    python manage.py financial_health            # report only (safe)
    python manage.py financial_health --fix      # rewrite derived caches

--fix NEVER changes user-entered amounts (payment amount, allocation
amounts, invoice line prices). It only recomputes paid_amount, balance_due,
payment_status, purchase settlement flags, contact balances and bank
balances from their source rows.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum

from accounting.models import (
    BankAccount,
    Contact,
    Invoice,
    LedgerEntry,
    Payment,
    Purchase,
    PurchaseAllocation,
)

TOLERANCE = Decimal('0.005')
STEP = Decimal('0.01')
ZERO = Decimal('0')


def _sum(qs):
    return qs.aggregate(t=Sum('amount'))['t'] or ZERO


class Command(BaseCommand):
    help = 'Report (and optionally repair) derived financial totals'

    def add_arguments(self, parser):
        parser.add_argument(
            '--fix',
            action='store_true',
            default=False,
            help='Rewrite stale derived totals (never touches source amounts).',
        )

    def handle(self, *args, **options):
        apply_changes = options['fix']
        if apply_changes:
            self.stdout.write(self.style.ERROR(
                'FIX MODE — derived totals will be rewritten.'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                'REPORT ONLY — add --fix to rewrite derived totals.'
            ))
        self.stdout.write('')

        problems = 0
        changed_contacts = set()

        # ── 1. Invoice cached payment state ──
        stale_invoices = 0
        for inv in Invoice.objects.all().iterator():
            actual_paid = (
                _sum(inv.payment_allocations.all())
                + _sum(inv.advance_adjustments.all())
            ).quantize(STEP)
            if actual_paid <= 0:
                expected_status = 'unpaid'
            elif inv.grand_total > 0 and actual_paid >= inv.grand_total:
                expected_status = 'paid'
            else:
                expected_status = 'partial'
            expected_due = max(
                ZERO, inv.grand_total - actual_paid
            ).quantize(STEP)
            expected_paid = actual_paid.quantize(STEP)

            drift = (
                abs((inv.paid_amount or ZERO) - expected_paid) > TOLERANCE
                or abs((inv.balance_due or ZERO) - expected_due) > TOLERANCE
                or inv.payment_status != expected_status
            )
            if not drift:
                continue

            stale_invoices += 1
            problems += 1
            self.stdout.write(
                f'  Invoice {inv.invoice_number}: stored paid={inv.paid_amount} '
                f'due={inv.balance_due} status={inv.payment_status} | '
                f'should be paid={expected_paid} due={expected_due} '
                f'status={expected_status}'
            )
            if apply_changes:
                Invoice.objects.filter(pk=inv.pk).update(
                    paid_amount=expected_paid,
                    balance_due=expected_due,
                    payment_status=expected_status,
                )
                if inv.customer_id:
                    changed_contacts.add(inv.customer_id)

        # ── 2. Purchase bill settlement ──
        stale_purchases = 0
        for purchase in Purchase.objects.all().iterator():
            actual = _sum(purchase.payment_allocations.all()).quantize(STEP)
            # A bill settled by hand ("Mark Paid", no allocation rows ever)
            # is a legitimate state, not drift.
            ever_allocated = PurchaseAllocation.all_objects.filter(
                purchase=purchase
            ).exists()
            manually_marked = purchase.paid and not ever_allocated
            if manually_marked:
                continue
            expected_paid_flag = actual >= purchase.grand_total and purchase.grand_total > 0
            drift = (
                abs((purchase.paid_amount or ZERO) - actual) > TOLERANCE
                or purchase.paid != expected_paid_flag
            )
            if not drift:
                continue
            stale_purchases += 1
            problems += 1
            self.stdout.write(
                f'  Purchase {purchase.purchase_number}: stored paid={purchase.paid_amount} '
                f'flag={purchase.paid} | allocated={actual}'
            )
            if apply_changes:
                purchase.recalc_settlement()

        # ── 3. Payments over-allocated ──
        over_allocated = 0
        for payment in Payment.objects.all().iterator():
            used = (
                _sum(payment.allocations.all())
                + _sum(payment.purchase_allocations.all())
                + _sum(payment.advance_adjustments.all())
            )
            if used > payment.allocation_pool + TOLERANCE:
                over_allocated += 1
                problems += 1
                self.stdout.write(self.style.ERROR(
                    f'  Payment #{payment.pk}: allocations {used} exceed '
                    f'pool {payment.allocation_pool} '
                    f'(amount {payment.amount} + discount {payment.discount_amount}) '
                    f'— review manually, not auto-fixed.'
                ))

        # ── 4. Ledger entries where debit != credit ──
        unbalanced = 0
        for entry in LedgerEntry.objects.all().iterator():
            debit = ZERO
            credit = ZERO
            for line in entry.lines.all():
                debit += line.debit or ZERO
                credit += line.credit or ZERO
            if abs(debit - credit) > TOLERANCE:
                unbalanced += 1
                problems += 1
                self.stdout.write(self.style.ERROR(
                    f'  Ledger entry #{entry.pk} ({entry.entry_type}, '
                    f'{entry.date}): debit {debit} != credit {credit} '
                    f'— review manually, not auto-fixed.'
                ))

        # ── 5. Contact + bank cached balances ──
        if apply_changes:
            for contact in Contact.objects.all().iterator():
                contact.recalc_balance()
                contact.recalc_advance_balance()
            for account in BankAccount.objects.all().iterator():
                account.recalculate_balance()

        self.stdout.write('')
        self.stdout.write(
            f'Invoices with stale totals: {stale_invoices}\n'
            f'Purchases with stale settlement: {stale_purchases}\n'
            f'Payments over-allocated: {over_allocated}\n'
            f'Unbalanced ledger entries: {unbalanced}\n'
            f'Total findings: {problems}'
        )
        if problems and not apply_changes:
            self.stdout.write(self.style.WARNING(
                'Nothing was changed. Re-run with --fix to repair derived totals.'
            ))
        elif apply_changes:
            self.stdout.write(self.style.SUCCESS(
                'Derived totals recalculated. Source amounts were not modified.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('All financial totals are consistent.'))
