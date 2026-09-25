# accounting/views/statements.py
import csv
import logging
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.contrib import messages
from django.db.models import Q, Sum
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger

from ..models import (
    Contact, LedgerEntry, LedgerLine, Invoice, RepairJob,
    CompanyProfile, Purchase, Payment, CreditNote,
)
from .utils import is_htmx, redirect_to_staff
from ..decorators import handle_errors

# Excel generation
try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    openpyxl = None

logger = logging.getLogger(__name__)


# ============================================================
# HELPER: Orphan-safe ledger line filter
# ============================================================
def _valid_ledger_line_filter():
    """
    Returns a Q object that matches only ledger lines whose parent record
    (if any) still exists.

    Silently hides orphan entries left behind by legacy delete bugs —
    no manual cleanup command needed.

    Rule:
      - journal / opening entries → always valid (no reference)
      - purchase entries → parent Purchase must exist & not deleted
      - payment / advance entries → parent Payment must exist & not deleted
      - sales entries → parent Invoice must exist & not deleted
      - credit_note entries → parent CreditNote must exist & not deleted
    """
    from ..models import Purchase, Payment, Invoice, CreditNote

    return (
        Q(ledger_entry__entry_type__in=['journal', 'opening']) |
        Q(
            ledger_entry__entry_type='purchase',
            ledger_entry__reference_id__in=Purchase.objects.values('pk'),
        ) |
        Q(
            ledger_entry__entry_type__in=[
                'payment', 'advance_received', 'advance_paid',
            ],
            ledger_entry__reference_id__in=Payment.objects.values('pk'),
        ) |
        Q(
            ledger_entry__entry_type='sales',
            ledger_entry__reference_id__in=Invoice.objects.values('pk'),
        ) |
        Q(
            ledger_entry__entry_type='credit_note',
            ledger_entry__reference_id__in=CreditNote.objects.values('pk'),
        )
    )


# ============================================================
# HELPER: Get statement lines with filters + subledger filter
# ============================================================
def get_statement_lines(contact, date_from=None, date_to=None,
                        txn_type=None, search=None, subledger=None):
    """
    Returns filtered and annotated statement lines for a contact.

    subledger:
        'receivable' → only customer-side lines
        'payable'    → only vendor-side lines
        None         → all lines (legacy behaviour)
    """
    lines = LedgerLine.objects.filter(contact=contact) \
        .filter(ledger_entry__is_deleted=False) \
        .filter(_valid_ledger_line_filter()) \
        .exclude(ledger_entry__entry_type='opening') \
        .select_related('ledger_entry', 'account') \
        .order_by('ledger_entry__date', 'ledger_entry__id')

    if subledger:
        lines = lines.filter(subledger_type=subledger)

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)
        
    if txn_type == 'invoice':
        lines = lines.filter(ledger_entry__entry_type='sales')

    elif txn_type == 'purchase':
        lines = lines.filter(ledger_entry__entry_type='purchase')

    elif txn_type == 'payment':
        # Payment filter shows:
        #   (a) Actual Payment module entries
        #   (b) Legacy journal entries that were payment-like
        #       (receipt/payment/advance_received/advance_paid)
        lines = lines.filter(
            Q(ledger_entry__entry_type__in=[
                'payment', 'advance_received', 'advance_paid',
            ]) |
            Q(
                ledger_entry__entry_type='journal',
                ledger_entry__journal_type__in=[
                    'receipt', 'payment',
                    'advance_received', 'advance_paid',
                ]
            )
        )

    elif txn_type == 'discount':
        # Discount journals (allowed / received)
        lines = lines.filter(
            ledger_entry__entry_type='journal',
            ledger_entry__journal_type='discount',
        )

    elif txn_type == 'journal':
        # True journals only — exclude payment-like entries
        lines = lines.filter(
            ledger_entry__entry_type='journal'
        ).exclude(
            ledger_entry__journal_type__in=[
                'receipt', 'payment',
                'advance_received', 'advance_paid',
            ]
        )

    return lines


# ============================================================
# HELPER: Carried-forward opening balance (customer side)
# ============================================================
def _get_customer_opening_balance(contact, date_from=None):
    """
    Customer statement opening balance.
    - Starts from contact.opening_balance
    - If date_from given, adds up prior *receivable* transactions
    - Customer: +ve = owes us (Dr - Cr)
    """
    opening_balance = contact.opening_balance or Decimal('0')

    if date_from:
        prior_lines = LedgerLine.objects.filter(contact=contact) \
            .filter(ledger_entry__is_deleted=False) \
            .filter(_valid_ledger_line_filter()) \
            .exclude(ledger_entry__entry_type='opening') \
            .filter(ledger_entry__date__lt=date_from, subledger_type='receivable')

        prior_debit = prior_lines.aggregate(Sum('debit'))['debit__sum'] or Decimal('0')
        prior_credit = prior_lines.aggregate(Sum('credit'))['credit__sum'] or Decimal('0')
        opening_balance = opening_balance + prior_debit - prior_credit

    return opening_balance


# ============================================================
# HELPER: Carried-forward opening balance (vendor side)
# ============================================================
def _get_vendor_opening_balance(contact, date_from=None):
    """
    Vendor statement opening balance.
    - Starts from contact.opening_balance
    - If date_from given, adds up prior *payable* transactions
    - Vendor: +ve = we owe (Cr - Dr)
    """
    opening_balance = contact.opening_balance or Decimal('0')

    if date_from:
        prior_lines = LedgerLine.objects.filter(contact=contact) \
            .filter(ledger_entry__is_deleted=False) \
            .filter(_valid_ledger_line_filter()) \
            .exclude(ledger_entry__entry_type='opening') \
            .filter(ledger_entry__date__lt=date_from, subledger_type='payable')

        prior_debit = prior_lines.aggregate(Sum('debit'))['debit__sum'] or Decimal('0')
        prior_credit = prior_lines.aggregate(Sum('credit'))['credit__sum'] or Decimal('0')
        opening_balance = opening_balance + prior_credit - prior_debit

    return opening_balance


# ============================================================
# HELPER: Batch fetch to eliminate N+1 queries
# ============================================================
def _batch_fetch_related(entry_lines):
    """
    One-shot batch fetch of all models referenced by statement lines.

    Reduces N+1 queries (50-75 queries on a 25-row page) to just 6 queries.
    Returns a dict of lookup maps for use inside the row-building loops.
    """
    inv_ids = set()
    pur_ids = set()
    pay_ids = set()
    cn_ids = set()
    entry_ids = set()

    for line in entry_lines:
        entry = line.ledger_entry
        entry_ids.add(entry.pk)
        if not entry.reference_id:
            continue
        etype = entry.entry_type
        if etype == 'sales':
            inv_ids.add(entry.reference_id)
        elif etype == 'purchase':
            pur_ids.add(entry.reference_id)
        elif etype in ('payment', 'advance_received', 'advance_paid'):
            pay_ids.add(entry.reference_id)
        elif etype == 'credit_note':
            cn_ids.add(entry.reference_id)

    invoices = {inv.pk: inv for inv in Invoice.objects.filter(pk__in=inv_ids)}
    repairs = {
        r.invoice_id: r
        for r in RepairJob.objects.filter(invoice_id__in=inv_ids).only(
            'id', 'invoice_id', 'device_model', 'job_number'
        )
    }
    purchases = {p.pk: p for p in Purchase.objects.filter(pk__in=pur_ids)}
    payments = {
        p.pk: p
        for p in Payment.objects.filter(pk__in=pay_ids).select_related('bank_account')
    }
    credit_notes = {
        cn.pk: cn
        for cn in CreditNote.objects.filter(pk__in=cn_ids).select_related('invoice')
    }

    # All lines of every referenced entry — needed for discount/advance detection
    entries_with_lines = (
        LedgerEntry.objects.filter(pk__in=entry_ids).prefetch_related('lines__account')
    )
    entry_lines_map = {e.pk: list(e.lines.all()) for e in entries_with_lines}

    return {
        'invoices': invoices,
        'repairs': repairs,
        'purchases': purchases,
        'payments': payments,
        'credit_notes': credit_notes,
        'entry_lines': entry_lines_map,
    }


