# accounting/views/contacts.py  (only for staff)
import csv
import json
import logging
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.db.models import Q, Sum, F, Case, When, Value, IntegerField
from django.db.models.functions import Lower
from django.db import transaction
from django.contrib import messages
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.forms import modelform_factory
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger 

# openpyxl imports
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment, numbers
from openpyxl.utils import get_column_letter

from ..models import *
from ..forms import ContactForm
from .utils import is_htmx, htmx_response, redirect_to_staff
from ..decorators import handle_errors

logger = logging.getLogger(__name__)


# ============================================================
# HELPER FUNCTION
# ============================================================
def annotate_contact_balances(contacts):
    """
    Annotates a queryset or list of Contact objects with current_balance.
    """
    if not contacts:
        return
    # Ensure we have a list of IDs for the filter
    contact_ids = [c.id for c in contacts]
    ledger_balances = (
        LedgerLine.objects
        .filter(contact__in=contact_ids)
        .values('contact')
        .annotate(net_balance=Sum(F('debit') - F('credit')))
    )
    balance_dict = {item['contact']: item['net_balance'] for item in ledger_balances}
    for contact in contacts:
        net = balance_dict.get(contact.id, Decimal('0'))
        # For customers, positive balance means they owe us (Dr - Cr)
        # For vendors, we flip the sign
        contact.current_balance = net if contact.contact_type in ('customer', 'both') else -net


# ============================================================
# CORE: GET PAGINATED CONTACTS CONTEXT (DRY Principle)
# ============================================================
def get_paginated_contacts_context(request, queryset=None):
    """
    Shared logic to filter, paginate, and annotate contacts.
    Returns context dict ready for rendering.
    """
    search = request.GET.get('search', '').strip()
    contact_type = request.GET.get('contact_type', '')
    page_number = request.GET.get('page', 1)

    if queryset is None:
        queryset = Contact.objects.all().order_by('-created_at')

    # Apply filters
    if search:
        queryset = queryset.filter(
            Q(name__icontains=search) |
            Q(phone__icontains=search) |
            Q(email__icontains=search) |
            Q(company_name__icontains=search)
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

    context = {
        'page_obj': page_obj,
        'contacts': page_obj.object_list,  # For backward compatibility with templates
        'search': search,
        'contact_type_filter': contact_type,
    }
    return context


# ============================================================
# FIELD VALIDATION (HTMX) – STAFF ONLY
# ============================================================
def validate_contact_field(request):
    """Real-time validation for Contact fields."""
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")
    value = request.GET.get(field_name, '').strip()
    contact_id = request.GET.get('contact_id')
    try:
        instance = Contact.objects.get(pk=contact_id) if contact_id else None
    except Contact.DoesNotExist:
        instance = None
    form = ContactForm(data={field_name: value}, instance=instance)
    form.full_clean()
    errors = form.errors.get(field_name, [])
    error_html = f'<div id="field-{field_name}" class="invalid-feedback">'
    for err in errors:
        error_html += f'<div>{err}</div>'
    error_html += '</div>'
    return HttpResponse(error_html)


# ============================================================
# CONTACT LIST (WITH FILTERS, PAGINATION & HTMX)
# ============================================================
@handle_errors(default_redirect='accounting:contact_list')
def contact_list(request):
    context = get_paginated_contacts_context(request)
    if is_htmx(request):
        return render(request, 'contacts/partials/contact_table.html', context)
    return render(request, 'contacts/contact_list.html', context)


# ============================================================
# EXCEL EXPORT (PROFESSIONAL STYLED)
# ============================================================
@require_http_methods(["GET"])
def export_contacts_excel(request):
    """
    Export all contacts to a professional styled Excel (.xlsx) file.
    Features: Colors, Borders, Currency Format, Auto-width, Freeze Panes.
    """
    contacts = Contact.objects.all().order_by('name')
    annotate_contact_balances(contacts)

    wb = Workbook()
    ws = wb.active
    ws.title = "Contacts List"

    # ========== STYLES ==========
    # Header Style
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")  # Dark Blue

    # Border Style
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    # Alignment
    center_align = Alignment(horizontal='center', vertical='center')
    left_align = Alignment(horizontal='left', vertical='center')
    right_align = Alignment(horizontal='right', vertical='center')

    # Money Format (Indian Currency with commas)
    money_format = numbers.FORMAT_NUMBER_COMMA_SEPARATED1  # e.g., 1,234.00

    # ========== TITLE ROW ==========
    ws.merge_cells('A1:K1')
    title_cell = ws.cell(row=1, column=1, value="📊 Contact List – A1 Computer Solutions")
    title_cell.font = Font(bold=True, size=14, color="1F4E78")
    title_cell.alignment = center_align
    ws.row_dimensions[1].height = 30

    # ========== HEADER ROW (Row 2) ==========
    headers = [
        'ID', 'Type', 'Name', 'Company', 'Phone', 'Email',
        'Address', 'GSTIN', 'State', 'Opening Balance', 'Current Balance'
    ]

    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = center_align
        ws.row_dimensions[2].height = 25

    # ========== DATA ROWS ==========
    for idx, contact in enumerate(contacts, start=3):
        row_num = idx

        # Alternating Row Colors (Professional Banding)
        if idx % 2 == 0:
            row_fill = PatternFill(start_color="F2F6FC", end_color="F2F6FC", fill_type="solid")  # Light Blueish Gray
        else:
            row_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")  # White

        # Define row data
        row_data = [
            contact.id,
            contact.get_contact_type_display(),
            contact.name,
            contact.company_name or '',
            contact.phone or '',
            contact.email or '',
            contact.address or '',
            contact.gstin or '',
            contact.state or '',
            float(contact.opening_balance),
            float(contact.current_balance),
        ]

        # Write cells with styling
        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=value)
            cell.border = thin_border
            cell.fill = row_fill

            # Apply specific alignments
            if col_idx in (1, 2, 5):  # ID, Type, Phone -> Center
                cell.alignment = center_align
            elif col_idx in (10, 11):  # Balance columns -> Right aligned with currency format
                cell.alignment = right_align
                cell.number_format = money_format
            else:
                cell.alignment = left_align

    # ========== AUTO-ADJUST COLUMN WIDTHS ==========
    for col in ws.columns:
        max_length = 0
        column_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except:
                pass
        # Set width with a cap (max 50) and minimum 12
        adjusted_width = max(min(max_length + 3, 50), 12)
        ws.column_dimensions[column_letter].width = adjusted_width

    # ========== FREEZE HEADER ROW ==========
    ws.freeze_panes = 'A3'  # Freezes row 2 (header) and column A

    # ========== RESPONSE ==========
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename="contacts.xlsx"'
    wb.save(response)
    return response


