# accounting/views/sales.py
import json
import logging
from decimal import Decimal
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.contrib import messages
from django.db.models import Q, Sum, F
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.utils import timezone
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from ..models import *
from ..forms import InvoiceForm, InvoiceItemForm
from accounting.utils.notification_helpers import send_notification_to_customer, send_notification_sse
from .utils import is_htmx, htmx_response, redirect_to_staff, toast_only_response
from ..decorators import handle_errors

logger = logging.getLogger(__name__)


# ============================================================
# HELPER 1: SYNC SESSION ITEMS (FOR EDIT MODE)
# ============================================================
def sync_session_items(request, invoice=None):
    """
    Ensures session contains items from the invoice (for edit mode).
    If invoice is provided and session is empty, loads invoice items.
    """
    session_key = 'temp_invoice_items'
    if invoice:
        if session_key not in request.session or not request.session[session_key]:
            items = []
            for item in invoice.items.select_related('product').all():
                items.append({
                    'product_id': item.product.id,
                    'product_name': item.product.name,
                    'quantity': str(item.quantity),
                    'unit_price': str(item.unit_price),
                    'tax_rate': str(item.tax_rate),
                    'line_total': str(item.line_total),
                    'description': item.description or '',
                    'db_item_id': item.id,
                })
            request.session[session_key] = items
    return request.session.get(session_key, [])


# ============================================================
# HELPER 2: GET PAGINATED INVOICES CONTEXT
# ============================================================
def get_paginated_invoices_context(request, queryset=None):
    if queryset is None:
        queryset = Invoice.objects.select_related('customer').all().order_by('-date')

    search = request.GET.get('search', '').strip()
    customer_id = request.GET.get('customer', '')
    status = request.GET.get('status', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    reset = request.GET.get('reset', '')
    page_number = request.GET.get('page', 1)

    if reset:
        search = customer_id = status = date_from = date_to = ''

    if search:
        queryset = queryset.filter(
            Q(invoice_number__icontains=search) |
            Q(customer__name__icontains=search)
        )
    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)
    if status == 'overdue':
        queryset = queryset.filter(
            payment_status__in=['unpaid', 'partial'],
            due_date__lt=timezone.now().date(),
        )
    elif status:
        queryset = queryset.filter(payment_status=status)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    paginator = Paginator(queryset, 20)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # ============================================================
    # FIX: Batch-prefetch linked RepairJobs for the current page.
    # Without this, `{% if invoice.linked_repair %}` in the template
    # triggers 1 DB query per invoice row (N+1).
    # ============================================================
    page_invoices = list(page_obj.object_list)
    invoice_ids = [inv.pk for inv in page_invoices if inv.pk]
    if invoice_ids:
        repair_map = {
            rj.invoice_id: rj
            for rj in RepairJob.objects.filter(
                invoice_id__in=invoice_ids
            ).only('id', 'invoice_id', 'job_number', 'device_model', 'status')
        }
        for inv in page_invoices:
            inv.linked_repair = repair_map.get(inv.pk)

    customers = Contact.objects.filter(contact_type__in=['customer', 'both']).order_by('name')

    total_sales = queryset.aggregate(total=Sum('grand_total'))['total'] or Decimal('0')
    total_received = queryset.filter(payment_status='paid').aggregate(total=Sum('grand_total'))['total'] or Decimal('0')
    total_unpaid = total_sales - total_received

    context = {
        'invoices': page_invoices,      
        'page_obj': page_obj,
        'customers': customers,
        'search': search,
        'customer_id': customer_id,
        'status': status,
        'date_from': date_from,
        'date_to': date_to,
        'total_sales': total_sales,
        'total_received': total_received,
        'total_unpaid': total_unpaid,
    }
    return context


# ============================================================
# 1. INVOICE LIST
# ============================================================
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_list(request):
    context = get_paginated_invoices_context(request)
    if is_htmx(request):
        return render(request, 'sales/partials/invoice_table.html', context)
    return render(request, 'sales/invoice_list.html', context)


