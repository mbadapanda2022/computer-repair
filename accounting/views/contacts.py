# accounting/views/contacts.py
import csv
import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.db.models import (
    Q, Sum, F, Case, When, Value, IntegerField, Prefetch,
)
from django.db.models.functions import Coalesce
from django.db import transaction
from django.contrib import messages
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment, numbers
from openpyxl.utils import get_column_letter

from ..models import (
    Contact, LedgerLine, Invoice, RepairJob, Payment,
)
from ..forms import ContactForm
from .utils import is_htmx, htmx_response, redirect_to_staff
from ..decorators import handle_errors

logger = logging.getLogger(__name__)


# ============================================================
# HELPER: Annotate contacts with receivable / payable / net
# ============================================================
def annotate_contact_balances(contacts):
    """
    Annotate each Contact with:
      - receivable_balance: net amount the party owes us (Dr - Cr on receivable lines)
      - payable_balance:    net amount we owe the party (Cr - Dr on payable lines)
      - current_balance:    net position (receivable - payable) for customer/both,
                            or just payable for vendor-only.
    Uses `subledger_type` so customer and vendor sides stay separate.
    """
    if not contacts:
        return

    contact_ids = [c.id for c in contacts]

    # --- Receivable side ---
    recv = (
        LedgerLine.objects
        .filter(contact__in=contact_ids, subledger_type='receivable')
        .values('contact')
        .annotate(dr=Sum('debit'), cr=Sum('credit'))
    )
    recv_map = {
        item['contact']: (item['dr'] or Decimal('0')) - (item['cr'] or Decimal('0'))
        for item in recv
    }

    # --- Payable side ---
    pay = (
        LedgerLine.objects
        .filter(contact__in=contact_ids, subledger_type='payable')
        .values('contact')
        .annotate(dr=Sum('debit'), cr=Sum('credit'))
    )
    pay_map = {
        item['contact']: (item['cr'] or Decimal('0')) - (item['dr'] or Decimal('0'))
        for item in pay
    }

    for contact in contacts:
        receivable = recv_map.get(contact.id, Decimal('0')).quantize(Decimal('0.01'))
        payable = pay_map.get(contact.id, Decimal('0')).quantize(Decimal('0.01'))
        contact.receivable_balance = receivable
        contact.payable_balance = payable

        if contact.contact_type in ('customer', 'both'):
            contact.current_balance = (receivable - payable).quantize(Decimal('0.01'))
        else:
            contact.current_balance = payable


# ============================================================
# CORE: Paginated contacts context
# ============================================================
def get_paginated_contacts_context(request, queryset=None):
    """Filter, paginate, and annotate contacts for list views."""
    search = request.GET.get('search', '').strip()
    contact_type = request.GET.get('contact_type', '')
    sort = request.GET.get('sort', 'name')  # name | -created | balance
    page_number = request.GET.get('page', 1)

    if queryset is None:
        queryset = Contact.objects.all()

    # --- Sorting ---
    sort_map = {
        'name': ('name',),
        'name_desc': ('-name',),
        'recent': ('-created_at',),
        'oldest': ('created_at',),
    }
    queryset = queryset.order_by(*sort_map.get(sort, ('name',)))

    # --- Filters ---
    if search:
        queryset = queryset.filter(
            Q(name__icontains=search) |
            Q(phone__icontains=search) |
            Q(email__icontains=search) |
            Q(company_name__icontains=search) |
            Q(gstin__icontains=search)
        )
    if contact_type:
        queryset = queryset.filter(contact_type=contact_type)

    paginator = Paginator(queryset, 25)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    annotate_contact_balances(page_obj.object_list)

    return {
        'page_obj': page_obj,
        'contacts': page_obj.object_list,
        'search': search,
        'contact_type_filter': contact_type,
        'sort': sort,
    }


# ============================================================
# FIELD VALIDATION (HTMX)
# ============================================================
@require_http_methods(["GET"])
def validate_contact_field(request):
    """Real-time validation for Contact fields."""
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    value = request.GET.get(field_name, '').strip()
    contact_id = request.GET.get('contact_id')

    try:
        instance = Contact.objects.get(pk=contact_id) if contact_id else None
    except (Contact.DoesNotExist, ValueError):
        instance = None

    form = ContactForm(data={field_name: value}, instance=instance)
    form.full_clean()
    errors = form.errors.get(field_name, [])

    html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
    for err in errors:
        html += f'<div><i class="bi bi-exclamation-circle me-1"></i>{err}</div>'
    html += '</div>'
    return HttpResponse(html)


# ============================================================
# CONTACT LIST
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def contact_list(request):
    context = get_paginated_contacts_context(request)
    if is_htmx(request):
        return render(request, 'contacts/partials/contact_table.html', context)
    return render(request, 'contacts/contact_list.html', context)


