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
    Contact, LedgerLine, Invoice, RepairJob, CompanyProfile,
    Purchase, Payment,
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
        .exclude(ledger_entry__entry_type='opening') \
        .select_related('ledger_entry') \
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
            .exclude(ledger_entry__entry_type='opening') \
            .filter(ledger_entry__date__lt=date_from,
                    subledger_type='receivable')

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
            .exclude(ledger_entry__entry_type='opening') \
            .filter(ledger_entry__date__lt=date_from,
                    subledger_type='payable')

        prior_debit = prior_lines.aggregate(Sum('debit'))['debit__sum'] or Decimal('0')
        prior_credit = prior_lines.aggregate(Sum('credit'))['credit__sum'] or Decimal('0')
        opening_balance = opening_balance + prior_credit - prior_debit

    return opening_balance


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

        if search:
            s = search.lower()
            if (not (invoice_no and s in invoice_no.lower())
                and not (desc and s in desc.lower())
                and not (action_text and s in action_text.lower())):
                continue

        if line.debit > 0:
            running_balance += line.debit
            debit_amt = line.debit
            credit_amt = Decimal('0')
            total_debit += debit_amt
        else:
            running_balance -= line.credit
            debit_amt = Decimal('0')
            credit_amt = line.credit
            total_credit += credit_amt

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
    contact = get_object_or_404(Contact, pk=contact_id)

    # For 'both' type contacts, redirect to combined statement
    # (customer_statement alone doesn't show payable side or net position)
    if contact.contact_type == 'both':
        return redirect('accounting:combined_statement', contact_id=contact.pk)

    if request.GET.get('reset'):
        date_from = ''
        date_to = ''
        txn_type = ''
        search = ''
    else:
        date_from = request.GET.get('date_from', '')
        date_to = request.GET.get('date_to', '')
        txn_type = request.GET.get('txn_type', '')
        search = request.GET.get('search', '')

    page_number = request.GET.get('page', 1)

    lines = get_statement_lines(contact, date_from, date_to,
                                txn_type, search,
                                subledger='receivable')

    opening = _get_customer_opening_balance(contact, date_from)

    if date_from:
        opening_as_on_date = date_from
        opening_label = "Opening Balance (Carried Forward)"
    else:
        opening_as_on_date = contact.opening_balance_date
        opening_label = "Opening Balance"

    running_balance = opening
    statement_lines = []

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
        issue_text = None
        action_text = None
        invoice_id = None
        repair_job_id = None

        if invoice:
            invoice_no = invoice.invoice_number
            invoice_id = invoice.id
            if repair_job:
                repair_job_id = repair_job.id
                device_model = repair_job.device_model
                issue_text = repair_job.issue_description
                action_text = repair_job.action_taken
                desc = f"Inv: {invoice.invoice_number} Device: {device_model or '-'}"
            else:
                desc = f"Invoice {invoice.invoice_number}"
        elif entry.entry_type in ('payment', 'advance_received', 'advance_paid'):
            desc = f"Payment — {entry.description}"

        elif entry.entry_type == 'journal':
            jtype = (entry.journal_type or '').lower()
            if jtype in ('receipt', 'payment', 'advance_received', 'advance_paid'):
                desc = entry.description or f"{jtype.replace('_', ' ').title()}"
            elif jtype == 'discount':
                desc = f"Discount — {entry.description}"
            else:
                desc = entry.description or "Journal Entry"

        if search:
            s = search.lower()
            match = (
                (invoice_no and s in invoice_no.lower())
                or (issue_text and s in issue_text.lower())
                or (action_text and s in action_text.lower())
                or (desc and s in desc.lower())
                or (device_model and s in device_model.lower())
            )
            if not match:
                continue

        if line.debit > 0:
            running_balance += line.debit
            debit_amt = line.debit
            credit_amt = Decimal('0')
        else:
            running_balance -= line.credit
            debit_amt = Decimal('0')
            credit_amt = line.credit

        if debit_amt == 0 and credit_amt == 0:
            continue

        statement_lines.append({
            'date': entry.date,
            'description': desc,
            'debit': debit_amt,
            'credit': credit_amt,
            'balance': running_balance,
            'invoice_no': invoice_no,
            'device_model': device_model,
            'issue': issue_text,
            'action': action_text,
            'invoice_id': invoice_id,
            'repair_job_id': repair_job_id,
        })

    dr_total = sum(l['debit'] for l in statement_lines) if statement_lines else Decimal('0')
    cr_total = sum(l['credit'] for l in statement_lines) if statement_lines else Decimal('0')
    closing = running_balance

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
        'opening': opening,
        'opening_as_on_date': opening_as_on_date,
        'opening_label': opening_label,
        'dr_total': dr_total,
        'cr_total': cr_total,
        'closing': closing,
        'closing_as_on_date': date_to,
        'date_from': date_from,
        'date_to': date_to,
        'txn_type': txn_type,
        'search': search,
    }

    if is_htmx(request):
        return render(request, 'statements/partials/customer_statement_table.html', context)
    return render(request, 'statements/customer_statement.html', context)