# ============================================================
# 2. PRODUCT SEARCH
# ============================================================
@login_required
def product_search(request):
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return render(request, 'sales/partials/product_suggestions.html', {'products': []})

    products = Product.objects.filter(
        Q(name__icontains=q) | Q(hsn_code__icontains=q),
        is_active=True,
        is_service=False
    )[:20]

    def relevance_score(p):
        name = p.name.lower()
        hsn = (p.hsn_code or '').lower()
        q_lower = q.lower()
        if name == q_lower or hsn == q_lower:
            return 0
        if name.startswith(q_lower) or hsn.startswith(q_lower):
            return 1
        if q_lower in name or q_lower in hsn:
            return 2
        return 3

    products = sorted(products, key=relevance_score)[:10]
    return render(request, 'sales/partials/product_suggestions.html', {'products': products})


# ============================================================
# 3. INVOICE CREATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:invoice_list', htmx_template='sales/partials/invoice_form_modal.html')
def invoice_create(request):
    # ── Fresh open (GET): always reset session items ──
    # This prevents leaking old items from a previously-cancelled form.
    # POST failures keep the session (form re-renders with items intact).
    if request.method == 'GET' and request.GET.get('keep') != '1':
        request.session['temp_invoice_items'] = []
    elif 'temp_invoice_items' not in request.session:
        request.session['temp_invoice_items'] = []

    template_name = 'sales/partials/invoice_form_modal.html' if is_htmx(request) else 'sales/invoice_form.html'

    if request.method == 'POST':
        form = InvoiceForm(request.POST)
        if form.is_valid():
            invoice = form.save(commit=False)
            invoice.save()  # Save to get an ID

            temp_items = request.session.get('temp_invoice_items', [])
            for item_data in temp_items:
                InvoiceItem.objects.create(
                    invoice=invoice,
                    product_id=item_data['product_id'],
                    quantity=Decimal(item_data['quantity']),
                    unit_price=Decimal(item_data['unit_price']),
                    tax_rate=Decimal(item_data['tax_rate']),
                    description=item_data.get('description', '')
                )
            request.session['temp_invoice_items'] = []

            # Calculate totals and save again (this will also sync ledger)
            invoice.calculate_totals()
            invoice.save()  # This calls sync_invoice_ledger automatically

            logger.info(f"Invoice {invoice.invoice_number} created by {request.user.username}")

            # ---- Notify customer ----
            try:
                if invoice.customer and invoice.customer.user:
                    send_notification_to_customer(
                        invoice.customer,
                        title=f"New Invoice: {invoice.invoice_number}",
                        message=(
                            f"An invoice of ₹{invoice.grand_total} has been generated for you. "
                            f"Due: ₹{invoice.balance_due}."
                        ),
                        link=reverse('customer:customer_invoice_detail', args=[invoice.pk]),
                        notif_type='info',
                        category='sales',
                        send_email=False,
                    )
                for staff in User.objects.filter(is_staff=True):
                    send_notification_sse(staff)
            except Exception as notif_err:
                logger.error(f"Invoice notification failed: {notif_err}", exc_info=True)

            if is_htmx(request):
                messages.success(request, f"Invoice {invoice.invoice_number} created successfully.")
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:invoice_detail', args=[invoice.pk])
                return response

            messages.success(request, "Invoice created.")
            return redirect_to_staff('invoice_detail', pk=invoice.pk)

        else:
            temp_items = request.session.get('temp_invoice_items', [])
            context = {
                'form': form,
                'items': temp_items,
                'products': Product.objects.filter(is_active=True).order_by('name'),
                'customers': Contact.objects.filter(contact_type__in=['customer', 'both']).order_by('name'),
                'is_htmx': is_htmx(request),
            }
            if is_htmx(request):
                return render(request, 'sales/partials/invoice_form_modal.html', context)
            return render(request, 'sales/invoice_form.html', context)

    else:
        form = InvoiceForm()
        temp_items = request.session.get('temp_invoice_items', [])
        context = {
            'form': form,
            'items': temp_items,
            'products': Product.objects.filter(is_active=True).order_by('name'),
            'customers': Contact.objects.filter(contact_type__in=['customer', 'both']).order_by('name'),
            'is_htmx': is_htmx(request),
        }
        return render(request, template_name, context)