# ============================================================
# EXCEL EXPORT (Updated for receivable / payable)
# ============================================================
@require_http_methods(["GET"])
def export_contacts_excel(request):
    """Export all contacts to a professional Excel file."""
    contacts = list(Contact.objects.all().order_by('name'))
    annotate_contact_balances(contacts)

    wb = Workbook()
    ws = wb.active
    ws.title = "Contacts"

    # Styles
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    title_font = Font(bold=True, size=14, color="1F4E78")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin'),
    )
    center_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left_align = Alignment(horizontal='left', vertical='center', wrap_text=True)
    right_align = Alignment(horizontal='right', vertical='center')
    money_format = numbers.FORMAT_NUMBER_COMMA_SEPARATED1

    TOTAL_COLS = 13
    last_col = get_column_letter(TOTAL_COLS)

    # Title row
    ws.merge_cells(f'A1:{last_col}1')
    title_cell = ws.cell(row=1, column=1, value="📇 Contact List – A1 Computer Solutions")
    title_cell.font = title_font
    title_cell.alignment = center_align
    ws.row_dimensions[1].height = 30

    # Headers
    headers = [
        'ID', 'Type', 'Name', 'Company', 'Phone', 'Email',
        'GSTIN', 'State', 'Opening',
        'Receivable', 'Payable', 'Net Position', 'Address',
    ]
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = center_align
    ws.row_dimensions[2].height = 25

    # Data rows
    for idx, contact in enumerate(contacts, start=3):
        row_fill = PatternFill(
            start_color="F2F6FC" if idx % 2 == 0 else "FFFFFF",
            end_color="F2F6FC" if idx % 2 == 0 else "FFFFFF",
            fill_type="solid",
        )
        row_data = [
            contact.id,
            contact.get_contact_type_display(),
            contact.name,
            contact.company_name or '',
            contact.phone or '',
            contact.email or '',
            contact.gstin or '',
            contact.state or '',
            float(contact.opening_balance or 0),
            float(contact.receivable_balance or 0),
            float(contact.payable_balance or 0),
            float(contact.current_balance or 0),
            contact.address or '',
        ]

        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=idx, column=col_idx, value=value)
            cell.border = thin_border
            cell.fill = row_fill

            if col_idx in (1, 2, 5):
                cell.alignment = center_align
            elif col_idx in (9, 10, 11, 12):
                cell.alignment = right_align
                cell.number_format = money_format
            else:
                cell.alignment = left_align

    # Auto column widths
    for col in ws.columns:
        max_length = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = max(min(max_length + 3, 45), 12)

    ws.freeze_panes = 'A3'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename="contacts.xlsx"'
    wb.save(response)
    return response


# ============================================================
# CONTACT CREATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:contact_list', htmx_template='contacts/contact_form.html')
def contact_create(request):
    if request.method == 'POST':
        form = ContactForm(request.POST)
        if form.is_valid():
            contact = form.save()
            logger.info(f"Contact '{contact.name}' created by {request.user.username}")

            if is_htmx(request):
                context = get_paginated_contacts_context(request)
                response = render(request, 'contacts/partials/contact_table.html', context)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': f'Contact "{contact.name}" created.'},
                    'closeModal': '',
                })
                return response

            messages.success(request, f'Contact "{contact.name}" created successfully.')
            return redirect_to_staff('contact_list')
        else:
            if is_htmx(request):
                response = render(request, 'contacts/contact_form.html', {'form': form})
                response['HX-Retarget'] = '#mainModalContent'
                return response

    form = ContactForm()
    return render(request, 'contacts/contact_form.html', {'form': form})


# ============================================================
# CONTACT UPDATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:contact_list', htmx_template='contacts/contact_form.html')
def contact_update(request, pk):
    contact = get_object_or_404(Contact, pk=pk)

    if request.method == 'POST':
        form = ContactForm(request.POST, instance=contact)
        if form.is_valid():
            contact = form.save()
            logger.info(f"Contact '{contact.name}' updated by {request.user.username}")

            if is_htmx(request):
                context = get_paginated_contacts_context(request)
                response = render(request, 'contacts/partials/contact_table.html', context)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': f'Contact "{contact.name}" updated.'},
                    'closeModal': '',
                })
                return response

            messages.success(request, f'Contact "{contact.name}" updated successfully.')
            return redirect_to_staff('contact_list')
        else:
            if is_htmx(request):
                response = render(request, 'contacts/contact_form.html', {
                    'form': form, 'contact': contact,
                })
                response['HX-Retarget'] = '#mainModalContent'
                return response

    form = ContactForm(instance=contact)
    return render(request, 'contacts/contact_form.html', {'form': form, 'contact': contact})