# ============================================================
# HELPER: Line-level professional description
# ============================================================
def _describe_line(line, entry, related):
    """
    Build a professional, line-level description for a statement row.

    Detects and labels:
      - Sales Invoice (main debit)
      - Discount Allowed (credit on receivable paired with Dr 5010)
      - Advance Applied (credit on receivable paired with Dr 1012)
      - Repair Invoice (with device model)
      - Purchase
      - Payment/Advance Received/Paid (with method + bank)
      - Journal (with sub-type)
      - Credit Note (with CN number and reference invoice)

    Returns a dict with all display fields.
    """
    result = {
        'description': entry.description or '',
        'reference': '',
        'invoice_no': None,
        'invoice_id': None,
        'repair_job_id': None,
        'device_model': None,
        'purchase_id': None,
        'purchase_no': None,
        'payment_id': None,
        'journal_entry_id': None,
    }

    account_code = getattr(line.account, 'code', '') if line.account else ''

    # ── SALES INVOICE ──
    if entry.entry_type == 'sales' and entry.reference_id:
        inv = related['invoices'].get(entry.reference_id)
        if not inv:
            return result

        result['invoice_no'] = inv.invoice_number
        result['invoice_id'] = inv.pk
        result['reference'] = inv.invoice_number

        repair = related['repairs'].get(inv.pk)
        if repair:
            result['repair_job_id'] = repair.pk
            result['device_model'] = repair.device_model

        if account_code == '1011':
            if line.debit > 0 and line.credit == 0:
                # Main invoice debit — customer owes us
                if repair:
                    result['description'] = (
                        f"Repair Invoice {inv.invoice_number} — "
                        f"{repair.device_model or 'device'}"
                    )
                else:
                    result['description'] = f"Sales Invoice {inv.invoice_number}"

            elif line.credit > 0 and line.debit == 0:
                # Credit on receivable — could be discount OR advance applied
                entry_lines = related['entry_lines'].get(entry.pk, [])
                has_advance_side = any(
                    getattr(l.account, 'code', '') == '1012' for l in entry_lines
                )
                has_discount_side = any(
                    getattr(l.account, 'code', '') == '5010' for l in entry_lines
                )

                if has_advance_side and not has_discount_side:
                    result['description'] = f"Advance Applied → {inv.invoice_number}"
                elif has_discount_side and not has_advance_side:
                    result['description'] = f"Discount Allowed on {inv.invoice_number}"
                elif has_discount_side and has_advance_side:
                    # Both present — distinguish by amount
                    inv_discount = inv.discount_amount or Decimal('0')
                    if line.credit == inv_discount:
                        result['description'] = f"Discount Allowed on {inv.invoice_number}"
                    else:
                        result['description'] = f"Advance Applied → {inv.invoice_number}"
                else:
                    result['description'] = f"Adjustment — {inv.invoice_number}"

        elif account_code == '1012':
            # Dr 1012 — advance being applied from liability side
            result['description'] = f"Advance Adjustment — {inv.invoice_number}"

        return result

    # ── PURCHASE ──
    if entry.entry_type == 'purchase' and entry.reference_id:
        pur = related['purchases'].get(entry.reference_id)
        if not pur:
            return result
        result['purchase_id'] = pur.pk
        result['purchase_no'] = pur.purchase_number
        result['reference'] = pur.purchase_number
        result['description'] = f"Purchase {pur.purchase_number}"
        return result

    # ── PAYMENT / ADVANCE ──
    if entry.entry_type in ('payment', 'advance_received', 'advance_paid') and entry.reference_id:
        pay = related['payments'].get(entry.reference_id)
        if not pay:
            return result

        result['payment_id'] = pay.pk
        result['reference'] = f"PMT-{pay.pk:04d}"

        prefix = 'Advance' if pay.is_advance else 'Payment'
        direction = 'Received' if pay.direction == 'received' else 'Paid'
        method = pay.get_method_display()

        description = f"{prefix} {direction} — {method}"
        if pay.bank_account:
            description += f" ({pay.bank_account.name})"
        if pay.upi_ref:
            description += f" · UPI: {pay.upi_ref}"
        elif pay.reference:
            description += f" · Ref: {pay.reference}"

        result['description'] = description
        return result

    # ── JOURNAL ──
    if entry.entry_type == 'journal':
        result['journal_entry_id'] = entry.pk
        result['reference'] = f"JRN-{entry.pk:04d}"

        jtype = (entry.journal_type or '').lower()
        base = (entry.description or '').strip()

        if jtype == 'discount_allowed':
            result['description'] = f"Discount Allowed — {base}" if base else "Discount Allowed"
        elif jtype == 'discount_received':
            result['description'] = f"Discount Received — {base}" if base else "Discount Received"
        elif jtype == 'receipt':
            result['description'] = f"Receipt — {base}" if base else "Receipt from Customer"
        elif jtype == 'payment':
            result['description'] = f"Payment — {base}" if base else "Payment to Vendor"
        elif jtype == 'advance_received':
            result['description'] = f"Advance Received — {base}" if base else "Advance from Customer"
        elif jtype == 'advance_paid':
            result['description'] = f"Advance Paid — {base}" if base else "Advance to Vendor"
        elif jtype == 'discount':
            result['description'] = f"Discount — {base}" if base else "Discount Adjustment"
        else:
            result['description'] = base or "Journal Entry"
        return result

    # ── CREDIT NOTE ──
    if entry.entry_type == 'credit_note' and entry.reference_id:
        cn = related['credit_notes'].get(entry.reference_id)
        if cn:
            inv_ref = cn.invoice.invoice_number if cn.invoice else '—'
            result['reference'] = cn.credit_note_number
            result['description'] = (
                f"Credit Note {cn.credit_note_number} (against {inv_ref})"
            )
        return result

    # ── FALLBACK (bank_manual, opening) ──
    result['description'] = entry.description or entry.get_entry_type_display()
    return result