# ============================================================
# 4. INVOICE UPDATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:invoice_list', htmx_template='sales/partials/invoice_form_modal.html')
def invoice_update(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    sync_session_items(request, invoice)

    template_name = 'sales/partials/invoice_form_modal.html' if is_htmx(request) else 'sales/invoice_form.html'

    if request.method == 'POST':
        form = InvoiceForm(request.POST, instance=invoice)
        if form.is_valid():
            invoice = form.save(commit=False)
            # Delete existing items
            invoice.items.all().delete()
            # Create new items from session
            for item_data in request.session.get('temp_invoice_items', []):
                InvoiceItem.objects.create(
                    invoice=invoice,
                    product_id=item_data['product_id'],
                    quantity=Decimal(item_data['quantity']),
                    unit_price=Decimal(item_data['unit_price']),
                    tax_rate=Decimal(item_data['tax_rate']),
                    description=item_data.get('description', '')
                )
            request.session['temp_invoice_items'] = []

            invoice.calculate_totals()
            invoice.save()  # This syncs ledger automatically

            logger.info(f"Invoice {invoice.invoice_number} updated by {request.user.username}")

            if is_htmx(request):
                messages.success(request, f"Invoice {invoice.invoice_number} updated successfully.")
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:invoice_detail', args=[invoice.pk])
                return response

            messages.success(request, "Invoice updated.")
            return redirect_to_staff('invoice_detail', pk=invoice.pk)

        else:
            temp_items = request.session.get('temp_invoice_items', [])
            context = {
                'form': form,
                'invoice': invoice,
                'items': temp_items,
                'products': Product.objects.filter(is_active=True).order_by('name'),
                'customers': Contact.objects.filter(contact_type__in=['customer', 'both']).order_by('name'),
                'is_htmx': is_htmx(request),
            }
            if is_htmx(request):
                return render(request, 'sales/partials/invoice_form_modal.html', context)
            return render(request, 'sales/invoice_form.html', context)

    else:
        form = InvoiceForm(instance=invoice)
        temp_items = request.session.get('temp_invoice_items', [])
        context = {
            'form': form,
            'invoice': invoice,
            'items': temp_items,
            'products': Product.objects.filter(is_active=True).order_by('name'),
            'customers': Contact.objects.filter(contact_type__in=['customer', 'both']).order_by('name'),
            'is_htmx': is_htmx(request),
        }
        return render(request, template_name, context)


# ============================================================
# 5. INVOICE DELETE
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_delete(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)

    # ── Safety: block delete if payments are allocated ──
    if invoice.payment_allocations.exists():
        return toast_only_response(
            {
                'level': 'danger',
                'message': (
                    f'Cannot delete {invoice.invoice_number} — '
                    f'payments are linked. Remove/reallocate payments first.'
                )
            },
            status=400,
        )

    invoice_number = invoice.invoice_number
    customer = invoice.customer

    # 1. Handle Repair Job (revert status and unlink invoice)
    repair_job = None
    try:
        repair_job = RepairJob.objects.filter(invoice=invoice).first()
        if repair_job:
            if repair_job.status == 'delivered':
                repair_job.status = 'ready'
            repair_job.invoice = None
            repair_job.save(update_fields=['status', 'invoice'])
            logger.info(f"Repair job {repair_job.job_number} reverted to 'ready'.")
    except Exception as e:
        logger.error(f"Error reverting repair job status: {e}")

    for entry in LedgerEntry.objects.filter(
        reference_id=invoice.id, entry_type='sales'
    ):
        for line in list(entry.lines.all()):
            line.delete()
        entry.delete()

    for item in invoice.items.all():
        item.delete()

    # 4. Soft-delete the invoice header itself.
    invoice.delete()
    logger.info(f"Invoice {invoice_number} deleted by {request.user.username}")

    # 3. Notify customer (if they have a user account)
    try:
        if customer and customer.user:
            send_notification_to_customer(
                customer,
                title=f"Invoice Deleted: {invoice_number}",
                message=f"Invoice {invoice_number} has been removed from your account.",
                link=reverse('customer:customer_invoices'),
                notif_type='warning',
                category='sales',
                send_email=False,
            )
        for staff in User.objects.filter(is_staff=True):
            send_notification_sse(staff)
    except Exception as notif_err:
        logger.error(f"Invoice delete notification failed: {notif_err}")

    # 4. Return HTMX response: trigger client-side reload to preserve filters
    message = f'Invoice {invoice_number} deleted.'
    if repair_job:
        message += f' Repair #{repair_job.job_number} reverted to Ready.'

    if is_htmx(request):
        response = HttpResponse()
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': message},
            'reloadInvoices': ''
        })
        return response

    messages.success(request, message)
    return redirect_to_staff('invoice_list')


