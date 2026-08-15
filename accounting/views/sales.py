import json
import logging
from decimal import Decimal

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

from ..models import *
from ..forms import InvoiceForm, InvoiceItemForm, PaymentForm
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
# HELPER 2: LEDGER ENTRY
# ============================================================
def create_or_update_invoice_ledger(invoice):
    try:
        LedgerEntry.objects.filter(reference_id=invoice.id, entry_type='sales').delete()
        entry = LedgerEntry.objects.create(
            date=invoice.date,
            entry_type='sales',
            reference_id=invoice.id,
            description=f"Invoice {invoice.invoice_number}",
            total_amount=invoice.grand_total,
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=f'Customer: {invoice.customer.name}',
            contact=invoice.customer,
            debit=invoice.grand_total,
            credit=0,
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account='Sales Revenue',
            debit=0,
            credit=invoice.grand_total,
        )
        logger.info(f"Ledger entry created for invoice {invoice.invoice_number}")
    except Exception as e:
        logger.error(f"Ledger error for {invoice.invoice_number}: {e}")
        raise


# ============================================================
# HELPER 3: REVERSE STOCK ON DELETE
# ============================================================
def reverse_invoice_stock(invoice):
    """Add stock back when invoice is deleted."""
    for item in invoice.items.all():
        if not item.product.is_service:
            Product.objects.filter(pk=item.product_id).update(
                current_stock=F('current_stock') + item.quantity
            )
            StockMovement.objects.create(
                product=item.product,
                movement_type='adjustment',
                quantity=item.quantity,
                reference=f"INV_DEL_{invoice.invoice_number}",
                date=timezone.now().date(),
                notes=f"Stock restored due to deletion of invoice {invoice.invoice_number}"
            )
    logger.info(f"Stock reversed for deleted invoice {invoice.invoice_number}")


# ============================================================
# HELPER 4: GET PAGINATED INVOICES CONTEXT
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
    if status:
        queryset = queryset.filter(payment_status=status)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    paginator = Paginator(queryset, 20)  # 20 per page
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    customers = Contact.objects.filter(contact_type__in=['customer', 'both']).order_by('name')

    total_sales = queryset.aggregate(total=Sum('grand_total'))['total'] or Decimal('0')
    total_received = queryset.filter(payment_status='paid').aggregate(total=Sum('grand_total'))['total'] or Decimal('0')
    total_unpaid = total_sales - total_received

    context = {
        'invoices': page_obj.object_list,
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
# 1. INVOICE LIST (WITH PAGINATION)
# ============================================================
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_list(request):
    context = get_paginated_invoices_context(request)
    if is_htmx(request):
        return render(request, 'sales/partials/invoice_table.html', context)
    return render(request, 'sales/invoice_list.html', context)


# ============================================================
# 2. PRODUCT SEARCH (WITH RELEVANCE ORDERING - SQLITE FRIENDLY)
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

    # Python Relevance Sorting (SQLite में Case/When reliable नहीं है)
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
    if 'temp_invoice_items' not in request.session:
        request.session['temp_invoice_items'] = []

    template_name = 'sales/partials/invoice_form_modal.html' if is_htmx(request) else 'sales/invoice_form.html'

    if request.method == 'POST':
        form = InvoiceForm(request.POST)
        if form.is_valid():
            invoice = form.save(commit=False)
            invoice.save()

            temp_items = request.session.get('temp_invoice_items', [])
            for item in temp_items:
                InvoiceItem.objects.create(
                    invoice=invoice,
                    product_id=item['product_id'],
                    quantity=Decimal(item['quantity']),
                    unit_price=Decimal(item['unit_price']),
                    tax_rate=Decimal(item['tax_rate']),
                    description=item.get('description', '')
                )
            request.session['temp_invoice_items'] = []

            invoice.calculate_totals()
            invoice.save()
            invoice.update_stock_from_items()  # Stock Update
            create_or_update_invoice_ledger(invoice)

            logger.info(f"Invoice {invoice.invoice_number} created by {request.user.username}")

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
        # GET Request
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
# 4. INVOICE UPDATE (WITH SESSION SYNC)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:invoice_list', htmx_template='sales/partials/invoice_form_modal.html')
def invoice_update(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)

    # Sync session with database items (if empty)
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
            invoice.save()
            invoice.update_stock_from_items()  # Stock Update
            create_or_update_invoice_ledger(invoice)

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
        # GET Request
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
# 5. INVOICE DELETE (WITH STOCK REVERSAL)
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_delete(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    invoice_number = invoice.invoice_number

    # Reverse Stock
    reverse_invoice_stock(invoice)

    # Delete Ledger
    LedgerEntry.objects.filter(reference_id=invoice.id, entry_type='sales').delete()
    invoice.delete()

    logger.info(f"Invoice {invoice_number} deleted by {request.user.username}")

    context = get_paginated_invoices_context(request)
    return htmx_response(
        request,
        'sales/partials/invoice_table.html',
        context=context,
        toast={'level': 'success', 'message': f'Invoice {invoice_number} deleted.'}
    )


# ============================================================
# 6. ADD INVOICE ITEM (SESSION)
# ============================================================
@csrf_protect
def add_invoice_item(request):
    if request.method != 'POST':
        return HttpResponse("Method not allowed", status=405)

    try:
        product_id = request.POST.get('product')
        if not product_id:
            return HttpResponse("Product is required.", status=400)

        product = get_object_or_404(Product, pk=product_id)
        qty = Decimal(request.POST.get('quantity', 1))
        price = Decimal(request.POST.get('unit_price', product.selling_price or 0))
        tax = Decimal(request.POST.get('tax_rate', product.tax_rate or 0))

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

        # Pass items_total to the partial
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

        items_total = sum(Decimal(i['line_total']) for i in items)
        return render(request, 'sales/partials/invoice_items.html', {'items': items, 'items_total': items_total})

    except (ValueError, IndexError):
        return HttpResponse("Invalid index.", status=400)


# ============================================================
# 8. INVOICE DETAIL
# ============================================================
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related('customer'), pk=pk)
    payments = invoice.payments_received.all().order_by('-date')
    context = {
        'invoice': invoice,
        'payments': payments,
        'payment_form': PaymentForm(initial={'direction': 'received', 'contact': invoice.customer})
    }
    return render(request, 'sales/invoice_detail.html', context)


# ============================================================
# 9. INVOICE PRINT
# ============================================================
def invoice_print(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related('customer'), pk=pk)
    company = CompanyProfile.get_instance()
    logo_exists = bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name))
    gst_breakup = invoice.get_gst_breakup()
    context = {
        'invoice': invoice,
        'company': company,
        'logo_exists': logo_exists,
        'gst_breakup': gst_breakup,
    }
    return render(request, 'sales/invoice_print.html', context)