# ============================================================
# HELPER: Build customer statement rows (receivable-only)
# ============================================================
def _build_customer_statement_data(contact, date_from=None, date_to=None,
                                    txn_type=None, search=None):
    """
    Returns (statement_lines, dr_total, cr_total, closing, opening,
             opening_as_on_date, opening_label).

    Uses line-level descriptions + batch fetch.
    """
    lines_qs = get_statement_lines(
        contact, date_from, date_to, txn_type, search, subledger='receivable'
    )
    lines_list = list(lines_qs)
    related = _batch_fetch_related(lines_list)

    opening = _get_customer_opening_balance(contact, date_from)

    if date_from:
        try:
            opening_as_on_date = datetime.strptime(date_from, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            opening_as_on_date = None
        opening_label = "Opening Balance (Carried Forward)"
    else:
        opening_as_on_date = contact.opening_balance_date
        opening_label = "Opening Balance"

    running = opening
    statement_lines = []

    for line in lines_list:
        entry = line.ledger_entry
        desc_data = _describe_line(line, entry, related)

        if line.debit > 0:
            running += line.debit
            debit_amt = line.debit
            credit_amt = Decimal('0')
        else:
            running -= line.credit
            debit_amt = Decimal('0')
            credit_amt = line.credit

        if debit_amt == 0 and credit_amt == 0:
            continue

        # Search filter — display-only, running balance unaffected
        if search:
            s = search.lower()
            match = (
                (desc_data['invoice_no'] and s in desc_data['invoice_no'].lower())
                or (desc_data['description'] and s in desc_data['description'].lower())
                or (desc_data['device_model'] and s in desc_data['device_model'].lower())
            )
            if not match:
                continue

        statement_lines.append({
            'date': entry.date,
            'description': desc_data['description'],
            'debit': debit_amt,
            'credit': credit_amt,
            'balance': running,
            'invoice_no': desc_data['invoice_no'],
            'device_model': desc_data['device_model'],
            'invoice_id': desc_data['invoice_id'],
            'repair_job_id': desc_data['repair_job_id'],
        })

    dr_total = sum(l['debit'] for l in statement_lines) if statement_lines else Decimal('0')
    cr_total = sum(l['credit'] for l in statement_lines) if statement_lines else Decimal('0')

    return {
        'statement_lines': statement_lines,
        'opening': opening,
        'opening_as_on_date': opening_as_on_date,
        'opening_label': opening_label,
        'dr_total': dr_total,
        'cr_total': cr_total,
        'closing': running,
    }


# ============================================================
# HELPER: Build vendor statement rows (payable-only)
# ============================================================
def _build_vendor_statement_data(contact, date_from=None, date_to=None,
                                  search=None):
    """
    Returns (statement_lines, totals, closing, opening, opening_label).
    Vendor perspective: balance = opening + credit − debit.
    """
    lines_qs = (
        LedgerLine.objects.filter(contact=contact, subledger_type='payable')
        .filter(ledger_entry__is_deleted=False)
        .filter(_valid_ledger_line_filter())
        .exclude(ledger_entry__entry_type='opening')
        .select_related('ledger_entry', 'account')
        .order_by('ledger_entry__date', 'ledger_entry__id')
    )

    if date_from:
        lines_qs = lines_qs.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines_qs = lines_qs.filter(ledger_entry__date__lte=date_to)

    lines_list = list(lines_qs)
    related = _batch_fetch_related(lines_list)

    opening_balance = _get_vendor_opening_balance(contact, date_from)

    if date_from:
        try:
            opening_as_on_date = datetime.strptime(date_from, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            opening_as_on_date = None
        opening_label = "Opening Balance (Carried Forward)"
    else:
        opening_as_on_date = contact.opening_balance_date
        opening_label = "Opening Balance"

    running = opening_balance
    statement_lines = []

    for line in lines_list:
        entry = line.ledger_entry
        desc_data = _describe_line(line, entry, related)

        debit = line.debit or Decimal('0')
        credit = line.credit or Decimal('0')
        running = running + credit - debit

        if search:
            s = search.lower()
            match = (
                (desc_data['reference'] and s in desc_data['reference'].lower())
                or (desc_data['description'] and s in desc_data['description'].lower())
            )
            if not match:
                continue

        statement_lines.append({
            'date': entry.date,
            'type': entry.get_entry_type_display(),
            'reference': desc_data['reference'] or '—',
            'description': desc_data['description'],
            'debit': debit,
            'credit': credit,
            'balance': running,
            'purchase_id': desc_data['purchase_id'],
        })

    total_debit = sum(l['debit'] for l in statement_lines) if statement_lines else Decimal('0')
    total_credit = sum(l['credit'] for l in statement_lines) if statement_lines else Decimal('0')

    return {
        'statement_lines': statement_lines,
        'opening_balance': opening_balance,
        'opening_as_on_date': opening_as_on_date,
        'opening_label': opening_label,
        'closing_balance': running,
        'total_debit': total_debit,
        'total_credit': total_credit,
    }


# ============================================================
# CUSTOMER STATEMENT - EXCEL
# ============================================================
def customer_statement_excel(request, contact_id):
    if openpyxl is None:
        messages.error(request, "Openpyxl library is not installed.")
        return redirect_to_staff('customer_statement', contact_id=contact_id)

    contact = get_object_or_404(Contact, pk=contact_id,
                                contact_type__in=['customer', 'both'])

    # For 'both' type, use combined excel
    if contact.contact_type == 'both':
        return redirect(
            f"/statements/combined/{contact.pk}/excel/"
            + (f"?{request.GET.urlencode()}" if request.GET else "")
        )
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '')

    lines = get_statement_lines(contact, date_from, date_to,
                                txn_type, search,
                                subledger='receivable')

    opening = _get_customer_opening_balance(contact, date_from)
    if date_from:
        opening_label = f"Opening Balance (Carried Forward, as on {date_from})"
    elif contact.opening_balance_date:
        opening_label = f"Opening Balance (as on {contact.opening_balance_date.strftime('%d-%m-%Y')})"
    else:
        opening_label = "Opening Balance"

    running_balance = opening
    total_debit = Decimal('0')
    total_credit = Decimal('0')
    statement_data = []

    for line in lines:
        entry = line.ledger_entry
        invoice = None
        repair_job = None

        if entry.entry_type == 'sales' and entry.reference_id:
            try:
                invoice = Invoice.objects.get(pk=entry.reference_id)
                repair_job = RepairJob.objects.filter(invoice=invoice).first()
            except Invoice.DoesNotExist:
                pass

        desc = entry.description
        invoice_no = None
        device_model = None
        action_text = None

        if invoice:
            invoice_no = invoice.invoice_number
            if repair_job:
                device_model = repair_job.device_model
                action_text = repair_job.action_taken
                desc = f"Inv: {invoice.invoice_number} Device: {device_model or '-'}"
            else:
                desc = f"Invoice {invoice.invoice_number}"
        elif entry.entry_type == 'payment':
            desc = f"Payment - {entry.description}"
        elif entry.entry_type == 'journal':
            desc = f"Journal - {entry.description}"
            
        if line.debit > 0:
            running_balance += line.debit
            debit_amt = line.debit
            credit_amt = Decimal('0')
        else:
            running_balance -= line.credit
            debit_amt = Decimal('0')
            credit_amt = line.credit

        if search:
            s = search.lower()
            if (not (invoice_no and s in invoice_no.lower())
                and not (desc and s in desc.lower())
                and not (action_text and s in action_text.lower())):
                continue

        statement_data.append({
            'date': entry.date,
            'type': entry.get_entry_type_display(),
            'reference': f"{entry.entry_type}#{entry.reference_id}" if entry.reference_id else '',
            'description': desc,
            'action': action_text or '',
            'debit': debit_amt,
            'credit': credit_amt,
            'balance': running_balance,
        })

    closing_balance = running_balance

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Customer Statement"

    header_font = Font(bold=True, color="FFFFFF", size=12)
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center")
    border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    money_format = '#,##0.00'

    company = CompanyProfile.get_instance()
    ws.merge_cells('A1:H1')
    ws['A1'] = company.name
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A2:H2')
    ws['A2'] = f"Customer Statement - {contact.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws.merge_cells('A3:H3')
    period = f"Period: {date_from if date_from else 'Start'} to {date_to if date_to else 'Today'}"
    ws['A3'] = period
    ws['A3'].alignment = Alignment(horizontal="center")

    headers = ['Date', 'Transaction Type', 'Reference', 'Description',
               'Action', 'Debit (₹)', 'Credit (₹)', 'Balance (₹)']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=5, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = border

    row = 6
    ws.cell(row=row, column=4, value=opening_label)
    ws.cell(row=row, column=8, value=float(opening))
    ws.cell(row=row, column=8).number_format = money_format
    for col in range(1, 9):
        ws.cell(row=row, column=col).border = border
    row += 1

    for trans in statement_data:
        ws.cell(row=row, column=1, value=trans['date'].strftime("%d-%m-%Y"))
        ws.cell(row=row, column=2, value=trans['type'])
        ws.cell(row=row, column=3, value=trans['reference'])
        ws.cell(row=row, column=4, value=trans['description'])
        action = trans.get('action', '')
        if action and len(action) > 80:
            action = action[:80] + '...'
        ws.cell(row=row, column=5, value=action)
        ws.cell(row=row, column=6, value=float(trans['debit']) if trans['debit'] else "")
        if trans['debit']:
            ws.cell(row=row, column=6).number_format = money_format
        ws.cell(row=row, column=7, value=float(trans['credit']) if trans['credit'] else "")
        if trans['credit']:
            ws.cell(row=row, column=7).number_format = money_format
        ws.cell(row=row, column=8, value=float(trans['balance']))
        ws.cell(row=row, column=8).number_format = money_format
        for col in range(1, 9):
            ws.cell(row=row, column=col).border = border
        row += 1

    ws.cell(row=row, column=4, value="Closing Balance")
    ws.cell(row=row, column=8, value=float(closing_balance))
    ws.cell(row=row, column=8).number_format = money_format
    for col in range(1, 9):
        ws.cell(row=row, column=col).border = border
        ws.cell(row=row, column=col).font = Font(bold=True)
    row += 1

    ws.cell(row=row, column=4, value="Total")
    ws.cell(row=row, column=6, value=float(total_debit))
    ws.cell(row=row, column=6).number_format = money_format
    ws.cell(row=row, column=7, value=float(total_credit))
    ws.cell(row=row, column=7).number_format = money_format
    for col in range(1, 9):
        ws.cell(row=row, column=col).border = border
        ws.cell(row=row, column=col).font = Font(bold=True)

    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 18
    ws.column_dimensions['C'].width = 18
    ws.column_dimensions['D'].width = 35
    ws.column_dimensions['E'].width = 40
    ws.column_dimensions['F'].width = 15
    ws.column_dimensions['G'].width = 15
    ws.column_dimensions['H'].width = 15

    ws.freeze_panes = 'A6'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="customer_statement_{contact.name}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
    wb.save(response)
    return response


# ============================================================
# CUSTOMER STATEMENT - WHATSAPP
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def customer_statement_whatsapp(request, contact_id):
    contact = get_object_or_404(Contact, pk=contact_id)

    # 'both' type → route to combined WhatsApp (consistency with other views)
    if contact.contact_type == 'both':
        query = request.GET.urlencode()
        url = f"/statements/combined/{contact.pk}/whatsapp/"
        if query:
            url += f"?{query}"
        return redirect(url)

    company = CompanyProfile.get_instance()
    phone = contact.phone

    if not phone:
        messages.error(request, "Customer phone number not available.")
        return redirect_to_staff('customer_statement', contact_id=contact_id)

    phone_clean = phone.replace(' ', '').replace('-', '').replace('+', '')
    if not phone_clean:
        messages.error(request, "Invalid phone number.")
        return redirect_to_staff('customer_statement', contact_id=contact_id)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '')

    opening = _get_customer_opening_balance(contact, date_from)
    lines = get_statement_lines(contact, date_from, date_to,
                                txn_type, search,
                                subledger='receivable')

    running_balance = opening
    for line in lines:
        if line.debit > 0:
            running_balance += line.debit
        else:
            running_balance -= line.credit
    closing = running_balance

    period = f"{date_from if date_from else 'Start'} to {date_to if date_to else 'Today'}"

    message = f"""📊 *Customer Statement*

👤 Customer: {contact.name}
📅 Period: {period}
💰 Opening Balance: ₹{opening:,.2f}
💵 Closing Balance: ₹{closing:,.2f}

For complete statement, please visit our portal https://a1computersolutions.onrender.com/

Thank you,
{company.name}
{company.phone}"""

    encoded_msg = quote(message)
    whatsapp_url = f"https://wa.me/{phone_clean}?text={encoded_msg}"
    return redirect(whatsapp_url)


