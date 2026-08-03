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

from ..models import Contact, LedgerLine, Invoice, RepairJob, CompanyProfile
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
# HELPER: Get statement lines with filters
# ============================================================
def get_statement_lines(contact, date_from=None, date_to=None, txn_type=None, search=None):
    """Returns filtered and annotated statement lines for a contact."""
    lines = LedgerLine.objects.filter(contact=contact) \
        .exclude(ledger_entry__entry_type='opening') \
        .select_related('ledger_entry') \
        .order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)
    if txn_type == 'invoice':
        lines = lines.filter(ledger_entry__entry_type='sales')
    elif txn_type == 'payment':
        lines = lines.filter(ledger_entry__entry_type='payment')
    elif txn_type == 'discount':
        lines = lines.filter(ledger_entry__entry_type='journal', ledger_entry__description__icontains='discount')
    elif txn_type == 'journal':
        lines = lines.filter(ledger_entry__entry_type='journal')

    return lines


# ============================================================
# CUSTOMER STATEMENT - EXCEL
# ============================================================

@handle_errors(default_redirect='accounting:contact_list')
def customer_statement_excel(request, contact_id):
    if openpyxl is None:
        messages.error(request, "Openpyxl library is not installed.")
        return redirect_to_staff('customer_statement', contact_id=contact_id)

    contact = get_object_or_404(Contact, pk=contact_id, contact_type__in=['customer', 'both'])
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '')

    lines = get_statement_lines(contact, date_from, date_to, txn_type, search)
    opening = contact.opening_balance
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
            if not (invoice_no and s in invoice_no.lower()) and not (desc and s in desc.lower()) and not (action_text and s in action_text.lower()):
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

    # Create workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Customer Statement"

    # Styles (unchanged)
    header_font = Font(bold=True, color="FFFFFF", size=12)
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    header_alignment = Alignment(horizontal="center", vertical="center")
    border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    money_format = '#,##0.00'

    company = CompanyProfile.get_instance()
    ws.merge_cells('A1:H1')  # changed to 8 columns
    ws['A1'] = company.name
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A2:H2')
    ws['A2'] = f"Customer Statement - {contact.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws.merge_cells('A3:H3')
    period = f"Period: {date_from if date_from else 'Start'} to {date_to if date_to else 'Today'}"
    ws['A3'] = period
    ws['A3'].alignment = Alignment(horizontal="center")

    # Headers with new Action column
    headers = ['Date', 'Transaction Type', 'Reference', 'Description', 'Action', 'Debit (₹)', 'Credit (₹)', 'Balance (₹)']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=5, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_alignment
        cell.border = border

    row = 6
    # Opening balance row (same but adjust columns)
    ws.cell(row=row, column=1, value="")
    ws.cell(row=row, column=2, value="")
    ws.cell(row=row, column=3, value="")
    ws.cell(row=row, column=4, value="Opening Balance")
    ws.cell(row=row, column=5, value="")  # Action empty
    ws.cell(row=row, column=6, value="")
    ws.cell(row=row, column=7, value="")
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
        # Action column (truncated)
        action = trans.get('action', '')
        if action and len(action) > 80:
            action = action[:80] + '...'
        ws.cell(row=row, column=5, value=action)
        # Debit/Credit/Balance shifted
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

    # Closing balance row (shift columns)
    ws.cell(row=row, column=4, value="Closing Balance")
    ws.cell(row=row, column=8, value=float(closing_balance))
    ws.cell(row=row, column=8).number_format = money_format
    for col in range(1, 9):
        ws.cell(row=row, column=col).border = border
        ws.cell(row=row, column=col).font = Font(bold=True)
    row += 1

    # Totals row (shift columns)
    ws.cell(row=row, column=4, value="Total")
    ws.cell(row=row, column=6, value=float(total_debit))
    ws.cell(row=row, column=6).number_format = money_format
    ws.cell(row=row, column=7, value=float(total_credit))
    ws.cell(row=row, column=7).number_format = money_format
    for col in range(1, 9):
        ws.cell(row=row, column=col).border = border
        ws.cell(row=row, column=col).font = Font(bold=True)

    # Column widths (updated)
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

    # Clean phone number
    phone_clean = phone.replace(' ', '').replace('-', '').replace('+', '')
    if not phone_clean:
        messages.error(request, "Invalid phone number.")
        return redirect_to_staff('customer_statement', contact_id=contact_id)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    # Get opening and closing balances
    opening = contact.opening_balance
    lines = get_statement_lines(contact, date_from, date_to)

    running_balance = opening
    for line in lines:
        if line.debit > 0:
            running_balance += line.debit
        else:
            running_balance -= line.credit
    closing = running_balance

    period = f"{date_from if date_from else 'Start'} to {date_to if date_to else 'Today'}"

    # Clean message without exposing all transactions in URL
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
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '')
    page_number = request.GET.get('page', 1)

    lines = get_statement_lines(contact, date_from, date_to, txn_type, search)
    opening = contact.opening_balance
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
        elif entry.entry_type == 'payment':
            desc = f"Payment - {entry.description}"
        elif entry.entry_type == 'journal':
            desc = f"Journal - {entry.description}"

        if search:
            s = search.lower()
            if not (invoice_no and s in invoice_no.lower()) and not (issue_text and s in issue_text.lower()) and not (action_text and s in action_text.lower()):
                continue

        if line.debit > 0:
            running_balance += line.debit
            debit_amt = line.debit
            credit_amt = Decimal('0')
        else:
            running_balance -= line.credit
            debit_amt = Decimal('0')
            credit_amt = line.credit

        # ============================================================
        # SKIP ZERO-AMOUNT ROWS
        # ============================================================
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

    # Pagination
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
        'dr_total': dr_total,
        'cr_total': cr_total,
        'closing': closing,
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
    company = CompanyProfile.get_instance()

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '')

    # Order chronologically for statement (oldest to newest)
    lines = get_statement_lines(contact, date_from, date_to, txn_type, search)

    opening = contact.opening_balance
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
        invoice_no = None

        if invoice:
            invoice_no = invoice.invoice_number
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
            match = False
            if invoice_no and s in invoice_no.lower():
                match = True
            elif issue_text and s in issue_text.lower():
                match = True
            elif action_text and s in action_text.lower():
                match = True
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
        'dr_total': dr_total,
        'cr_total': cr_total,
        'closing': closing,
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
    contact = get_object_or_404(Contact, pk=contact_id, contact_type__in=['vendor', 'both'])

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    page_number = request.GET.get('page', 1)

    # Order chronologically (oldest to newest) for proper statement
    lines = LedgerLine.objects.filter(contact=contact) \
        .exclude(ledger_entry__entry_type='opening') \
        .select_related('ledger_entry') \
        .order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)

    # Calculate opening balance correctly
    opening_lines = LedgerLine.objects.filter(contact=contact) \
        .exclude(ledger_entry__entry_type='opening')
    if date_from:
        opening_lines = opening_lines.filter(ledger_entry__date__lt=date_from)

    total_debit_opening = opening_lines.aggregate(Sum('debit'))['debit__sum'] or Decimal('0')
    total_credit_opening = opening_lines.aggregate(Sum('credit'))['credit__sum'] or Decimal('0')
    opening_balance = total_credit_opening - total_debit_opening

    running_balance = opening_balance
    statement_lines = []

    for line in lines:
        entry = line.ledger_entry
        trans_type = entry.get_entry_type_display()
        ref = ''
        if entry.entry_type == 'purchase':
            ref = f"Purchase #{entry.reference_id}"
        elif entry.entry_type == 'payment':
            ref = f"Payment #{entry.reference_id}"
        elif entry.entry_type == 'journal':
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

    # Pagination
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
        'closing_balance': closing_balance,
        'total_debit': total_debit,
        'total_credit': total_credit,
        'date_from': date_from,
        'date_to': date_to,
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
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    # Order chronologically (oldest to newest)
    lines = LedgerLine.objects.filter(contact=contact) \
        .exclude(ledger_entry__entry_type='opening') \
        .select_related('ledger_entry') \
        .order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)

    opening_lines = LedgerLine.objects.filter(contact=contact) \
        .exclude(ledger_entry__entry_type='opening')
    if date_from:
        opening_lines = opening_lines.filter(ledger_entry__date__lt=date_from)

    total_debit_opening = opening_lines.aggregate(Sum('debit'))['debit__sum'] or Decimal('0')
    total_credit_opening = opening_lines.aggregate(Sum('credit'))['credit__sum'] or Decimal('0')
    opening_balance = total_credit_opening - total_debit_opening

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="vendor_statement_{contact.name}_{datetime.now().strftime("%Y%m%d")}.csv"'
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow(['Date', 'Transaction Type', 'Reference', 'Description', 'Debit (₹)', 'Credit (₹)', 'Balance (₹)'])
    writer.writerow(['', 'Opening Balance', '', '', '', '', f'{opening_balance:.2f}'])

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
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    # Order chronologically (oldest to newest)
    lines = LedgerLine.objects.filter(contact=contact) \
        .exclude(ledger_entry__entry_type='opening') \
        .select_related('ledger_entry') \
        .order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)

    opening_lines = LedgerLine.objects.filter(contact=contact) \
        .exclude(ledger_entry__entry_type='opening')
    if date_from:
        opening_lines = opening_lines.filter(ledger_entry__date__lt=date_from)

    total_debit_opening = opening_lines.aggregate(Sum('debit'))['debit__sum'] or Decimal('0')
    total_credit_opening = opening_lines.aggregate(Sum('credit'))['credit__sum'] or Decimal('0')
    opening_balance = total_credit_opening - total_debit_opening

    running_balance = opening_balance
    statement_lines = []

    for line in lines:
        entry = line.ledger_entry
        trans_type = entry.get_entry_type_display()
        ref = ''
        if entry.entry_type == 'purchase':
            ref = f"Purchase #{entry.reference_id}"
        elif entry.entry_type == 'payment':
            ref = f"Payment #{entry.reference_id}"
        elif entry.entry_type == 'journal':
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

    context = {
        'contact': contact,
        'statement_lines': statement_lines,
        'opening_balance': opening_balance,
        'closing_balance': closing_balance,
        'total_debit': total_debit,
        'total_credit': total_credit,
        'date_from': date_from,
        'date_to': date_to,
        'company': CompanyProfile.get_instance(),
        'logo_exists': bool(CompanyProfile.logo and CompanyProfile.logo.name and CompanyProfile.logo.storage.exists(CompanyProfile.logo.name)),
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
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    # Order chronologically (oldest to newest)
    lines = LedgerLine.objects.filter(contact=contact) \
        .exclude(ledger_entry__entry_type='opening') \
        .select_related('ledger_entry') \
        .order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)

    opening_lines = LedgerLine.objects.filter(contact=contact) \
        .exclude(ledger_entry__entry_type='opening')
    if date_from:
        opening_lines = opening_lines.filter(ledger_entry__date__lt=date_from)

    total_debit_opening = opening_lines.aggregate(Sum('debit'))['debit__sum'] or Decimal('0')
    total_credit_opening = opening_lines.aggregate(Sum('credit'))['credit__sum'] or Decimal('0')
    opening_balance = total_credit_opening - total_debit_opening

    # Create workbook
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
    ws.cell(row=row, column=1, value="")
    ws.cell(row=row, column=2, value="")
    ws.cell(row=row, column=3, value="")
    ws.cell(row=row, column=4, value="Opening Balance")
    ws.cell(row=row, column=5, value="")
    ws.cell(row=row, column=6, value="")
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
        if entry.entry_type == 'purchase':
            ref = f"Purchase #{entry.reference_id}"
        elif entry.entry_type == 'payment':
            ref = f"Payment #{entry.reference_id}"
        elif entry.entry_type == 'journal':
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