# ============================================================
# 6. ADD INVOICE ITEM (SESSION)
# ============================================================

def _safe_decimal(value, default=Decimal('0')):
    """Safely parse a value to Decimal, returning default on empty/invalid."""
    if value is None:
        return default
    value = str(value).strip()
    if value == '':
        return default
    try:
        return Decimal(value)
    except (ValueError, TypeError, ArithmeticError):
        return default


@csrf_protect
def add_invoice_item(request):
    if request.method != 'POST':
        return HttpResponse("Method not allowed", status=405)

    try:
        product_id = request.POST.get('product')
        if not product_id:
            return HttpResponse("Product is required.", status=400)

        product = get_object_or_404(Product, pk=product_id)
        qty = _safe_decimal(request.POST.get('quantity'), Decimal('1'))
        price = _safe_decimal(request.POST.get('unit_price'), product.selling_price or Decimal('0'))
        tax = _safe_decimal(request.POST.get('tax_rate'), product.tax_rate or Decimal('0'))

        if qty <= 0:
            return HttpResponse("Quantity must be positive.", status=400)
        if price < 0:
            return HttpResponse("Price cannot be negative.", status=400)
        if tax < 0 or tax > 100:
            return HttpResponse("Tax rate must be between 0 and 100.", status=400)

        line_total = (qty * price * (1 + tax / 100)).quantize(Decimal('0.01'))

        item = {
            'product_id': product.id,
            'product_name': product.name,
            'quantity': str(qty),
            'unit_price': str(price),
            'tax_rate': str(tax),
            'line_total': str(line_total),
            'description': request.POST.get('description', '')
        }

        items = request.session.get('temp_invoice_items', [])
        items.append(item)
        request.session['temp_invoice_items'] = items

        items_total = sum(Decimal(i['line_total']) for i in items)
        return render(request, 'sales/partials/invoice_items.html', {'items': items, 'items_total': items_total})

    except Exception as e:
        logger.error(f"Error adding invoice item: {e}")
        return HttpResponse("Error adding item.", status=500)


# ============================================================
# 7. REMOVE INVOICE ITEM (SESSION)
# ============================================================
@csrf_protect
def remove_invoice_item(request, index):
    try:
        items = request.session.get('temp_invoice_items', [])
        idx = int(index)
        if 0 <= idx < len(items):
            items.pop(idx)
            request.session['temp_invoice_items'] = items
        else:
            return HttpResponse("Invalid index.", status=400)

        items_total = sum(
            _safe_decimal(i.get('line_total')) for i in items
        )
        return render(request, 'sales/partials/invoice_items.html', {
            'items': items, 'items_total': items_total
        })

    except (ValueError, IndexError):
        return HttpResponse("Invalid index.", status=400)


# ============================================================
# 8. INVOICE DETAIL
# ============================================================
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related('customer'), pk=pk)
    payment_allocations = invoice.payment_allocations.select_related('payment').all().order_by('-payment__date')
    payments = [pa.payment for pa in payment_allocations] 
    context = {
        'invoice': invoice,
        'payments': payments,
        'payment_allocations': payment_allocations,
    }
    return render(request, 'sales/invoice_detail.html', context)