# ============================================================
# CUSTOMER STATEMENT - PRINT
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def customer_statement_print(request, contact_id):
    contact = get_object_or_404(Contact, pk=contact_id)

    # For 'both' type, redirect to combined print
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

    lines = get_statement_lines(contact, date_from, date_to,
                                txn_type, search,
                                subledger='receivable')

    opening = _get_customer_opening_balance(contact, date_from)

    if date_from:
        opening_as_on_date = date_from
        opening_label = "Opening Balance (Carried Forward)"
    else:
        opening_as_on_date = contact.opening_balance_date
        opening_label = "Opening Balance"

    running_balance = opening
    statement_lines = []

    for line in lines:
        entry = line.ledger_entry
        invoice = None
        repair_job = None
        device_model = None
        issue_text = None
        action_text = None

        if entry.entry_type == 'sales' and entry.reference_id:
            try:
                invoice = Invoice.objects.get(pk=entry.reference_id)
                repair_job = RepairJob.objects.filter(invoice=invoice).first()
            except Invoice.DoesNotExist:
                pass

        desc = entry.description

        if invoice:
            if repair_job:
                device_model = repair_job.device_model
                issue_text = repair_job.issue_description
                action_text = repair_job.action_taken
                desc = f"Inv: {invoice.invoice_number} Device: {device_model or '-'}"
            else:
                desc = f"Invoice {invoice.invoice_number}"
        elif entry.entry_type == 'payment':
            desc = f"Payment - {entry.description}"
        elif entry.entry_type == 'journal':
            desc = f"Journal - {entry.description}"

        if search:
            s = search.lower()
            match = (
                (invoice and s in invoice.invoice_number.lower())
                or (issue_text and s in issue_text.lower())
                or (action_text and s in action_text.lower())
                or (desc and s in desc.lower())
                or (device_model and s in device_model.lower())
            )
            if not match:
                continue

        if line.debit > 0:
            running_balance += line.debit
            debit_amt = line.debit
            credit_amt = Decimal('0')
        else:
            running_balance -= line.credit
            debit_amt = Decimal('0')
            credit_amt = line.credit

        statement_lines.append({
            'date': entry.date,
            'description': desc,
            'debit': debit_amt,
            'credit': credit_amt,
            'balance': running_balance,
            'device_model': device_model,
            'issue': issue_text,
            'action': action_text,
        })

    dr_total = sum(l['debit'] for l in statement_lines) if statement_lines else Decimal('0')
    cr_total = sum(l['credit'] for l in statement_lines) if statement_lines else Decimal('0')
    closing = running_balance

    context = {
        'contact': contact,
        'statement_lines': statement_lines,
        'opening': opening,
        'opening_as_on_date': opening_as_on_date,
        'opening_label': opening_label,
        'dr_total': dr_total,
        'cr_total': cr_total,
        'closing': closing,
        'closing_as_on_date': date_to,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name)),
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
    contact = get_object_or_404(Contact, pk=contact_id,
                                contact_type__in=['vendor', 'both'])

    # For 'both' type, redirect to combined statement
    if contact.contact_type == 'both':
        return redirect('accounting:combined_statement', contact_id=contact.pk)

    if request.GET.get('reset'):
        date_from = ''
        date_to = ''
        search = ''
    else:
        date_from = request.GET.get('date_from', '')
        date_to = request.GET.get('date_to', '')
        search = request.GET.get('search', '')

    page_number = request.GET.get('page', 1)

    lines = LedgerLine.objects.filter(contact=contact,
                                      subledger_type='payable') \
        .exclude(ledger_entry__entry_type='opening') \
        .select_related('ledger_entry') \
        .order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)

    opening_balance = _get_vendor_opening_balance(contact, date_from)

    if date_from:
        opening_as_on_date = date_from
        opening_label = "Opening Balance (Carried Forward)"
    else:
        opening_as_on_date = contact.opening_balance_date
        opening_label = "Opening Balance"

    running_balance = opening_balance
    statement_lines = []

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

        # Search filter
        if search:
            s = search.lower()
            if not (
                (ref and s in ref.lower())
                or (entry.description and s in entry.description.lower())
            ):
                continue

        debit = line.debit
        credit = line.credit
        running_balance = running_balance + credit - debit

        statement_lines.append({
            'date': entry.date,
            'type': trans_type,
            'reference': ref,
            'description': entry.description,
            'debit': debit,
            'credit': credit,
            'balance': running_balance,
        })

    closing_balance = running_balance
    total_debit = sum(l['debit'] for l in statement_lines) if statement_lines else Decimal('0')
    total_credit = sum(l['credit'] for l in statement_lines) if statement_lines else Decimal('0')

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
        'opening_balance': opening_balance,
        'opening_as_on_date': opening_as_on_date,
        'opening_label': opening_label,
        'closing_balance': closing_balance,
        'closing_as_on_date': date_to,
        'total_debit': total_debit,
        'total_credit': total_credit,
        'date_from': date_from,
        'date_to': date_to,
        'search': search,
        'company': CompanyProfile.get_instance(),
    }

    if is_htmx(request):
        return render(request, 'statements/partials/vendor_statement_table.html', context)
    return render(request, 'statements/vendor_statement.html', context)