# ============================================================
# CUSTOMER STATEMENT - HTML VIEW (With Pagination)
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def customer_statement(request, contact_id):
    """Customer statement — receivable side only."""
    contact = get_object_or_404(Contact, pk=contact_id)

    if contact.contact_type == 'both':
        return redirect('accounting:combined_statement', contact_id=contact.pk)

    if request.GET.get('reset'):
        date_from = date_to = txn_type = search = ''
        sort = 'desc'
    else:
        date_from = request.GET.get('date_from', '')
        date_to = request.GET.get('date_to', '')
        txn_type = request.GET.get('txn_type', '')
        search = request.GET.get('search', '')
        sort = request.GET.get('sort', 'desc')

    page_number = request.GET.get('page', 1)

    data = _build_customer_statement_data(
        contact, date_from or None, date_to or None, txn_type or None, search or None
    )
    statement_lines = data['statement_lines']

    if sort == 'desc':
        statement_lines.reverse()

    paginator = Paginator(statement_lines, 25)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    context = {
        'contact': contact,
        'statement_lines': page_obj.object_list,
        'page_obj': page_obj,
        'opening': data['opening'],
        'opening_as_on_date': data['opening_as_on_date'],
        'opening_label': data['opening_label'],
        'dr_total': data['dr_total'],
        'cr_total': data['cr_total'],
        'closing': data['closing'],
        'closing_as_on_date': date_to,
        'date_from': date_from,
        'date_to': date_to,
        'txn_type': txn_type,
        'search': search,
        'sort': sort,
    }

    if is_htmx(request):
        return render(request, 'statements/partials/customer_statement_table.html', context)
    return render(request, 'statements/customer_statement.html', context)

# ============================================================
# CUSTOMER STATEMENT - PRINT
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def customer_statement_print(request, contact_id):
    """Print-friendly customer statement."""
    contact = get_object_or_404(Contact, pk=contact_id)

    if contact.contact_type == 'both':
        return redirect(
            f"/statements/combined/{contact.pk}/print/"
            + (f"?{request.GET.urlencode()}" if request.GET else "")
        )

    company = CompanyProfile.get_instance()

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '')

    data = _build_customer_statement_data(
        contact, date_from or None, date_to or None, txn_type or None, search or None
    )

    context = {
        'contact': contact,
        'statement_lines': data['statement_lines'],
        'opening': data['opening'],
        'opening_as_on_date': data['opening_as_on_date'],
        'opening_label': data['opening_label'],
        'dr_total': data['dr_total'],
        'cr_total': data['cr_total'],
        'closing': data['closing'],
        'closing_as_on_date': date_to,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
        'date_from': date_from,
        'date_to': date_to,
        'txn_type': txn_type,
        'search': search,
    }
    return render(request, 'statements/print_statement.html', context)

# ============================================================
# VENDOR STATEMENT - HTML (With Pagination)
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def vendor_statement(request, contact_id):
    """Vendor statement — payable side only."""
    contact = get_object_or_404(Contact, pk=contact_id, contact_type__in=['vendor', 'both'])

    if contact.contact_type == 'both':
        return redirect('accounting:combined_statement', contact_id=contact.pk)

    if request.GET.get('reset'):
        date_from = date_to = search = ''
        sort = 'desc'
    else:
        date_from = request.GET.get('date_from', '')
        date_to = request.GET.get('date_to', '')
        search = request.GET.get('search', '')
        sort = request.GET.get('sort', 'desc')

    page_number = request.GET.get('page', 1)

    data = _build_vendor_statement_data(
        contact, date_from or None, date_to or None, search or None
    )
    statement_lines = data['statement_lines']

    if sort == 'desc':
        statement_lines.reverse()

    paginator = Paginator(statement_lines, 25)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    context = {
        'contact': contact,
        'statement_lines': page_obj.object_list,
        'page_obj': page_obj,
        'opening_balance': data['opening_balance'],
        'opening_as_on_date': data['opening_as_on_date'],
        'opening_label': data['opening_label'],
        'closing_balance': data['closing_balance'],
        'closing_as_on_date': date_to,
        'total_debit': data['total_debit'],
        'total_credit': data['total_credit'],
        'date_from': date_from,
        'date_to': date_to,
        'search': search,
        'sort': sort,
        'company': CompanyProfile.get_instance(),
    }

    if is_htmx(request):
        return render(request, 'statements/partials/vendor_statement_table.html', context)
    return render(request, 'statements/vendor_statement.html', context)

