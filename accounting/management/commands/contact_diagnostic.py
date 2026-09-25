# accounting/management/commands/contact_diagnostic.py
"""
Contact Diagnostic — dump everything about a contact's ledger.

Usage:
    python manage.py contact_diagnostic "Mukta"

Outputs:
    - Contact basic info
    - Opening balance entry
    - Every LedgerEntry with its LedgerLines
    - Grouped summaries: invoices, purchases, payments, journals, credit notes
    - Totals computed 3 different ways so we can spot the mismatch
"""

from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db.models import Sum

from accounting.models import (
    Contact, LedgerEntry, LedgerLine, Invoice, Purchase, Payment,
    RepairJob, CreditNote, AdvanceAdjustment,
)


def money(x):
    if x is None:
        return "0.00"
    return f"{Decimal(x).quantize(Decimal('0.01')):>12,.2f}"


class Command(BaseCommand):
    help = "Dump full ledger for a contact to diagnose statement mismatch"

    def add_arguments(self, parser):
        parser.add_argument('name', type=str, help='Contact name (partial match)')

    def handle(self, *args, **options):
        name_query = options['name']

        contacts = Contact.objects.filter(name__icontains=name_query)
        if not contacts.exists():
            self.stdout.write(self.style.ERROR(f"No contact found matching '{name_query}'"))
            return

        for contact in contacts:
            self._dump_contact(contact)

    def _dump_contact(self, contact):
        line = "=" * 90

        self.stdout.write(self.style.MIGRATE_HEADING(line))
        self.stdout.write(self.style.MIGRATE_HEADING(f"CONTACT: {contact.name}  (ID={contact.pk})"))
        self.stdout.write(self.style.MIGRATE_HEADING(line))

        # ── BASIC INFO ──
        self.stdout.write(f"Type             : {contact.get_contact_type_display()}")
        self.stdout.write(f"Phone            : {contact.phone or '—'}")
        self.stdout.write(f"Opening Balance  : {money(contact.opening_balance)}")
        self.stdout.write(f"Opening Bal Date : {contact.opening_balance_date or '—'}")
        self.stdout.write(f"")
        self.stdout.write(f"Stored Balances (cached on Contact model):")
        self.stdout.write(f"  Receivable        : {money(contact.receivable_balance)}")
        self.stdout.write(f"  Payable           : {money(contact.payable_balance)}")
        self.stdout.write(f"  Net (balance)     : {money(contact.balance)}")
        self.stdout.write(f"  Advance           : {money(contact.advance_balance)}")

        # ── ALL LEDGER LINES ──
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("── ALL LEDGER LINES ──"))
        self.stdout.write(
            f"{'Date':<12} {'Entry':<18} {'Account':<10} {'Sub':<10} "
            f"{'Debit':>12} {'Credit':>12} {'Desc'}"
        )
        self.stdout.write("-" * 90)

        lines_qs = (
            LedgerLine.objects
            .filter(contact=contact)
            .select_related('ledger_entry', 'account')
            .order_by('ledger_entry__date', 'ledger_entry__id')
        )

        total_debit = Decimal('0')
        total_credit = Decimal('0')

        for ll in lines_qs:
            entry = ll.ledger_entry
            code = ll.account.code if ll.account else '?'
            sub = ll.subledger_type or '—'
            desc = (entry.description or '')[:40]

            self.stdout.write(
                f"{str(entry.date):<12} "
                f"{entry.entry_type:<18} "
                f"{code:<10} "
                f"{sub:<10} "
                f"{money(ll.debit)} "
                f"{money(ll.credit)} "
                f"{desc}"
            )
            total_debit += ll.debit or Decimal('0')
            total_credit += ll.credit or Decimal('0')

        self.stdout.write("-" * 90)
        self.stdout.write(f"{'TOTAL':<54} {money(total_debit)} {money(total_credit)}")
        self.stdout.write("")

        # ── CORE DOCUMENTS ──
        self.stdout.write(self.style.MIGRATE_HEADING("── INVOICES (Sales) ──"))
        inv_total = Decimal('0')
        for inv in Invoice.objects.filter(customer=contact).order_by('date'):
            self.stdout.write(
                f"  {str(inv.date):<12} {inv.invoice_number:<12} "
                f"Grand={money(inv.grand_total)}  Paid={money(inv.paid_amount)}  "
                f"Due={money(inv.balance_due)}  [{inv.payment_status}]"
            )
            inv_total += inv.grand_total
        self.stdout.write(f"  Subtotal: {money(inv_total)}")
        self.stdout.write("")

        self.stdout.write(self.style.MIGRATE_HEADING("── REPAIR JOBS ──"))
        for rj in RepairJob.objects.filter(customer=contact).order_by('date_in'):
            inv_no = rj.invoice.invoice_number if rj.invoice else '—'
            self.stdout.write(
                f"  {str(rj.date_in):<12} {rj.job_number:<12} "
                f"Final={money(rj.final_amount)}  Invoice={inv_no}"
            )
        self.stdout.write("")

        self.stdout.write(self.style.MIGRATE_HEADING("── PURCHASES (Orders from this contact) ──"))
        pur_total = Decimal('0')
        for p in Purchase.objects.filter(vendor=contact).order_by('date'):
            self.stdout.write(
                f"  {str(p.date):<12} {p.purchase_number:<12} "
                f"Grand={money(p.grand_total)}  Paid={p.paid}"
            )
            pur_total += p.grand_total
        self.stdout.write(f"  Subtotal: {money(pur_total)}")
        self.stdout.write("")

        self.stdout.write(self.style.MIGRATE_HEADING("── PAYMENTS ──"))
        pay_recv = Decimal('0')
        pay_paid = Decimal('0')
        for pay in Payment.objects.filter(contact=contact).order_by('date'):
            marker = 'ADV' if pay.is_advance else '   '
            self.stdout.write(
                f"  {str(pay.date):<12} PMT-{pay.pk:04d} {marker} "
                f"{pay.direction:<10} {money(pay.amount)}  "
                f"Method={pay.method}"
            )
            if pay.direction == 'received':
                pay_recv += pay.amount
            else:
                pay_paid += pay.amount
        self.stdout.write(f"  Received: {money(pay_recv)}  Paid: {money(pay_paid)}")
        self.stdout.write("")

        self.stdout.write(self.style.MIGRATE_HEADING("── ADVANCE ADJUSTMENTS (applied to invoices) ──"))
        adv = AdvanceAdjustment.objects.filter(payment__contact=contact)
        if adv.exists():
            for a in adv:
                self.stdout.write(
                    f"  {str(a.date):<12} Payment#{a.payment.pk} → "
                    f"Invoice {a.invoice.invoice_number}  Amount={money(a.amount)}"
                )
        else:
            self.stdout.write("  (none)")
        self.stdout.write("")

        self.stdout.write(self.style.MIGRATE_HEADING("── CREDIT NOTES ──"))
        cn = CreditNote.objects.filter(customer=contact)
        cn_total = Decimal('0')
        for c in cn:
            self.stdout.write(
                f"  {str(c.date):<12} {c.credit_note_number:<10} "
                f"Against {c.invoice.invoice_number}  Total={money(c.total_amount)}"
            )
            cn_total += c.total_amount
        self.stdout.write(f"  Subtotal: {money(cn_total)}")
        self.stdout.write("")

        # ── THREE DIFFERENT CALCULATIONS ──
        self.stdout.write(self.style.MIGRATE_HEADING("── CALCULATION COMPARISON ──"))
        self.stdout.write("")

        # Method 1: Manual per user's logic
        opening = contact.opening_balance or Decimal('0')
        sales = inv_total  # from invoice table
        purchases = pur_total
        payments_recv = pay_recv
        cn_val = cn_total

        manual_net = opening + sales - purchases - payments_recv - cn_val
        self.stdout.write(self.style.WARNING("Method 1 — Manual (what you expect):"))
        self.stdout.write(f"  Opening         = {money(opening)}")
        self.stdout.write(f"  + Sales         = {money(sales)}")
        self.stdout.write(f"  + Purchases     = {money(purchases)}  (payable, reduces net)")
        self.stdout.write(f"  − Payments Recv = {money(payments_recv)}")
        self.stdout.write(f"  − Credit Notes  = {money(cn_val)}")
        self.stdout.write(f"  ─────────────────────────────────")
        self.stdout.write(f"  Net Mukta owes  = {money(manual_net)}")
        self.stdout.write("")

        # Method 2: Contact List logic (account code 1011, 1012, 2011, 1014)
        recv_by_code = LedgerLine.objects.filter(
            contact=contact, account__code__in=['1011', '1012']
        ).aggregate(dr=Sum('debit'), cr=Sum('credit'))
        pay_by_code = LedgerLine.objects.filter(
            contact=contact, account__code__in=['2011', '1014']
        ).aggregate(dr=Sum('debit'), cr=Sum('credit'))

        recv_val = (recv_by_code['dr'] or Decimal('0')) - (recv_by_code['cr'] or Decimal('0'))
        pay_val = (pay_by_code['cr'] or Decimal('0')) - (pay_by_code['dr'] or Decimal('0'))
        net_by_code = recv_val - pay_val if contact.contact_type != 'vendor' else pay_val

        self.stdout.write(self.style.WARNING("Method 2 — Contact List View (account code based):"))
        self.stdout.write(f"  Receivable (1011+1012) = {money(recv_val)}")
        self.stdout.write(f"  Payable (2011+1014)    = {money(pay_val)}")
        self.stdout.write(f"  Net                    = {money(net_by_code)}")
        self.stdout.write("")

        # Method 3: Statement logic (subledger_type based)
        recv_by_sub = LedgerLine.objects.filter(
            contact=contact, subledger_type='receivable'
        ).aggregate(dr=Sum('debit'), cr=Sum('credit'))
        pay_by_sub = LedgerLine.objects.filter(
            contact=contact, subledger_type='payable'
        ).aggregate(dr=Sum('debit'), cr=Sum('credit'))

        recv_sub = (recv_by_sub['dr'] or Decimal('0')) - (recv_by_sub['cr'] or Decimal('0'))
        pay_sub = (pay_by_sub['cr'] or Decimal('0')) - (pay_by_sub['dr'] or Decimal('0'))
        net_by_sub = recv_sub - pay_sub

        self.stdout.write(self.style.WARNING("Method 3 — Statement View (subledger_type based):"))
        self.stdout.write(f"  Receivable  = {money(recv_sub)}")
        self.stdout.write(f"  Payable     = {money(pay_sub)}")
        self.stdout.write(f"  Net         = {money(net_by_sub)}")
        self.stdout.write("")

        # ── THE VERDICT ──
        self.stdout.write(self.style.MIGRATE_HEADING("── VERDICT ──"))
        self.stdout.write(f"  Manual (expected):      {money(manual_net)}")
        self.stdout.write(f"  Contact List (Method 2):{money(net_by_code)}")
        self.stdout.write(f"  Statement (Method 3):   {money(net_by_sub)}")
        self.stdout.write("")

        if abs(manual_net - net_by_code) < Decimal('0.01'):
            self.stdout.write(self.style.SUCCESS("  ✅ Contact List MATCHES manual"))
        else:
            self.stdout.write(self.style.ERROR(
                f"  ❌ Contact List differs by {money(manual_net - net_by_code)}"
            ))

        if abs(manual_net - net_by_sub) < Decimal('0.01'):
            self.stdout.write(self.style.SUCCESS("  ✅ Statement MATCHES manual"))
        else:
            self.stdout.write(self.style.ERROR(
                f"  ❌ Statement differs by {money(manual_net - net_by_sub)}"
            ))

        self.stdout.write("")