# ============================================================
# VENDOR STATEMENT - CSV
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def vendor_statement_csv(request, contact_id):
    contact = get_object_or_404(Contact, pk=contact_id, contact_type__in=['vendor', 'both'])

    # For 'both' type, redirect to combined statement excel (CSV not supported for combined)
    if contact.contact_type == 'both':
        return redirect('accounting:combined_statement', contact_id=contact.pk)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

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

    # For 'both' type, redirect to combined statement print
    if contact.contact_type == 'both':
        query = request.GET.urlencode()
        url = f"/statements/combined/{contact.pk}/print/"
        if query:
            url += f"?{query}"
        return redirect(url)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

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

    if date_from:
        opening_as_on_date = date_from
        opening_label = "Opening Balance (Carried Forward)"
    else:
        opening_as_on_date = contact.opening_balance_date
        opening_label = "Opening Balance"

    running_balance = opening_balance
    statement_lines = []

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

        statement_lines.append({
            'date': entry.date,
            'type': trans_type,
            'reference': ref,
            'description': entry.description,
            'debit': debit,
            'credit': credit,
            'balance': running_balance,
        })

    closing_balance = running_balance
    total_debit = sum(l['debit'] for l in statement_lines) if statement_lines else Decimal('0')
    total_credit = sum(l['credit'] for l in statement_lines) if statement_lines else Decimal('0')
    company = CompanyProfile.get_instance()
    logo_exists = bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name))

    context = {
        'contact': contact,
        'statement_lines': statement_lines,
        'opening_balance': opening_balance,
        'opening_as_on_date': opening_as_on_date,
        'opening_label': opening_label,
        'closing_balance': closing_balance,
        'closing_as_on_date': date_to,
        'total_debit': total_debit,
        'total_credit': total_credit,
        'date_from': date_from,
        'date_to': date_to,
        'company': company,
        'logo_exists': logo_exists,
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
    ).exclude(ledger_entry__entry_type='opening')

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
                        txn_type=None, search=None):
    """
    Build combined statement data (rows + totals + opening/closing).
    Both sides computed independently; net position = receivable - payable.
    """
    opening_recv, opening_pay = _get_combined_opening_balances(contact, date_from)

    qs = LedgerLine.objects.filter(contact=contact) \
        .exclude(ledger_entry__entry_type='opening') \
        .select_related('ledger_entry') \
        .order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        qs = qs.filter(ledger_entry__date__gte=date_from)
    if date_to:
        qs = qs.filter(ledger_entry__date__lte=date_to)

    if txn_type == 'invoice':
        qs = qs.filter(ledger_entry__entry_type='sales')

    elif txn_type == 'purchase':
        qs = qs.filter(ledger_entry__entry_type='purchase')

    elif txn_type == 'payment':
        # Payment filter — includes legacy journal-payments
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
        # True journals only — exclude payment-like entries
        qs = qs.filter(
            ledger_entry__entry_type='journal'
        ).exclude(
            ledger_entry__journal_type__in=[
                'receipt', 'payment',
                'advance_received', 'advance_paid',
            ]
        )

    running_recv = opening_recv
    running_pay = opening_pay
    rows = []

    total_recv_dr = Decimal('0')
    total_recv_cr = Decimal('0')
    total_pay_dr = Decimal('0')
    total_pay_cr = Decimal('0')

    for line in qs:
        entry = line.ledger_entry
        sub = line.subledger_type

        if sub not in ('receivable', 'payable'):
            continue

        # ---- Enrich description ----
        description = entry.description
        reference = ''
        invoice_no = None
        device_model = None
        invoice_id = None
        repair_job_id = None

        if entry.entry_type == 'sales' and entry.reference_id:
            try:
                inv = Invoice.objects.get(pk=entry.reference_id)
                invoice_no = inv.invoice_number
                invoice_id = inv.id
                repair = RepairJob.objects.filter(invoice=inv).first()
                if repair:
                    repair_job_id = repair.id
                    device_model = repair.device_model
                    description = f"Repair Invoice: {device_model}"
                else:
                    description = "Sales Invoice"
                reference = inv.invoice_number
            except Invoice.DoesNotExist:
                pass

        elif entry.entry_type == 'purchase' and entry.reference_id:
            try:
                pur = Purchase.objects.get(pk=entry.reference_id)
                reference = pur.purchase_number
                description = "Purchase Bill"
            except Purchase.DoesNotExist:
                pass

        elif entry.entry_type in ('payment', 'advance_received', 'advance_paid') and entry.reference_id:
            try:
                pay = Payment.objects.get(pk=entry.reference_id)
                reference = f"PMT-{pay.id:04d}"
                method = pay.get_method_display()
                bank = pay.bank_account.name if pay.bank_account else 'Cash'
                direction = 'Received' if pay.direction == 'received' else 'Paid'
                prefix = 'Advance' if pay.is_advance else 'Payment'
                description = f"{prefix} {direction} · {method} · {bank}"
                if pay.upi_ref:
                    description += f" · UPI: {pay.upi_ref}"
                elif pay.reference:
                    description += f" · Ref: {pay.reference}"
            except Payment.DoesNotExist:
                pass

        elif entry.entry_type == 'journal':
            # Make description self-explanatory based on journal_type
            jtype = (entry.journal_type or '').lower()
            base_desc = entry.description or ''

            if jtype == 'receipt':
                description = f"Receipt — {base_desc}" if base_desc else "Receipt from Customer"
            elif jtype == 'payment':
                description = f"Payment — {base_desc}" if base_desc else "Payment to Vendor"
            elif jtype == 'advance_received':
                description = f"Advance Received — {base_desc}" if base_desc else "Advance from Customer"
            elif jtype == 'advance_paid':
                description = f"Advance Paid — {base_desc}" if base_desc else "Advance to Vendor"
            elif jtype == 'discount':
                description = f"Discount — {base_desc}" if base_desc else "Discount Adjustment"
            else:
                description = base_desc or "Journal Entry"

        # ---- Search filter ----
        if search:
            s = search.lower()
            match = (
                (invoice_no and s in invoice_no.lower())
                or (reference and s in reference.lower())
                or (description and s in description.lower())
                or (device_model and s in device_model.lower())
            )
            if not match:
                continue

        # ---- Running balances ----
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

        rows.append({
            'date': entry.date,
            'entry_type': entry.entry_type,
            'entry_type_display': entry.get_entry_type_display(),
            'description': description,
            'reference': reference,
            'invoice_no': invoice_no,
            'device_model': device_model,
            'invoice_id': invoice_id,
            'repair_job_id': repair_job_id,
            'recv_dr': recv_dr,
            'recv_cr': recv_cr,
            'pay_dr': pay_dr,
            'pay_cr': pay_cr,
            'running_recv': running_recv,
            'running_pay': running_pay,
            'net': running_recv - running_pay,
        })

    return {
        'rows': rows,
        'opening_recv': opening_recv,
        'opening_pay': opening_pay,
        'closing_recv': running_recv.quantize(Decimal('0.01')),
        'closing_pay': running_pay.quantize(Decimal('0.01')),
        'net_position': (running_recv - running_pay).quantize(Decimal('0.01')),
        'total_recv_dr': total_recv_dr.quantize(Decimal('0.01')),
        'total_recv_cr': total_recv_cr.quantize(Decimal('0.01')),
        'total_pay_dr': total_pay_dr.quantize(Decimal('0.01')),
        'total_pay_cr': total_pay_cr.quantize(Decimal('0.01')),
    }