# ============================================================
# 9. INVOICE PRINT
# ============================================================
def invoice_print(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related('customer'), pk=pk)
    company = CompanyProfile.get_instance()
    logo_exists = bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name))
    gst_breakup = invoice.get_gst_breakup()  # Ensure this method exists; if not, implement
    context = {
        'invoice': invoice,
        'company': company,
        'logo_exists': logo_exists,
        'gst_breakup': gst_breakup,
    }
    return render(request, 'sales/invoice_print.html', context)

# ============================================================
# INVOICE LIST — PRINT
# ============================================================
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_list_print(request):
    """
    Print-friendly invoice list with current filters — no pagination.

    Reads filters directly from request.GET (not from a paginated context)
    so we can apply batch-prefetch on the FULL result set.
    """
    search = request.GET.get('search', '').strip()
    customer_id = request.GET.get('customer', '')
    status = request.GET.get('status', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    queryset = Invoice.objects.select_related('customer').all().order_by('-date')

    if search:
        queryset = queryset.filter(
            Q(invoice_number__icontains=search) |
            Q(customer__name__icontains=search)
        )
    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)
    if status == 'overdue':
        queryset = queryset.filter(
            payment_status__in=['unpaid', 'partial'],
            due_date__lt=timezone.now().date(),
        )
    elif status:
        queryset = queryset.filter(payment_status=status)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    # Materialise list once — avoids re-evaluation
    invoices_list = list(queryset)

    # ============================================================
    # Batch-prefetch linked RepairJobs for the FULL list.
    # Mirrors the logic in get_paginated_invoices_context but
    # operates on all rows (no pagination).
    # ============================================================
    invoice_ids = [inv.pk for inv in invoices_list if inv.pk]
    if invoice_ids:
        repair_map = {
            rj.invoice_id: rj
            for rj in RepairJob.objects
                .filter(invoice_id__in=invoice_ids)
                .only('id', 'invoice_id', 'job_number', 'device_model', 'status')
        }
        for inv in invoices_list:
            inv.linked_repair = repair_map.get(inv.pk)

    # Totals over filtered set
    total_sales = queryset.aggregate(
        total=Sum('grand_total')
    )['total'] or Decimal('0')
    total_received = queryset.filter(payment_status='paid').aggregate(
        total=Sum('grand_total')
    )['total'] or Decimal('0')
    total_unpaid = total_sales - total_received

    # Customer name for filter-display header
    customer_name = None
    if customer_id:
        try:
            customer_name = Contact.objects.get(pk=customer_id).name
        except (Contact.DoesNotExist, ValueError, TypeError):
            pass

    company = CompanyProfile.get_instance()

    context = {
        'invoices': invoices_list,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
        'search': search,
        'customer_id': customer_id,
        'customer_name': customer_name,
        'status': status,
        'date_from': date_from,
        'date_to': date_to,
        'total_sales': total_sales,
        'total_received': total_received,
        'total_unpaid': total_unpaid,
    }
    return render(request, 'sales/invoice_list_print.html', context)