# ============================================================
# VENDOR STATEMENT - CSV
# ============================================================
def vendor_statement_csv(request, contact_id):
    contact = get_object_or_404(Contact, pk=contact_id, contact_type__in=['vendor', 'both'])

    # For 'both' type, redirect to combined statement Excel (filters preserved)
    if contact.contact_type == 'both':
        query = request.GET.urlencode()
        url = f"/statements/combined/{contact.pk}/excel/"
        if query:
            url += f"?{query}"
        return redirect(url)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    search = request.GET.get('search', '').strip()

    lines = LedgerLine.objects.filter(
        contact=contact,
        subledger_type='payable',
    ).exclude(
        ledger_entry__entry_type='opening'
    ).select_related('ledger_entry').order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)

    opening_balance = _get_vendor_opening_balance(contact, date_from)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="vendor_statement_{contact.name}_{datetime.now().strftime("%Y%m%d")}.csv"'
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow(['Date', 'Transaction Type', 'Reference', 'Description', 'Debit (₹)', 'Credit (₹)', 'Balance (₹)'])

    if date_from:
        opening_label = f"Opening Balance (Carried Forward, as on {date_from})"
    elif contact.opening_balance_date:
        opening_label = f"Opening Balance (as on {contact.opening_balance_date.strftime('%d-%m-%Y')})"
    else:
        opening_label = "Opening Balance"

    writer.writerow(['', opening_label, '', '', '', '', f'{opening_balance:.2f}'])

    running_balance = opening_balance
    for line in lines:
        entry = line.ledger_entry
        debit = line.debit
        credit = line.credit
        running_balance = running_balance + credit - debit

        if search:
            s = search.lower()
            if not (
                (entry.description and s in entry.description.lower())
                or (entry.reference_id and s in str(entry.reference_id).lower())
            ):
                continue

        writer.writerow([
            entry.date.strftime("%d-%m-%Y"),
            entry.get_entry_type_display(),
            f"{entry.entry_type}#{entry.reference_id}" if entry.reference_id else '',
            entry.description,
            f'{debit:.2f}',
            f'{credit:.2f}',
            f'{running_balance:.2f}',
        ])
    return response


# ============================================================
# VENDOR STATEMENT - PRINT
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def vendor_statement_print(request, contact_id):
    contact = get_object_or_404(Contact, pk=contact_id, contact_type__in=['vendor', 'both'])

    if contact.contact_type == 'both':
        query = request.GET.urlencode()
        url = f"/statements/combined/{contact.pk}/print/"
        if query:
            url += f"?{query}"
        return redirect(url)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    search = request.GET.get('search', '')

    data = _build_vendor_statement_data(
        contact, date_from or None, date_to or None, search or None
    )

    company = CompanyProfile.get_instance()

    context = {
        'contact': contact,
        'statement_lines': data['statement_lines'],
        'opening_balance': data['opening_balance'],
        'opening_as_on_date': data['opening_as_on_date'],
        'opening_label': data['opening_label'],
        'closing_balance': data['closing_balance'],
        'closing_as_on_date': date_to,
        'total_debit': data['total_debit'],
        'total_credit': data['total_credit'],
        'date_from': date_from,
        'date_to': date_to,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
    }
    return render(request, 'statements/print_vendor_statement.html', context)

# ============================================================
# VENDOR STATEMENT - EXCEL
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def vendor_statement_excel(request, contact_id):
    if openpyxl is None:
        messages.error(request, "Openpyxl library is not installed. Please install it to export Excel.")
        return redirect_to_staff('vendor_statement', contact_id=contact_id)

    contact = get_object_or_404(Contact, pk=contact_id, contact_type__in=['vendor', 'both'])

    # For 'both' type, redirect to combined excel
    if contact.contact_type == 'both':
        query = request.GET.urlencode()
        url = f"/statements/combined/{contact.pk}/excel/"
        if query:
            url += f"?{query}"
        return redirect(url)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    search = request.GET.get('search', '').strip()
    lines = LedgerLine.objects.filter(
        contact=contact,
        subledger_type='payable',
    ).exclude(
        ledger_entry__entry_type='opening'
    ).select_related('ledger_entry').order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)

    opening_balance = _get_vendor_opening_balance(contact, date_from)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Vendor Statement"

    header_font = Font(bold=True, color="FFFFFF", size=12)
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center")
    border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    money_format = '#,##0.00'

    company = CompanyProfile.get_instance()
    ws.merge_cells('A1:G1')
    ws['A1'] = company.name
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A2:G2')
    ws['A2'] = f"Vendor Statement - {contact.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws.merge_cells('A3:G3')
    period = f"Period: {date_from if date_from else 'Start'} to {date_to if date_to else 'Today'}"
    ws['A3'] = period
    ws['A3'].alignment = Alignment(horizontal="center")

    headers = ['Date', 'Transaction Type', 'Reference', 'Description', 'Debit (₹)', 'Credit (₹)', 'Balance (₹)']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=5, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = border

    row = 6
    if date_from:
        opening_text = f"Opening Balance (Carried Forward, as on {date_from})"
    elif contact.opening_balance_date:
        opening_text = f"Opening Balance (as on {contact.opening_balance_date.strftime('%d-%m-%Y')})"
    else:
        opening_text = "Opening Balance"
    ws.cell(row=row, column=4, value=opening_text)
    ws.cell(row=row, column=7, value=float(opening_balance))
    ws.cell(row=row, column=7).number_format = money_format
    for col in range(1, 8):
        ws.cell(row=row, column=col).border = border
    row += 1

    running_balance = opening_balance
    total_debit = Decimal('0')
    total_credit = Decimal('0')

    for line in lines:
        entry = line.ledger_entry
        trans_type = entry.get_entry_type_display()
        ref = ''
        if entry.entry_type == 'purchase' and entry.reference_id:
            try:
                pur = Purchase.objects.get(pk=entry.reference_id)
                ref = pur.purchase_number
            except Purchase.DoesNotExist:
                ref = f"Purchase #{entry.reference_id}"
        elif entry.entry_type == 'payment' and entry.reference_id:
            ref = f"PMT-{entry.reference_id:04d}"
        elif entry.entry_type == 'journal' and entry.reference_id:
            ref = f"Journal #{entry.reference_id}"

        debit = line.debit
        credit = line.credit
        running_balance = running_balance + credit - debit
        total_debit += debit
        total_credit += credit

        if search:
            s = search.lower()
            if not (
                (ref and s in ref.lower())
                or (entry.description and s in entry.description.lower())
            ):
                continue

        ws.cell(row=row, column=1, value=entry.date.strftime("%d-%m-%Y"))
        ws.cell(row=row, column=2, value=trans_type)
        ws.cell(row=row, column=3, value=ref)
        ws.cell(row=row, column=4, value=entry.description)
        ws.cell(row=row, column=5, value=float(debit) if debit else "")
        if debit:
            ws.cell(row=row, column=5).number_format = money_format
        ws.cell(row=row, column=6, value=float(credit) if credit else "")
        if credit:
            ws.cell(row=row, column=6).number_format = money_format
        ws.cell(row=row, column=7, value=float(running_balance))
        ws.cell(row=row, column=7).number_format = money_format

        for col in range(1, 8):
            ws.cell(row=row, column=col).border = border
        row += 1

    ws.cell(row=row, column=4, value="Closing Balance")
    ws.cell(row=row, column=7, value=float(running_balance))
    ws.cell(row=row, column=7).number_format = money_format
    for col in range(1, 8):
        ws.cell(row=row, column=col).border = border
        ws.cell(row=row, column=col).font = Font(bold=True)
    row += 1

    ws.cell(row=row, column=4, value="Total")
    ws.cell(row=row, column=5, value=float(total_debit))
    ws.cell(row=row, column=5).number_format = money_format
    ws.cell(row=row, column=6, value=float(total_credit))
    ws.cell(row=row, column=6).number_format = money_format
    for col in range(1, 8):
        ws.cell(row=row, column=col).border = border
        ws.cell(row=row, column=col).font = Font(bold=True)

    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 18
    ws.column_dimensions['C'].width = 18
    ws.column_dimensions['D'].width = 40
    ws.column_dimensions['E'].width = 15
    ws.column_dimensions['F'].width = 15
    ws.column_dimensions['G'].width = 15

    ws.freeze_panes = 'A6'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="vendor_statement_{contact.name}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
    wb.save(response)
    return response