# ============================================================
# CONTACT DELETE (soft, HTMX)
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:contact_list')
def contact_delete(request, pk):
    contact = get_object_or_404(Contact, pk=pk)

    # Block deletion if any transaction exists
    if (
        contact.sales_invoices.exists()
        or contact.purchases.exists()
        or contact.ledger_lines.exists()
        or contact.repair_jobs.exists()
        or contact.payments.exists()
    ):
        response = HttpResponse("Cannot delete contact with existing transactions.", status=400)
        response['HX-Trigger'] = json.dumps({
            'showToast': {
                'level': 'danger',
                'message': f'"{contact.name}" has transactions and cannot be deleted.',
            }
        })
        return response

    name = contact.name
    contact.delete()  # soft delete

    context = get_paginated_contacts_context(request)
    response = render(request, 'contacts/partials/contact_table.html', context)
    response['HX-Trigger'] = json.dumps({
        'showToast': {'level': 'success', 'message': f'Contact "{name}" deleted.'}
    })
    return response


# ============================================================
# CONTACT DETAIL MODAL
# ============================================================
def contact_detail_modal(request, pk):
    """Return contact detail as HTMX modal, with proper stats per type."""
    contact = get_object_or_404(Contact, pk=pk)

    # Invoice stats (customer side)
    invoices = Invoice.objects.filter(customer=contact)
    total_invoiced = invoices.aggregate(t=Sum('grand_total'))['t'] or Decimal('0')
    total_due = invoices.aggregate(t=Sum('balance_due'))['t'] or Decimal('0')

    # Purchases (vendor side)
    purchases = contact.purchases.all()
    total_purchased = purchases.aggregate(t=Sum('grand_total'))['t'] or Decimal('0')

    # Repairs
    repairs = RepairJob.objects.filter(customer=contact)

    # Payments — received (from customer) and paid (to vendor)
    payments_received = Payment.objects.filter(contact=contact, direction='received')
    payments_paid = Payment.objects.filter(contact=contact, direction='paid')
    total_received = payments_received.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    total_paid = payments_paid.aggregate(t=Sum('amount'))['t'] or Decimal('0')

    context = {
        'contact': contact,
        # Invoice
        'invoice_count': invoices.count(),
        'total_invoiced': total_invoiced,
        'total_due': total_due,
        # Purchases
        'purchase_count': purchases.count(),
        'total_purchased': total_purchased,
        # Repairs
        'repair_count': repairs.count(),
        # Payments
        'total_received': total_received,
        'total_paid': total_paid,
    }
    return render(request, 'contacts/partials/contact_detail_modal.html', context)


# ============================================================
# QUICK JOURNAL / STATEMENT REDIRECTS
# ============================================================
def quick_journal(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    return redirect_to_staff('journal_create_for_contact', contact_id=contact.pk)


def quick_statement(request, pk):
    """Smart redirect: customer → customer_statement, else vendor."""
    contact = get_object_or_404(Contact, pk=pk)
    # TODO: Re-enable combined_statement for 'both' when module is ready
    if contact.contact_type in ('customer', 'both'):
        return redirect_to_staff('customer_statement', contact_id=contact.pk)
    return redirect_to_staff('vendor_statement', contact_id=contact.pk)


# ============================================================
# BULK DELETE
# ============================================================
@csrf_protect
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:contact_list')
def bulk_delete_contacts(request):
    ids = request.POST.getlist('ids')
    if not ids:
        return JsonResponse({'status': 'error', 'message': 'No contacts selected.'}, status=400)

    contacts = Contact.objects.filter(pk__in=ids)

    # Block whole operation if ANY contact has transactions
    blocking = []
    for contact in contacts:
        if (
            contact.sales_invoices.exists()
            or contact.purchases.exists()
            or contact.ledger_lines.exists()
            or contact.repair_jobs.exists()
            or contact.payments.exists()
        ):
            blocking.append(contact.name)

    if blocking:
        return JsonResponse({
            'status': 'error',
            'message': f'Cannot delete: {", ".join(blocking)} (has transactions).',
        }, status=400)

    count = contacts.count()
    contacts.delete()  # soft delete all
    return JsonResponse({'status': 'ok', 'deleted': count})


# ============================================================
# CONTACT SEARCH (JSON — for autocomplete / API use)
# ============================================================
@require_http_methods(["GET"])
def contact_search(request):
    """JSON endpoint for contact autocomplete."""
    q = request.GET.get('q', '').strip()
    results = []

    if len(q) >= 1:
        contacts = Contact.objects.filter(
            Q(name__icontains=q) |
            Q(phone__icontains=q) |
            Q(email__icontains=q) |
            Q(company_name__icontains=q)
        )[:20]

        results = [{
            'id': c.id,
            'name': c.name,
            'phone': c.phone or '',
            'email': c.email or '',
            'type': c.contact_type,
            'type_display': c.get_contact_type_display(),
            'label': f"{c.name} ({c.phone or 'No phone'})",
        } for c in contacts]

    return JsonResponse({'results': results})