# ============================================================
# 10. ADD PAYMENT (FROM INVOICE DETAIL)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:invoice_list')
def add_payment(request, invoice_pk):
    invoice = get_object_or_404(Invoice, pk=invoice_pk)

    if request.method == 'POST':
        form = PaymentForm(request.POST)
        if form.is_valid():
            payment = form.save()
            if not payment.invoices.exists():
                payment.invoices.add(invoice)
            invoice.update_paid_amount()
            logger.info(f"Payment {payment.id} recorded for {invoice.invoice_number}")

            if is_htmx(request):
                payments = invoice.payments_received.all().order_by('-date')
                response = render(request, 'sales/partials/payment_list.html', {'payments': payments})
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': 'Payment recorded.'},
                    'closeModal': ''
                })
                return response
            messages.success(request, "Payment recorded.")
            return redirect_to_staff('invoice_detail', pk=invoice.pk)
        else:
            if is_htmx(request):
                return render(request, 'payments/payment_form.html', {'form': form, 'invoice': invoice})
    else:
        form = PaymentForm(initial={
            'direction': 'received',
            'contact': invoice.customer,
            'amount': invoice.balance_due,
            'date': timezone.now().date(),
        })
    return render(request, 'payments/payment_form.html', {'form': form, 'invoice': invoice})



@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_delete(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    invoice_number = invoice.invoice_number

    # 1. Reverse Stock
    reverse_invoice_stock(invoice)

    # 2. Delete Ledger Entry
    LedgerEntry.objects.filter(reference_id=invoice.id, entry_type='sales').delete()

    # 3. Handle Repair Job (Revert status to 'ready')
    repair_job = None
    try:
        repair_job = RepairJob.objects.filter(invoice=invoice).first()
        if repair_job:
            if repair_job.status == 'delivered':
                repair_job.status = 'ready'
            repair_job.invoice = None
            repair_job.save(update_fields=['status', 'invoice'])
            logger.info(f"Repair job {repair_job.job_number} status reverted to 'ready'.")
    except Exception as e:
        logger.error(f"Error reverting repair job status: {e}")

    # 4. Delete the invoice
    invoice.delete()

    logger.info(f"Invoice {invoice_number} deleted by {request.user.username}")

    # 5. Return HTMX response
    context = get_paginated_invoices_context(request)
    return htmx_response(
        request,
        'sales/partials/invoice_table.html',
        context=context,
        toast={
            'level': 'success',
            'message': f'Invoice {invoice_number} deleted. ' + 
                       (f'Repair #{repair_job.job_number} reverted to Ready.' if repair_job else '')
        }
    )
    
    