# ============================================================
# COMBINED STATEMENT — Helpers
# ============================================================
def _get_combined_opening_balances(contact, date_from=None):
    """
    Compute opening receivable/payable for combined statement.
    Contact's opening_balance sits on the correct side based on contact_type.
    If date_from given, carries forward all prior transactions.
    """
    base = contact.opening_balance or Decimal('0')
    opening_recv = Decimal('0')
    opening_pay = Decimal('0')

    if contact.contact_type in ('customer', 'both'):
        opening_recv = base
    else:
        opening_pay = base

    if not date_from:
        return (opening_recv.quantize(Decimal('0.01')),
                opening_pay.quantize(Decimal('0.01')))

    prior = LedgerLine.objects.filter(
        contact=contact,
        ledger_entry__date__lt=date_from,
        ledger_entry__is_deleted=False,
    ).filter(_valid_ledger_line_filter()).exclude(ledger_entry__entry_type='opening')

    pr = prior.filter(subledger_type='receivable').aggregate(
        dr=Sum('debit'), cr=Sum('credit')
    )
    opening_recv += (pr['dr'] or Decimal('0')) - (pr['cr'] or Decimal('0'))

    pp = prior.filter(subledger_type='payable').aggregate(
        dr=Sum('debit'), cr=Sum('credit')
    )
    opening_pay += (pp['cr'] or Decimal('0')) - (pp['dr'] or Decimal('0'))

    return (opening_recv.quantize(Decimal('0.01')),
            opening_pay.quantize(Decimal('0.01')))


def _build_combined_rows(contact, date_from=None, date_to=None,
                          txn_type=None, search=None, sort='desc'):
    """
    Build combined statement data (rows + totals + opening/closing).

    Both sides computed independently; net position = receivable - payable.

    sort='desc' → newest first
    sort='asc'  → oldest first (bank-style)

    Each row carries:
      recv_dr, recv_cr, pay_dr, pay_cr   — original split
      running_recv, running_pay, net     — running positions
      debit_amount, credit_amount        — merged single-column (bank-style)

    Performance: batch-fetches all related records — no N+1.
    Descriptions: line-level (discount / advance / journal sub-type).
    """
    opening_recv, opening_pay = _get_combined_opening_balances(contact, date_from)

    qs = (
        LedgerLine.objects.filter(contact=contact)
        .filter(ledger_entry__is_deleted=False)
        .filter(_valid_ledger_line_filter())
        .exclude(ledger_entry__entry_type='opening')
        .select_related('ledger_entry', 'account')
        .order_by('ledger_entry__date', 'ledger_entry__id')
    )

    if date_from:
        qs = qs.filter(ledger_entry__date__gte=date_from)
    if date_to:
        qs = qs.filter(ledger_entry__date__lte=date_to)

    if txn_type == 'invoice':
        qs = qs.filter(ledger_entry__entry_type='sales')
    elif txn_type == 'purchase':
        qs = qs.filter(ledger_entry__entry_type='purchase')
    elif txn_type == 'payment':
        qs = qs.filter(
            Q(ledger_entry__entry_type__in=[
                'payment', 'advance_received', 'advance_paid',
            ]) |
            Q(
                ledger_entry__entry_type='journal',
                ledger_entry__journal_type__in=[
                    'receipt', 'payment',
                    'advance_received', 'advance_paid',
                ]
            )
        )
    elif txn_type == 'discount':
        qs = qs.filter(
            ledger_entry__entry_type='journal',
            ledger_entry__journal_type='discount',
        )
    elif txn_type == 'journal':
        qs = qs.filter(
            ledger_entry__entry_type='journal'
        ).exclude(
            ledger_entry__journal_type__in=[
                'receipt', 'payment',
                'advance_received', 'advance_paid',
            ]
        )

    lines_list = list(qs)
    related = _batch_fetch_related(lines_list)

    running_recv = opening_recv
    running_pay = opening_pay
    rows = []

    total_recv_dr = Decimal('0')
    total_recv_cr = Decimal('0')
    total_pay_dr = Decimal('0')
    total_pay_cr = Decimal('0')

    _RECV_CODES = frozenset({'1011', '1012'})
    _PAY_CODES = frozenset({'2011', '1014'})

    for line in lines_list:
        entry = line.ledger_entry
        sub = line.subledger_type

        # Fallback for legacy data — derive subledger from account code
        if not sub and line.contact_id:
            try:
                code = line.account.code
                if code in _RECV_CODES:
                    sub = 'receivable'
                elif code in _PAY_CODES:
                    sub = 'payable'
            except Exception:
                pass

        if sub not in ('receivable', 'payable'):
            continue

        desc_data = _describe_line(line, entry, related)

        # Running balances — always updated, search is display-only
        recv_dr = recv_cr = pay_dr = pay_cr = Decimal('0')

        if sub == 'receivable':
            recv_dr = line.debit or Decimal('0')
            recv_cr = line.credit or Decimal('0')
            running_recv = running_recv + recv_dr - recv_cr
            total_recv_dr += recv_dr
            total_recv_cr += recv_cr
        else:
            pay_dr = line.debit or Decimal('0')
            pay_cr = line.credit or Decimal('0')
            running_pay = running_pay + pay_cr - pay_dr
            total_pay_dr += pay_dr
            total_pay_cr += pay_cr

        # Search filter — display-only
        if search:
            s = search.lower()
            match = (
                (desc_data['invoice_no'] and s in desc_data['invoice_no'].lower())
                or (desc_data['reference'] and s in desc_data['reference'].lower())
                or (desc_data['description'] and s in desc_data['description'].lower())
                or (desc_data['device_model'] and s in desc_data['device_model'].lower())
            )
            if not match:
                continue

        debit_amount = (recv_dr + pay_dr).quantize(Decimal('0.01'))
        credit_amount = (recv_cr + pay_cr).quantize(Decimal('0.01'))

        rows.append({
            'date': entry.date,
            'entry_type': entry.entry_type,
            'entry_type_display': entry.get_entry_type_display(),
            'description': desc_data['description'],
            'reference': desc_data['reference'],
            'invoice_no': desc_data['invoice_no'],
            'device_model': desc_data['device_model'],
            'invoice_id': desc_data['invoice_id'],
            'repair_job_id': desc_data['repair_job_id'],
            'purchase_id': desc_data['purchase_id'],
            'purchase_no': desc_data['purchase_no'],
            'payment_id': desc_data['payment_id'],
            'journal_entry_id': desc_data['journal_entry_id'],
            'recv_dr': recv_dr,
            'recv_cr': recv_cr,
            'pay_dr': pay_dr,
            'pay_cr': pay_cr,
            'running_recv': running_recv,
            'running_pay': running_pay,
            'net': running_recv - running_pay,
            'debit_amount': debit_amount,
            'credit_amount': credit_amount,
        })

    if sort == 'desc':
        rows.reverse()

    total_debit = (total_recv_dr + total_pay_dr).quantize(Decimal('0.01'))
    total_credit = (total_recv_cr + total_pay_cr).quantize(Decimal('0.01'))

    return {
        'rows': rows,
        'opening_recv': opening_recv,
        'opening_pay': opening_pay,
        'opening_net': (opening_recv - opening_pay).quantize(Decimal('0.01')),
        'closing_recv': running_recv.quantize(Decimal('0.01')),
        'closing_pay': running_pay.quantize(Decimal('0.01')),
        'net_position': (running_recv - running_pay).quantize(Decimal('0.01')),
        'total_recv_dr': total_recv_dr.quantize(Decimal('0.01')),
        'total_recv_cr': total_recv_cr.quantize(Decimal('0.01')),
        'total_pay_dr': total_pay_dr.quantize(Decimal('0.01')),
        'total_pay_cr': total_pay_cr.quantize(Decimal('0.01')),
        'total_debit': total_debit,
        'total_credit': total_credit,
    }