# ============================================================
# CONTACT CREATE (WITH HTMX MODAL)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:contact_list', htmx_template='contacts/contact_form.html')
def contact_create(request):
    if request.method == 'POST':
        form = ContactForm(request.POST)
        if form.is_valid():
            contact = form.save()
            if is_htmx(request):
                context = get_paginated_contacts_context(request)
                response = render(request, 'contacts/partials/contact_table.html', context)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': 'Contact created successfully.'},
                    'closeModal': ''
                })
                return response
            messages.success(request, "Contact created successfully.")
            return redirect_to_staff('contact_list')
        else:
            # Invalid form: Force HX-Retarget to keep form inside modal
            response = render(request, 'contacts/contact_form.html', {'form': form})
            response['HX-Retarget'] = '#mainModalContent'
            return response
    else:
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
            form.save()
            if is_htmx(request):
                context = get_paginated_contacts_context(request)
                response = render(request, 'contacts/partials/contact_table.html', context)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': 'Contact updated successfully.'},
                    'closeModal': ''
                })
                return response
            messages.success(request, "Contact updated successfully.")
            return redirect_to_staff('contact_list')
        else:
            # Invalid form: Force HX-Retarget to keep form inside modal
            response = render(request, 'contacts/contact_form.html', {'form': form, 'contact': contact})
            response['HX-Retarget'] = '#mainModalContent'
            return response
    else:
        form = ContactForm(instance=contact)
    return render(request, 'contacts/contact_form.html', {'form': form, 'contact': contact})