# ============================================================
# COMBINED STATEMENT — HTML View
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def combined_statement(request, contact_id, is_print=None):
    """Combined statement (receivable + payable) for any contact type."""
    contact = get_object_or_404(Contact, pk=contact_id)

    if request.GET.get('reset'):
        return redirect('accounting:combined_statement', contact_id=contact_id)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '').strip()
    page_number = request.GET.get('page', 1)

    data = _build_combined_rows(contact, date_from or None, date_to or None,
                                txn_type or None, search or None)
    rows = data.pop('rows')

    paginator = Paginator(rows, 25)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    company = CompanyProfile.get_instance()

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
        **data,
    }

    # Print mode: either from URL kwarg (is_print='1') or query param (?print=1)
    print_requested = (is_print == '1') or (request.GET.get('print') == '1')
    if print_requested:
        return render(request, 'statements/combined_statement_print.html', context)

    if is_htmx(request):
        return render(request, 'statements/partials/combined_statement_table.html', context)
    return render(request, 'statements/combined_statement.html', context)


# ============================================================
# COMBINED STATEMENT — Excel
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def combined_statement_excel(request, contact_id):
    """Export combined statement to Excel (all rows, no pagination)."""
    if openpyxl is None:
        messages.error(request, "Openpyxl library is not installed.")
        return redirect_to_staff('combined_statement', contact_id=contact_id)

    contact = get_object_or_404(Contact, pk=contact_id)
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '').strip()

    data = _build_combined_rows(contact, date_from or None, date_to or None,
                                txn_type or None, search or None)
    rows = data.pop('rows')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Combined Statement"

    title_font = Font(bold=True, size=14, color="1F4E78")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    green_fill = PatternFill(start_color="D9EAD3", end_color="D9EAD3", fill_type="solid")
    yellow_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    net_fill = PatternFill(start_color="E8F0FE", end_color="E8F0FE", fill_type="solid")

    thin = Side(style='thin', color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center', wrap_text=True)
    right = Alignment(horizontal='right', vertical='center')
    money_fmt = '#,##0.00'

    company = CompanyProfile.get_instance()
    ws.merge_cells('A1:I1')
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = title_font
    ws['A1'].alignment = center

    ws.merge_cells('A2:I2')
    ws['A2'] = f"Combined Statement — {contact.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws['A2'].alignment = center

    ws.merge_cells('A3:I3')
    period = f"Period: {date_from or 'Beginning'} to {date_to or 'Today'}"
    ws['A3'] = period
    ws['A3'].alignment = center

    # 2-tier headers
    # Top-left merged block: A5:B6
    ws.merge_cells('A5:B6')
    ws['A5'] = 'Date / Particulars'
    ws['A5'].font = header_font
    ws['A5'].fill = header_fill
    ws['A5'].alignment = center
    ws['A5'].border = border
    ws['B5'].border = border
    ws['A6'].border = border
    ws['B6'].border = border

    # Receivable block
    ws.merge_cells('C5:E5')
    ws['C5'] = 'Receivable (They Owe Us)'
    ws['C5'].font = header_font
    ws['C5'].fill = green_fill
    ws['C5'].alignment = center
    for col in ['C5', 'D5', 'E5']:
        ws[col].fill = green_fill
        ws[col].border = border

    # Payable block
    ws.merge_cells('F5:H5')
    ws['F5'] = 'Payable (We Owe Them)'
    ws['F5'].font = header_font
    ws['F5'].fill = yellow_fill
    ws['F5'].alignment = center
    for col in ['F5', 'G5', 'H5']:
        ws[col].fill = yellow_fill
        ws[col].border = border

    # Net block
    ws.merge_cells('I5:I6')
    ws['I5'] = 'Net Position'
    ws['I5'].font = header_font
    ws['I5'].fill = net_fill
    ws['I5'].alignment = center
    ws['I5'].border = border
    ws['I6'].border = border
    ws['I6'].fill = net_fill

    # Row 6 sub-headers — only non-merged cells (C6:H6)
    sub_headers = {
        3: 'Dr', 4: 'Cr', 5: 'Balance',
        6: 'Dr', 7: 'Cr', 8: 'Balance',
    }
    for col_idx, header_text in sub_headers.items():
        c = ws.cell(row=6, column=col_idx, value=header_text)
        c.font = Font(bold=True, size=10)
        c.alignment = center
        c.border = border
        if 3 <= col_idx <= 5:
            c.fill = green_fill
        else:
            c.fill = yellow_fill

    row = 7
    # Opening row
    ws.cell(row=row, column=1, value='—')
    ws.cell(row=row, column=2, value='Opening Balance').font = Font(bold=True)
    ws.cell(row=row, column=3, value=float(data['opening_recv']) if data['opening_recv'] > 0 else '')
    ws.cell(row=row, column=4, value=float(abs(data['opening_recv'])) if data['opening_recv'] < 0 else '')
    ws.cell(row=row, column=5, value=float(data['opening_recv']))
    ws.cell(row=row, column=6, value=float(abs(data['opening_pay'])) if data['opening_pay'] < 0 else '')
    ws.cell(row=row, column=7, value=float(data['opening_pay']) if data['opening_pay'] > 0 else '')
    ws.cell(row=row, column=8, value=float(data['opening_pay']))
    ws.cell(row=row, column=9, value=float(data['opening_recv'] - data['opening_pay']))
    for col in range(1, 10):
        c = ws.cell(row=row, column=col)
        c.border = border
        c.fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
        if col in (3, 4, 5, 6, 7, 8, 9):
            c.number_format = money_fmt
            c.alignment = right
    row += 1

    # Data rows
    for r in rows:
        ws.cell(row=row, column=1, value=r['date'].strftime('%d-%m-%Y'))
        desc = r['description']
        if r['reference']:
            desc += f"  [{r['reference']}]"
        ws.cell(row=row, column=2, value=desc)
        ws.cell(row=row, column=3, value=float(r['recv_dr']) if r['recv_dr'] else '')
        ws.cell(row=row, column=4, value=float(r['recv_cr']) if r['recv_cr'] else '')
        ws.cell(row=row, column=5, value=float(r['running_recv']))
        ws.cell(row=row, column=6, value=float(r['pay_dr']) if r['pay_dr'] else '')
        ws.cell(row=row, column=7, value=float(r['pay_cr']) if r['pay_cr'] else '')
        ws.cell(row=row, column=8, value=float(r['running_pay']))
        ws.cell(row=row, column=9, value=float(r['net']))

        for col in range(1, 10):
            c = ws.cell(row=row, column=col)
            c.border = border
            if col in (3, 4, 5, 6, 7, 8, 9):
                c.number_format = money_fmt
                c.alignment = right
            elif col == 1:
                c.alignment = center
            else:
                c.alignment = left
        row += 1

    # Totals
    ws.cell(row=row, column=2, value='Period Totals').font = Font(bold=True)
    ws.cell(row=row, column=3, value=float(data['total_recv_dr'])).number_format = money_fmt
    ws.cell(row=row, column=4, value=float(data['total_recv_cr'])).number_format = money_fmt
    ws.cell(row=row, column=6, value=float(data['total_pay_dr'])).number_format = money_fmt
    ws.cell(row=row, column=7, value=float(data['total_pay_cr'])).number_format = money_fmt
    for col in range(1, 10):
        c = ws.cell(row=row, column=col)
        c.border = border
        c.font = Font(bold=True)
        if col in (3, 4, 6, 7):
            c.alignment = right
    row += 1

    # Closing
    ws.cell(row=row, column=2, value='Closing Balance').font = Font(bold=True, size=11)
    ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=5)
    ws.cell(row=row, column=3, value=float(data['closing_recv'])).number_format = money_fmt
    ws.cell(row=row, column=3).font = Font(bold=True)
    ws.cell(row=row, column=3).alignment = right
    ws.merge_cells(start_row=row, start_column=6, end_row=row, end_column=8)
    ws.cell(row=row, column=6, value=float(data['closing_pay'])).number_format = money_fmt
    ws.cell(row=row, column=6).font = Font(bold=True)
    ws.cell(row=row, column=6).alignment = right
    ws.cell(row=row, column=9, value=float(data['net_position'])).number_format = money_fmt
    ws.cell(row=row, column=9).font = Font(bold=True)
    for col in range(1, 10):
        c = ws.cell(row=row, column=col)
        c.border = border
        c.fill = net_fill

    widths = [12, 42, 13, 13, 14, 13, 13, 14, 14]
    for idx, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = w

    ws.freeze_panes = 'A7'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    safe_name = contact.name.replace(' ', '_').replace('/', '_')
    response['Content-Disposition'] = f'attachment; filename="combined_{safe_name}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
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