# ============================================================
# INVOICE LIST — EXCEL EXPORT
# ============================================================
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_list_excel(request):
    """Export filtered invoices to Excel."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return toast_only_response(
            {'level': 'danger', 'message': 'Openpyxl is not installed.'},
            status=400
        )

    # Reuse filter logic
    queryset = Invoice.objects.select_related('customer').all().order_by('-date')
    search = request.GET.get('search', '').strip()
    customer_id = request.GET.get('customer', '')
    status = request.GET.get('status', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    if search:
        queryset = queryset.filter(
            Q(invoice_number__icontains=search) | Q(customer__name__icontains=search)
        )
    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)
    if status:
        queryset = queryset.filter(payment_status=status)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoices"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin = Side(style='thin', color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center')
    left = Alignment(horizontal='left', vertical='center')
    right = Alignment(horizontal='right', vertical='center')
    money_fmt = '#,##0.00'

    company = CompanyProfile.get_instance()
    ws.merge_cells('A1:H1')
    ws['A1'] = f"{company.name or 'A1 Computer Solutions'} — Invoice List"
    ws['A1'].font = Font(bold=True, size=14)
    ws['A1'].alignment = center
    ws.merge_cells('A2:H2')
    ws['A2'] = f"Generated: {timezone.now().strftime('%d-%m-%Y %H:%M')} | Total: {queryset.count()}"
    ws['A2'].alignment = center

    headers = ['Invoice #', 'Date', 'Customer', 'Amount', 'Paid', 'Due',
               'GST Type', 'Status']
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=4, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center
        c.border = border

    row = 5
    total_amt = Decimal('0')
    total_paid = Decimal('0')
    total_due = Decimal('0')

    for inv in queryset:
        total_amt += inv.grand_total
        total_paid += inv.paid_amount
        total_due += inv.balance_due

        ws.cell(row=row, column=1, value=inv.invoice_number)
        ws.cell(row=row, column=2, value=inv.date.strftime('%d-%m-%Y'))
        ws.cell(row=row, column=3, value=inv.customer.name)
        ws.cell(row=row, column=4, value=float(inv.grand_total)).number_format = money_fmt
        ws.cell(row=row, column=5, value=float(inv.paid_amount)).number_format = money_fmt
        ws.cell(row=row, column=6, value=float(inv.balance_due)).number_format = money_fmt
        ws.cell(row=row, column=7, value=inv.get_gst_type_display())
        ws.cell(row=row, column=8, value=inv.get_payment_status_display())

        for col in range(1, 9):
            ws.cell(row=row, column=col).border = border
            if col in (4, 5, 6):
                ws.cell(row=row, column=col).alignment = right
        row += 1

    # Totals row
    ws.cell(row=row, column=3, value='Totals').font = Font(bold=True)
    ws.cell(row=row, column=4, value=float(total_amt)).number_format = money_fmt
    ws.cell(row=row, column=5, value=float(total_paid)).number_format = money_fmt
    ws.cell(row=row, column=6, value=float(total_due)).number_format = money_fmt
    for col in (4, 5, 6):
        ws.cell(row=row, column=col).font = Font(bold=True)
    for col in range(1, 9):
        ws.cell(row=row, column=col).border = border

    widths = [14, 12, 30, 14, 14, 14, 18, 12]
    for idx, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = 'A5'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="invoices_{timezone.now().strftime("%Y%m%d_%H%M%S")}.xlsx"'
    wb.save(response)
    return response


# ============================================================
# 10. INVOICE DUPLICATE (copy invoice + items for fast entry)
# ============================================================
@login_required
@csrf_protect
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_duplicate(request, pk):
    """
    Clone an invoice into a fresh draft (same customer, items, discount
    settings). New invoice number, today's date, no payments.
    """
    from django.db import transaction

    source = get_object_or_404(Invoice, pk=pk)

    with transaction.atomic():
        new_invoice = Invoice.objects.create(
            customer=source.customer,
            date=timezone.now().date(),
            due_date=None,
            gst_type=source.gst_type,
            discount_amount=source.discount_amount,
            discount_type=source.discount_type,
            discount_note=source.discount_note,
            discount_date=None,
            notes=source.notes,
        )

        for item in source.items.all():
            InvoiceItem.objects.create(
                invoice=new_invoice,
                product=item.product,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                tax_rate=item.tax_rate,
            )

        new_invoice.calculate_totals()
        new_invoice.save()

    logger.info(
        f"Invoice {source.invoice_number} duplicated as "
        f"{new_invoice.invoice_number} by {request.user.username}"
    )

    if is_htmx(request):
        response = HttpResponse()
        response['HX-Redirect'] = reverse('accounting:invoice_update', args=[new_invoice.pk])
        return response
    messages.success(request, f"Duplicated as {new_invoice.invoice_number}.")
    return redirect_to_staff('invoice_update', pk=new_invoice.pk)


# ============================================================
# 11. INVOICE WHATSAPP SHARE (returns JSON with wa.me URL)
# ============================================================
@login_required
@require_http_methods(["GET"])
def invoice_whatsapp(request, pk):
    """
    Return JSON { url: <wa.me deep-link> } for sharing invoice via WhatsApp.
    Frontend opens it in a new tab.
    """
    invoice = get_object_or_404(Invoice, pk=pk)
    url = invoice.whatsapp_share_url
    if not url:
        return JsonResponse(
            {'error': 'Customer has no phone number on file.'},
            status=400
        )
    return JsonResponse({'url': url})


# ============================================================
# 12. CREDIT NOTES — LIST
# ============================================================
def _credit_notes_context(request):
    """Shared context for CN list + table."""
    search = request.GET.get('search', '').strip()
    customer_id = request.GET.get('customer', '')
    reason = request.GET.get('reason', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    page_number = request.GET.get('page', 1)

    qs = CreditNote.objects.select_related('customer', 'invoice').all().order_by('-date', '-id')

    if search:
        qs = qs.filter(
            Q(credit_note_number__icontains=search) |
            Q(invoice__invoice_number__icontains=search) |
            Q(customer__name__icontains=search)
        )
    if customer_id:
        qs = qs.filter(customer_id=customer_id)
    if reason:
        qs = qs.filter(reason=reason)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    paginator = Paginator(qs, 20)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    total_amount = qs.aggregate(t=Sum('total_amount'))['t'] or Decimal('0')

    return {
        'credit_notes': page_obj.object_list,
        'page_obj': page_obj,
        'customers': Contact.objects.filter(
            contact_type__in=['customer', 'both']
        ).order_by('name'),
        'reason_choices': CreditNote.REASON_CHOICES,
        'search': search,
        'customer_id': customer_id,
        'reason': reason,
        'date_from': date_from,
        'date_to': date_to,
        'total_amount': total_amount,
    }


@handle_errors(default_redirect='accounting:credit_note_list')
def credit_note_list(request):
    context = _credit_notes_context(request)
    if is_htmx(request):
        return render(request, 'sales/partials/credit_note_table.html', context)
    return render(request, 'sales/credit_note_list.html', context)


# ============================================================
# 13. CREDIT NOTES — CREATE (from Invoice)
# ============================================================
@login_required
@csrf_protect
@handle_errors(
    default_redirect='accounting:invoice_list',
    htmx_template='sales/partials/credit_note_form.html',
)
def credit_note_create(request, invoice_pk):
    invoice = get_object_or_404(
        Invoice.objects.select_related('customer'), pk=invoice_pk
    )

    if request.method == 'POST':
        # ── Parse item selections ──
        reason = request.POST.get('reason', 'sales_return')
        notes = request.POST.get('notes', '').strip()
        refund_method = request.POST.get('refund_method', 'credit')
        is_stock_return = request.POST.get('is_stock_return') == 'on'
        cn_date = request.POST.get('date') or timezone.now().date()

        # items: item_<id>=on  qty_<id>=N
        selections = []
        for item in invoice.items.all():
            checked = request.POST.get(f'item_{item.pk}') == 'on'
            if not checked:
                continue
            qty_str = request.POST.get(f'qty_{item.pk}', '').strip()
            try:
                qty = Decimal(qty_str)
            except (ValueError, TypeError):
                continue
            if qty <= 0:
                continue
            if qty > item.quantity:
                messages.error(
                    request,
                    f'Quantity for {item.product.name} cannot exceed {item.quantity}.'
                )
                # fall-through to render error form
                break
            selections.append((item, qty))

        if not selections:
            return htmx_response(
                request,
                'sales/partials/credit_note_form.html',
                context={
                    'invoice': invoice,
                    'error': 'Please select at least one item with quantity > 0.',
                },
                extra_headers={'HX-Retarget': '#mainModalContent'},
                status=400,
            )

        with transaction.atomic():
            cn = CreditNote.objects.create(
                invoice=invoice,
                customer=invoice.customer,
                date=cn_date,
                reason=reason,
                notes=notes,
                refund_method=refund_method,
                is_stock_return=is_stock_return,
                created_by=request.user,
            )

            for item, qty in selections:
                CreditNoteItem.objects.create(
                    credit_note=cn,
                    invoice_item=item,
                    product=item.product,
                    quantity_returned=qty,
                    unit_price=item.unit_price,
                    tax_rate=item.tax_rate,
                )

            cn.calculate_totals()
            cn.save()  # triggers ledger sync

        logger.info(
            f"Credit Note {cn.credit_note_number} created against "
            f"{invoice.invoice_number} by {request.user.username}"
        )

        # Customer notification
        try:
            if invoice.customer and invoice.customer.user:
                send_notification_to_customer(
                    invoice.customer,
                    title=f"Credit Note: {cn.credit_note_number}",
                    message=(
                        f"A credit of ₹{cn.total_amount} has been issued "
                        f"against invoice {invoice.invoice_number}."
                    ),
                    link=reverse('customer:customer_invoices'),
                    notif_type='info',
                    category='sales',
                    send_email=False,
                )
            for staff in User.objects.filter(is_staff=True):
                send_notification_sse(staff)
        except Exception as notif_err:
            logger.error(f"CN notification failed: {notif_err}")

        if is_htmx(request):
            response = HttpResponse()
            response['HX-Redirect'] = reverse(
                'accounting:credit_note_detail', args=[cn.pk]
            )
            return response
        return redirect_to_staff('credit_note_detail', pk=cn.pk)

    # GET — show form
    return render(
        request,
        'sales/partials/credit_note_form.html',
        {'invoice': invoice},
    )


# ============================================================
# 14. CREDIT NOTES — DETAIL
# ============================================================
def credit_note_detail(request, pk):
    cn = get_object_or_404(
        CreditNote.objects.select_related('invoice', 'customer'), pk=pk
    )
    context = {
        'credit_note': cn,
        'invoice': cn.invoice,
    }
    return render(request, 'sales/credit_note_detail.html', context)


# ============================================================
# 15. CREDIT NOTES — PRINT
# ============================================================
def credit_note_print(request, pk):
    cn = get_object_or_404(
        CreditNote.objects.select_related('invoice', 'customer'), pk=pk
    )
    company = CompanyProfile.get_instance()
    logo_exists = bool(
        company.logo and company.logo.name and company.logo.storage.exists(company.logo.name)
    )
    return render(request, 'sales/credit_note_print.html', {
        'credit_note': cn,
        'invoice': cn.invoice,
        'company': company,
        'logo_exists': logo_exists,
    })


# ============================================================
# 16. CREDIT NOTES — DELETE
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:credit_note_list')
def credit_note_delete(request, pk):
    cn = get_object_or_404(CreditNote, pk=pk)
    cn_number = cn.credit_note_number
    customer = cn.customer

    with transaction.atomic():
        # Delete ledger 
        LedgerEntry.objects.filter(
            entry_type='credit_note', reference_id=cn.id
        ).delete()
        # Delete items (their delete() reverses stock)
        cn.items.all().delete()
        # Hard-delete CN row (soft delete would leave ledger out of sync)
        cn.delete()

    logger.info(f"Credit Note {cn_number} deleted by {request.user.username}")

    if is_htmx(request):
        response = HttpResponse()
        response['HX-Trigger'] = json.dumps({
            'showToast': {
                'level': 'success',
                'message': f'Credit Note {cn_number} deleted.',
            },
            'reloadCreditNotes': '',
        })
        return response
    messages.success(request, f'Credit Note {cn_number} deleted.')
    return redirect_to_staff('credit_note_list')


# ============================================================
# 17. CREDIT NOTES — WHATSAPP SHARE URL
# ============================================================
@login_required
@require_http_methods(["GET"])
def credit_note_whatsapp(request, pk):
    cn = get_object_or_404(CreditNote, pk=pk)
    url = cn.whatsapp_share_url
    if not url:
        return JsonResponse(
            {'error': 'Customer has no phone number on file.'}, status=400
        )
    return JsonResponse({'url': url})