# ============================================================
# CONTACT DELETE (HTMX)
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:contact_list')
def contact_delete(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    # Check for existing transactions
    if (contact.sales_invoices.exists() or contact.purchases.exists() or
        contact.ledger_lines.exists() or contact.repair_jobs.exists() or
        contact.payments.exists()):
        response = HttpResponse("Cannot delete contact with existing transactions.", status=400)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'danger', 'message': 'Contact has transactions, cannot delete.'}
        })
        return response
    
    contact.delete()
    context = get_paginated_contacts_context(request)
    response = render(request, 'contacts/partials/contact_table.html', context)
    response['HX-Trigger'] = json.dumps({
        'showToast': {'level': 'success', 'message': 'Contact deleted successfully.'}
    })
    return response



# ============================================================
# CONTACT DETAIL MODAL
# ============================================================
def contact_detail_modal(request, pk):
    """Return contact detail as HTMX modal."""
    contact = get_object_or_404(Contact, pk=pk)
    
    # Get stats for this contact
    invoices = Invoice.objects.filter(customer=contact)
    repairs = RepairJob.objects.filter(customer=contact)
    payments = Payment.objects.filter(contact=contact, direction='received')
    
    context = {
        'contact': contact,
        'invoice_count': invoices.count(),
        'repair_count': repairs.count(),
        'total_payments': payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0'),
        'total_due': invoices.aggregate(Sum('balance_due'))['balance_due__sum'] or Decimal('0'),
    }
    return render(request, 'contacts/partials/contact_detail_modal.html', context)


# ============================================================
# QUICK JOURNAL / STATEMENT REDIRECTS
# ============================================================
def quick_journal(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    return redirect_to_staff('journal_create_for_contact', contact_id=contact.pk)


def quick_statement(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    if contact.contact_type in ('customer', 'both'):
        return redirect_to_staff('customer_statement', contact_id=contact.pk)
    return redirect_to_staff('vendor_statement', contact_id=contact.pk)


# ============================================================
# BULK DELETE (CSRF PROTECTED)
# ============================================================
@csrf_protect
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:contact_list')
def bulk_delete_contacts(request):
    ids = request.POST.getlist('ids')
    if not ids:
        return JsonResponse({'status': 'error', 'message': 'No contacts selected.'}, status=400)
    contacts = Contact.objects.filter(pk__in=ids)
    
    # Check each contact for existing transactions
    for contact in contacts:
        if (contact.sales_invoices.exists() or contact.purchases.exists() or
                contact.ledger_lines.exists() or contact.repair_jobs.exists() or
                contact.payments.exists()):
            return JsonResponse({
                'status': 'error',
                'message': f'Contact "{contact.name}" has existing transactions and cannot be deleted.'
            }, status=400)
    
    count = contacts.delete()[0]
    return JsonResponse({'status': 'ok', 'deleted': count})


# ============================================================
# CONTACT SEARCH (Autocomplete)
# ============================================================

@require_http_methods(["GET"])
def contact_search(request):
    """HTMX endpoint for contact autocomplete in payment form."""
    q = request.GET.get('q', '').strip()
    contacts = Contact.objects.none()
    
    
    if q:
        # Filter using multiple fields
        contacts = Contact.objects.filter(
            Q(name__icontains=q) |
            Q(phone__icontains=q) |
            Q(email__icontains=q) |
            Q(company_name__icontains=q)
        )
        
        # Assign relevance score
        relevance = Case(
            # Exact matches (highest priority)
            When(name__iexact=q, then=Value(1)),
            When(phone__iexact=q, then=Value(2)),
            When(email__iexact=q, then=Value(3)),
            When(company_name__iexact=q, then=Value(4)),
            # Starts with (medium priority)
            When(name__istartswith=q, then=Value(5)),
            When(phone__istartswith=q, then=Value(6)),
            When(email__istartswith=q, then=Value(7)),
            When(company_name__istartswith=q, then=Value(8)),
            # Contains (lowest priority)
            default=Value(10),
            output_field=IntegerField()
        )
        contacts = contacts.annotate(relevance=relevance).order_by('relevance', 'name')
        contacts = contacts[:20]  # Limit to 20 suggestions
    
    context = {'contacts': contacts}
    return render(request, 'contacts/partials/contact_suggestions.html', context)