# ============================================================
# COMBINED STATEMENT — HTML View
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def combined_statement(request, contact_id, is_print=None):
    """Combined statement (receivable + payable) for any contact type.

    Stage 4:
      - Default sort = 'asc' (bank-style chronological)
      - view_mode = 'bank' (single balance) | 'detailed' (two-sided)
      - Bank mode forces ASC regardless of sort param
    """
    contact = get_object_or_404(Contact, pk=contact_id)

    if request.GET.get('reset'):
        return redirect('accounting:combined_statement', contact_id=contact_id)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '').strip()
    sort = request.GET.get('sort', 'asc')
    view_mode = request.GET.get('view', 'bank')
    if view_mode not in ('bank', 'detailed'):
        view_mode = 'bank'
    page_number = request.GET.get('page', 1)

    # Print always chronological & bank-style
    print_requested = (is_print == '1') or (request.GET.get('print') == '1')

    # Bank style is always chronological
    if view_mode == 'bank' or print_requested:
        effective_sort = 'asc'
    else:
        effective_sort = sort

    data = _build_combined_rows(
        contact,
        date_from or None,
        date_to or None,
        txn_type or None,
        search or None,
        sort=effective_sort,
    )
    rows = data.pop('rows')

    # Opening "as on" date
    if date_from:
        try:
            opening_as_on_date = datetime.strptime(date_from, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            opening_as_on_date = None
    else:
        opening_as_on_date = contact.opening_balance_date

    # Unfiltered period totals (all transaction types)
    period_qs = LedgerLine.objects.filter(contact=contact) \
        .exclude(ledger_entry__entry_type='opening')
    if date_from:
        period_qs = period_qs.filter(ledger_entry__date__gte=date_from)
    if date_to:
        period_qs = period_qs.filter(ledger_entry__date__lte=date_to)

    recv_agg = period_qs.filter(
        subledger_type='receivable',
        ledger_entry__is_deleted=False,
    ).aggregate(dr=Sum('debit'), cr=Sum('credit'))
    
    pay_agg = period_qs.filter(
        subledger_type='payable',
        ledger_entry__is_deleted=False,
    ).aggregate(dr=Sum('debit'), cr=Sum('credit'))

    period_billed = (recv_agg['dr'] or Decimal('0')).quantize(Decimal('0.01'))
    period_received = (recv_agg['cr'] or Decimal('0')).quantize(Decimal('0.01'))
    period_purchased = (pay_agg['cr'] or Decimal('0')).quantize(Decimal('0.01'))
    period_paid = (pay_agg['dr'] or Decimal('0')).quantize(Decimal('0.01'))

    paginator = Paginator(rows, 25)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    company = CompanyProfile.get_instance()

    # Base query string (used by view-mode toggle buttons)
    qp = request.GET.copy()
    qp.pop('view', None)
    qp.pop('page', None)
    base_query_string = qp.urlencode()

    context = {
        'contact': contact,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
        'rows': page_obj.object_list,
        'page_obj': page_obj,
        'date_from': date_from,
        'date_to': date_to,
        'txn_type': txn_type,
        'search': search,
        'sort': effective_sort,
        'view_mode': view_mode,
        'base_query_string': base_query_string,
        'opening_as_on_date': opening_as_on_date,
        'period_billed': period_billed,
        'period_received': period_received,
        'period_purchased': period_purchased,
        'period_paid': period_paid,
        **data,
    }

    if print_requested:
        context['rows'] = rows
        return render(request, 'statements/combined_statement_print.html', context)

    if is_htmx(request):
        if view_mode == 'detailed':
            return render(request, 'statements/partials/combined_statement_table_detailed.html', context)
        return render(request, 'statements/partials/combined_statement_table.html', context)
    return render(request, 'statements/combined_statement.html', context)

# ============================================================
# COMBINED STATEMENT — Excel (Bank Style)
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def combined_statement_excel(request, contact_id):
    """Export combined statement to Excel (bank-style, single balance column)."""
    if openpyxl is None:
        messages.error(request, "Openpyxl library is not installed.")
        return redirect_to_staff('combined_statement', contact_id=contact_id)

    contact = get_object_or_404(Contact, pk=contact_id)
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '').strip()

    data = _build_combined_rows(
        contact,
        date_from or None,
        date_to or None,
        txn_type or None,
        search or None,
        sort='asc',   # always chronological for excel
    )
    rows = data.pop('rows')

    # Opening "as on" date
    if date_from:
        try:
            opening_as_on_date = datetime.strptime(date_from, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            opening_as_on_date = None
    else:
        opening_as_on_date = contact.opening_balance_date

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Statement of Account"

    title_font = Font(bold=True, size=14, color="1F4E78")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    opening_fill = PatternFill(start_color="EEF2F7", end_color="EEF2F7", fill_type="solid")
    total_fill = PatternFill(start_color="E2E6EA", end_color="E2E6EA", fill_type="solid")
    closing_fill = PatternFill(start_color="D4EDDA", end_color="D4EDDA", fill_type="solid")
    alt_fill = PatternFill(start_color="F8F9FA", end_color="F8F9FA", fill_type="solid")

    thin = Side(style='thin', color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center', wrap_text=True)
    right = Alignment(horizontal='right', vertical='center')
    money_fmt = '#,##0.00'

    company = CompanyProfile.get_instance()

    # ---- Title block ----
    ws.merge_cells('A1:F1')
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = title_font
    ws['A1'].alignment = center

    ws.merge_cells('A2:F2')
    ws['A2'] = f"Statement of Account — {contact.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws['A2'].alignment = center

    ws.merge_cells('A3:F3')
    period = f"Period: {date_from or 'Beginning'} to {date_to or 'Today'}"
    ws['A3'] = period
    ws['A3'].alignment = center

    # ---- Snapshot rows ----
    ws.merge_cells('A4:F4')
    opening_text = f"Opening Balance (B/F): ₹{data['opening_net']:,.2f}"
    if opening_as_on_date:
        opening_text += f"  (as on {opening_as_on_date.strftime('%d-%m-%Y')})"
    ws['A4'] = opening_text
    ws['A4'].font = Font(bold=True, size=10)
    ws['A4'].alignment = center

    ws.merge_cells('A5:F5')
    closing_text = f"Closing Balance (C/F): ₹{data['net_position']:,.2f}"
    if date_to:
        closing_text += f"  (as on {date_to})"
    ws['A5'] = closing_text
    ws['A5'].font = Font(bold=True, size=10)
    ws['A5'].alignment = center

    # ---- Header row (row 7) ----
    headers = ['Date', 'Particulars', 'Ref', 'Debit (₹)', 'Credit (₹)', 'Balance']
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=7, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center
        c.border = border

    row = 8

    # ---- Opening Balance row ----
    opening_date_str = opening_as_on_date.strftime('%d-%m-%Y') if opening_as_on_date else '—'
    ws.cell(row=row, column=1, value=opening_date_str)
    ws.cell(row=row, column=2, value='Opening Balance (B/F)')
    ws.cell(row=row, column=3, value='—')
    ws.cell(row=row, column=4, value='')
    ws.cell(row=row, column=5, value='')
    ws.cell(row=row, column=6, value=float(data['opening_net']))
    ws.cell(row=row, column=6).number_format = money_fmt
    for col in range(1, 7):
        c = ws.cell(row=row, column=col)
        c.border = border
        c.fill = opening_fill
        c.font = Font(bold=True)
        if col == 1 or col == 3:
            c.alignment = center
        elif col == 6:
            c.alignment = right
        else:
            c.alignment = left
    row += 1

    # ---- Transaction rows ----
    for idx, r in enumerate(rows):
        desc = r['description']
        ref = r['reference'] or '—'

        ws.cell(row=row, column=1, value=r['date'].strftime('%d-%m-%Y'))
        ws.cell(row=row, column=2, value=desc)
        ws.cell(row=row, column=3, value=ref)
        ws.cell(row=row, column=4, value=float(r['debit_amount']) if r['debit_amount'] else '')
        ws.cell(row=row, column=5, value=float(r['credit_amount']) if r['credit_amount'] else '')
        ws.cell(row=row, column=6, value=float(r['net']))
        ws.cell(row=row, column=6).number_format = money_fmt

        if r['debit_amount']:
            ws.cell(row=row, column=4).number_format = money_fmt
        if r['credit_amount']:
            ws.cell(row=row, column=5).number_format = money_fmt

        for col in range(1, 7):
            c = ws.cell(row=row, column=col)
            c.border = border
            if idx % 2 == 1:
                c.fill = alt_fill
            if col == 1 or col == 3:
                c.alignment = center
            elif col in (4, 5, 6):
                c.alignment = right
            else:
                c.alignment = left
        row += 1

    # ---- Period Totals row ----
    ws.cell(row=row, column=2, value='Period Totals →')
    ws.cell(row=row, column=2).font = Font(bold=True)
    ws.cell(row=row, column=2).alignment = right
    ws.cell(row=row, column=4, value=float(data['total_debit'])).number_format = money_fmt
    ws.cell(row=row, column=5, value=float(data['total_credit'])).number_format = money_fmt
    for col in range(1, 7):
        c = ws.cell(row=row, column=col)
        c.border = border
        c.fill = total_fill
        c.font = Font(bold=True)
        if col in (4, 5):
            c.alignment = right
    row += 1

    # ---- Closing Balance row ----
    ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    ws.cell(row=row, column=2, value='Closing Balance (C/F)')
    ws.cell(row=row, column=2).font = Font(bold=True, size=11)
    ws.cell(row=row, column=2).alignment = right
    ws.cell(row=row, column=6, value=float(data['net_position'])).number_format = money_fmt
    ws.cell(row=row, column=6).font = Font(bold=True, size=11)
    ws.cell(row=row, column=6).alignment = right
    for col in range(1, 7):
        c = ws.cell(row=row, column=col)
        c.border = border
        c.fill = closing_fill

    # ---- Column widths ----
    widths = [12, 45, 15, 15, 15, 16]
    for idx, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = w

    ws.freeze_panes = 'A8'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    safe_name = contact.name.replace(' ', '_').replace('/', '_')
    response['Content-Disposition'] = (
        f'attachment; filename="statement_{safe_name}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
    )
    wb.save(response)
    return response

# ============================================================
# COMBINED STATEMENT — WhatsApp
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def combined_statement_whatsapp(request, contact_id):
    """Share combined statement summary via WhatsApp."""
    contact = get_object_or_404(Contact, pk=contact_id)
    company = CompanyProfile.get_instance()

    if not contact.phone:
        messages.error(request, "This contact has no phone number.")
        return redirect_to_staff('combined_statement', contact_id=contact_id)

    phone_clean = contact.phone.replace(' ', '').replace('-', '').replace('+', '')
    if not phone_clean:
        messages.error(request, "Invalid phone number.")
        return redirect_to_staff('combined_statement', contact_id=contact_id)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    data = _build_combined_rows(contact, date_from or None, date_to or None)
    period = f"{date_from or 'Start'} to {date_to or 'Today'}"

    net = data['net_position']
    if net > 0:
        net_line = f"Rs.{net:,.2f} (they owe us)"
    elif net < 0:
        net_line = f"Rs.{abs(net):,.2f} (we owe them)"
    else:
        net_line = "Settled"

    message = (
        f"📊 *Account Statement*\n\n"
        f"👤 Party: {contact.name}\n"
        f"📅 Period: {period}\n\n"
        f"🔵 Receivable: Rs.{data['closing_recv']:,.2f}\n"
        f"🟡 Payable:    Rs.{data['closing_pay']:,.2f}\n"
        f"⚖️ *Net: {net_line}*\n\n"
        f"Regards,\n{company.name}\n{company.phone or ''}"
    )

    encoded = quote(message)
    return redirect(f"https://wa.me/{phone_clean}?text={encoded}")

# ============================================================
# VENDOR STATEMENT - WHATSAPP
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def vendor_statement_whatsapp(request, contact_id):
    contact = get_object_or_404(Contact, pk=contact_id)

    # 'both' type → combined WhatsApp (consistency)
    if contact.contact_type == 'both':
        query = request.GET.urlencode()
        url = f"/statements/combined/{contact.pk}/whatsapp/"
        if query:
            url += f"?{query}"
        return redirect(url)

    company = CompanyProfile.get_instance()
    phone = contact.phone

    if not phone:
        messages.error(request, "Vendor phone number not available.")
        return redirect_to_staff('vendor_statement', contact_id=contact_id)

    phone_clean = phone.replace(' ', '').replace('-', '').replace('+', '')
    if not phone_clean:
        messages.error(request, "Invalid phone number.")
        return redirect_to_staff('vendor_statement', contact_id=contact_id)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    opening = _get_vendor_opening_balance(contact, date_from)

    lines = LedgerLine.objects.filter(
        contact=contact,
        subledger_type='payable',
    ).exclude(
        ledger_entry__entry_type='opening'
    ).order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)

    running_balance = opening
    for line in lines:
        debit = line.debit
        credit = line.credit
        running_balance = running_balance + credit - debit
    closing = running_balance

    period = f"{date_from if date_from else 'Start'} to {date_to if date_to else 'Today'}"

    message = f"""📊 *Vendor Statement*

🏢 Vendor: {contact.name}
📅 Period: {period}
💰 Opening Balance: ₹{opening:,.2f}
💵 Closing Balance: ₹{closing:,.2f}

For complete statement, please visit our portal https://a1computersolutions.onrender.com/

Thank you,
{company.name}
{company.phone or ''}"""

    encoded_msg = quote(message)
    whatsapp_url = f"https://wa.me/{phone_clean}?text={encoded_msg}"
    return redirect(whatsapp_url)