# accounting/views/purchases.py
import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q, Sum
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.urls import reverse

from ..models import (
    Purchase, PurchaseItem, Product, Contact, CompanyProfile,
    LedgerEntry,
)
from ..forms import PurchaseForm, PurchaseItemForm, ProductForm
from accounting.utils.notification_helpers import (
    send_notification_to_staff,
    send_notification_sse,
)
from .utils import is_htmx, htmx_response, redirect_to_staff, toast_only_response
from ..decorators import handle_errors

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Border, Side, Alignment, numbers
    from openpyxl.utils import get_column_letter
except ImportError:
    Workbook = None

logger = logging.getLogger(__name__)


# ============================================================
# HELPER: Safe Decimal parsing
# ============================================================
def _safe_decimal(value, default=Decimal('0')):
    """Safely parse a value to Decimal, returning default on empty/invalid."""
    if value is None:
        return default
    s = str(value).strip()
    if s == '':
        return default
    try:
        return Decimal(s)
    except (ValueError, TypeError, ArithmeticError):
        return default


# ============================================================
# HELPER: Sync session items from DB (edit mode)
# ============================================================
def sync_session_items(request, purchase=None):
    """Load purchase items into session for edit mode."""
    session_key = 'temp_purchase_items'
    if purchase:
        if session_key not in request.session or not request.session[session_key]:
            items = []
            for item in purchase.items.select_related('product').all():
                items.append({
                    'product_id': item.product.id,
                    'product_name': item.product.name,
                    'quantity': str(item.quantity),
                    'unit_price': str(item.unit_price),
                    'tax_rate': str(item.tax_rate),
                    'line_total': str(item.line_total),
                    'is_office_use': item.is_office_use,
                    'db_item_id': item.id,
                })
            request.session[session_key] = items
    return request.session.get(session_key, [])


# ============================================================
# HELPER: Paginated purchases context
# ============================================================
def get_paginated_purchases_context(request, queryset=None):
    if queryset is None:
        queryset = Purchase.objects.select_related('vendor').all().order_by('-date', '-id')

    search = request.GET.get('search', '').strip()
    vendor_id = request.GET.get('vendor', '')
    paid = request.GET.get('paid', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    page_number = request.GET.get('page', 1)

    if search:
        queryset = queryset.filter(
            Q(purchase_number__icontains=search) |
            Q(vendor__name__icontains=search)
        )
    if vendor_id:
        queryset = queryset.filter(vendor_id=vendor_id)
    if paid == 'yes':
        queryset = queryset.filter(paid=True)
    elif paid == 'no':
        queryset = queryset.filter(paid=False)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    paginator = Paginator(queryset, 15)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    vendors = Contact.objects.filter(
        contact_type__in=['vendor', 'both']
    ).order_by('name')

    total_amount = queryset.aggregate(total=Sum('grand_total'))['total'] or Decimal('0')
    total_paid = queryset.filter(paid=True).aggregate(
        total=Sum('grand_total')
    )['total'] or Decimal('0')
    total_unpaid = total_amount - total_paid

    return {
        'purchases': page_obj.object_list,
        'page_obj': page_obj,
        'vendors': vendors,
        'search': search,
        'vendor_id': vendor_id,
        'paid': paid,
        'date_from': date_from,
        'date_to': date_to,
        'total_amount': total_amount,
        'total_paid': total_paid,
        'total_unpaid': total_unpaid,
    }


# ============================================================
# FIELD VALIDATION (HTMX)
# ============================================================
@require_http_methods(["GET"])
def validate_purchase_field(request):
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    value = request.GET.get(field_name, '')
    purchase_id = request.GET.get('purchase_id')

    try:
        if purchase_id:
            instance = Purchase.objects.get(pk=purchase_id)
            form = PurchaseForm(data={field_name: value}, instance=instance)
        else:
            form = PurchaseForm(data={field_name: value})
        form.full_clean()
        errors = form.errors.get(field_name, [])
        error_html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
        for err in errors:
            error_html += f'<div><i class="bi bi-exclamation-circle me-1"></i>{err}</div>'
        error_html += '</div>'
        return HttpResponse(error_html)
    except Exception as e:
        logger.error(f"Validation error on {field_name}: {e}")
        return HttpResponse(
            f'<div id="field-{field_name}" class="invalid-feedback d-block">Server error</div>'
        )


# ============================================================
# PRODUCT SEARCH (autocomplete)
# ============================================================
def purchase_product_search(request):
    q = request.GET.get('q', '').strip()
    products = Product.objects.filter(is_service=False, is_active=True)

    if len(q) >= 2:
        products = products.filter(
            Q(name__icontains=q) | Q(hsn_code__icontains=q)
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
    else:
        products = []

    return render(
        request, 'purchases/partials/product_suggestions.html',
        {'products': products}
    )


# ============================================================
# PRODUCT QUICK ADD
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_product_quick_add(request):
    if request.method == 'POST':
        form = ProductForm(request.POST)
        if form.is_valid():
            product = form.save()
            logger.info(
                f"Product '{product.name}' quick-added in purchase "
                f"by {request.user.username}"
            )

            response = render(
                request, 'purchases/partials/product_quick_add_success.html',
                {'product': product}
            )
            response['HX-Trigger'] = json.dumps({
                'productCreated': {
                    'id': product.id,
                    'name': product.name,
                    'price': str(product.purchase_price),
                    'tax': str(product.tax_rate),
                },
                'closeModal': '',
                'showToast': {
                    'level': 'success',
                    'message': f'Product "{product.name}" created successfully!',
                },
            })
            return response
        else:
            return render(
                request, 'purchases/partials/product_quick_add_form.html',
                {'form': form}
            )
    else:
        form = ProductForm()
        return render(
            request, 'purchases/partials/product_quick_add_form.html',
            {'form': form}
        )


# ============================================================
# PURCHASE LIST
# ============================================================
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_list(request):
    context = get_paginated_purchases_context(request)
    if is_htmx(request):
        return render(request, 'purchases/partials/purchase_table.html', context)
    return render(request, 'purchases/purchase_list.html', context)


# ============================================================
# PURCHASE LIST — PRINT
# ============================================================
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_list_print(request):
    """Print-friendly purchase list with filters."""
    queryset = Purchase.objects.select_related('vendor').all().order_by('-date')
    search = request.GET.get('search', '').strip()
    vendor_id = request.GET.get('vendor', '')
    paid = request.GET.get('paid', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    if search:
        queryset = queryset.filter(
            Q(purchase_number__icontains=search) | Q(vendor__name__icontains=search)
        )
    if vendor_id:
        queryset = queryset.filter(vendor_id=vendor_id)
    if paid == 'yes':
        queryset = queryset.filter(paid=True)
    elif paid == 'no':
        queryset = queryset.filter(paid=False)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    total_amount = queryset.aggregate(total=Sum('grand_total'))['total'] or Decimal('0')
    total_paid = queryset.filter(paid=True).aggregate(
        total=Sum('grand_total')
    )['total'] or Decimal('0')
    total_unpaid = total_amount - total_paid

    company = CompanyProfile.get_instance()

    return render(request, 'purchases/purchase_list_print.html', {
        'purchases': queryset,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
        'search': search,
        'vendor_id': vendor_id,
        'paid': paid,
        'date_from': date_from,
        'date_to': date_to,
        'total_amount': total_amount,
        'total_paid': total_paid,
        'total_unpaid': total_unpaid,
    })


# ============================================================
# PURCHASE LIST — EXCEL (filter-aware)
# ============================================================
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_list_excel(request):
    """Export filtered purchases to Excel."""
    if Workbook is None:
        return toast_only_response(
            {'level': 'danger', 'message': 'Openpyxl library is not installed.'},
            status=400
        )

    queryset = Purchase.objects.select_related('vendor').all().order_by('-date')
    search = request.GET.get('search', '').strip()
    vendor_id = request.GET.get('vendor', '')
    paid = request.GET.get('paid', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    if search:
        queryset = queryset.filter(
            Q(purchase_number__icontains=search) | Q(vendor__name__icontains=search)
        )
    if vendor_id:
        queryset = queryset.filter(vendor_id=vendor_id)
    if paid == 'yes':
        queryset = queryset.filter(paid=True)
    elif paid == 'no':
        queryset = queryset.filter(paid=False)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    wb = Workbook()
    ws = wb.active
    ws.title = "Purchases"

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
    ws['A1'] = f"{company.name or 'A1 Computer Solutions'} — Purchase List"
    ws['A1'].font = Font(bold=True, size=14, color="1F4E78")
    ws['A1'].alignment = center

    ws.merge_cells('A2:H2')
    ws['A2'] = f"Generated: {timezone.now().strftime('%d-%m-%Y %H:%M')} | Total: {queryset.count()}"
    ws['A2'].alignment = center

    headers = ['Purchase #', 'Date', 'Vendor', 'Subtotal', 'Freight',
               'Tax', 'Grand Total', 'Status']
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=4, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center
        c.border = border

    row = 5
    total_amt = Decimal('0')
    total_paid = Decimal('0')
    for p in queryset:
        total_amt += p.grand_total
        if p.paid:
            total_paid += p.grand_total

        ws.cell(row=row, column=1, value=p.purchase_number)
        ws.cell(row=row, column=2, value=p.date.strftime('%d-%m-%Y'))
        ws.cell(row=row, column=3, value=p.vendor.name)
        ws.cell(row=row, column=4, value=float(p.subtotal)).number_format = money_fmt
        ws.cell(row=row, column=5, value=float(p.freight_charge or 0)).number_format = money_fmt
        ws.cell(row=row, column=6, value=float(p.tax_amount)).number_format = money_fmt
        ws.cell(row=row, column=7, value=float(p.grand_total)).number_format = money_fmt
        ws.cell(row=row, column=8, value='Paid' if p.paid else 'Unpaid')

        for col in range(1, 9):
            ws.cell(row=row, column=col).border = border
            if col in (4, 5, 6, 7):
                ws.cell(row=row, column=col).alignment = right
        row += 1

    # Totals
    ws.cell(row=row, column=3, value='Totals').font = Font(bold=True)
    ws.cell(row=row, column=4, value=float(total_amt)).number_format = money_fmt
    ws.cell(row=row, column=4).font = Font(bold=True)
    ws.cell(row=row, column=7, value=float(total_paid)).number_format = money_fmt
    ws.cell(row=row, column=7).font = Font(bold=True)
    for col in range(1, 9):
        ws.cell(row=row, column=col).border = border

    widths = [16, 14, 30, 14, 14, 14, 16, 12]
    for idx, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = 'A5'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f'attachment; filename="purchases_{timezone.now().strftime("%Y%m%d_%H%M%S")}.xlsx"'
    )
    wb.save(response)
    return response


# ============================================================
# PURCHASE CREATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_create(request):
    if 'temp_purchase_items' not in request.session:
        request.session['temp_purchase_items'] = []

    template_name = (
        'purchases/partials/purchase_form_partial.html'
        if is_htmx(request) else 'purchases/purchase_form.html'
    )

    if request.method == 'POST':
        form = PurchaseForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                purchase = form.save(commit=False)
                purchase.save()

                for item_data in request.session.get('temp_purchase_items', []):
                    PurchaseItem.objects.create(
                        purchase=purchase,
                        product_id=item_data['product_id'],
                        quantity=_safe_decimal(item_data['quantity'], Decimal('1')),
                        unit_price=_safe_decimal(item_data['unit_price']),
                        tax_rate=_safe_decimal(item_data['tax_rate']),
                        is_office_use=item_data.get('is_office_use', False),
                    )

                request.session['temp_purchase_items'] = []
                purchase.calculate_totals()
                purchase.save()  # triggers sync_purchase_ledger

            logger.info(
                f"Purchase {purchase.purchase_number} created "
                f"by {request.user.username}"
            )

            # ---- Notify staff ----
            try:
                send_notification_to_staff(
                    title=f"New Purchase: {purchase.purchase_number}",
                    message=f"Purchase of ₹{purchase.grand_total} from {purchase.vendor.name}",
                    link=reverse('accounting:purchase_detail', args=[purchase.pk]),
                    notif_type='info',
                    category='purchases',
                    send_email=False,
                )
                for staff in User.objects.filter(is_staff=True):
                    send_notification_sse(staff)
            except Exception as notif_err:
                logger.error(f"Purchase create notification failed: {notif_err}")

            if is_htmx(request):
                messages.success(
                    request,
                    f"Purchase {purchase.purchase_number} created successfully."
                )
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:purchase_detail', args=[purchase.pk])
                return response

            messages.success(request, f"Purchase {purchase.purchase_number} created successfully.")
            return redirect('accounting:purchase_detail', pk=purchase.pk)

        else:
            items = request.session.get('temp_purchase_items', [])
            items_total = sum(
                (_safe_decimal(i.get('line_total')) for i in items),
                Decimal('0')
            )
            context = {
                'form': form,
                'items': items,
                'items_total': items_total,
                'products': Product.objects.filter(
                    is_service=False, is_active=True
                ).order_by('name'),
                'purchase': None,
                'is_htmx': is_htmx(request),
            }
            if is_htmx(request):
                return render(
                    request, 'purchases/partials/purchase_form_partial.html', context
                )
            return render(request, 'purchases/purchase_form.html', context)

    else:
        form = PurchaseForm()
        items = request.session.get('temp_purchase_items', [])
        items_total = sum(
            (_safe_decimal(i.get('line_total')) for i in items),
            Decimal('0')
        )
        context = {
            'form': form,
            'items': items,
            'items_total': items_total,
            'products': Product.objects.filter(
                is_service=False, is_active=True
            ).order_by('name'),
            'purchase': None,
            'is_htmx': is_htmx(request),
        }
        return render(request, template_name, context)


# ============================================================
# PURCHASE UPDATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_update(request, pk):
    purchase = get_object_or_404(Purchase.objects.select_related('vendor'), pk=pk)
    sync_session_items(request, purchase)

    template_name = (
        'purchases/partials/purchase_form_partial.html'
        if is_htmx(request) else 'purchases/purchase_form.html'
    )

    if request.method == 'POST':
        form = PurchaseForm(request.POST, instance=purchase)
        if form.is_valid():
            with transaction.atomic():
                purchase = form.save()

                # Delete old items (this reverses stock via StockMovement.delete)
                purchase.items.all().delete()

                # Create new items
                for item_data in request.session.get('temp_purchase_items', []):
                    PurchaseItem.objects.create(
                        purchase=purchase,
                        product_id=item_data['product_id'],
                        quantity=_safe_decimal(item_data['quantity'], Decimal('1')),
                        unit_price=_safe_decimal(item_data['unit_price']),
                        tax_rate=_safe_decimal(item_data['tax_rate']),
                        is_office_use=item_data.get('is_office_use', False),
                    )

                request.session['temp_purchase_items'] = []
                purchase.calculate_totals()
                purchase.save()

            logger.info(
                f"Purchase {purchase.purchase_number} updated "
                f"by {request.user.username}"
            )

            # Notify staff
            try:
                for staff in User.objects.filter(is_staff=True):
                    send_notification_sse(staff)
            except Exception as notif_err:
                logger.error(f"Purchase update notification failed: {notif_err}")

            if is_htmx(request):
                messages.success(
                    request,
                    f"Purchase {purchase.purchase_number} updated successfully."
                )
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:purchase_detail', args=[purchase.pk])
                return response

            messages.success(request, f"Purchase {purchase.purchase_number} updated successfully.")
            return redirect('accounting:purchase_detail', pk=purchase.pk)

        else:
            items = request.session.get('temp_purchase_items', [])
            items_total = sum(
                (_safe_decimal(i.get('line_total')) for i in items),
                Decimal('0')
            )
            context = {
                'form': form,
                'purchase': purchase,
                'items': items,
                'items_total': items_total,
                'products': Product.objects.filter(
                    is_service=False, is_active=True
                ).order_by('name'),
                'is_htmx': is_htmx(request),
            }
            if is_htmx(request):
                return render(
                    request, 'purchases/partials/purchase_form_partial.html', context
                )
            return render(request, 'purchases/purchase_form.html', context)

    else:
        form = PurchaseForm(instance=purchase)
        items = request.session.get('temp_purchase_items', [])
        items_total = sum(
            (_safe_decimal(i.get('line_total')) for i in items),
            Decimal('0')
        )
        context = {
            'form': form,
            'purchase': purchase,
            'items': items,
            'items_total': items_total,
            'products': Product.objects.filter(
                is_service=False, is_active=True
            ).order_by('name'),
            'is_htmx': is_htmx(request),
        }
        return render(request, template_name, context)


# ============================================================
# PURCHASE DELETE
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_delete(request, pk):
    purchase = get_object_or_404(Purchase, pk=pk)
    purchase_name = purchase.purchase_number
    vendor = purchase.vendor

    with transaction.atomic():
        # Delete ledger entry AND its lines (Purchase.delete does not auto-delete ledger)
        for entry in LedgerEntry.objects.filter(
            reference_id=purchase.id, entry_type='purchase'
        ):
            for line in list(entry.lines.all()):
                line.delete()
            entry.delete()

        for item in purchase.items.all():
            item.delete()

        # Now soft-delete the purchase header itself.
        purchase.delete()

    logger.info(f"Purchase {purchase_name} deleted by {request.user.username}")

    # Notify staff
    try:
        send_notification_to_staff(
            title=f"Purchase Deleted: {purchase_name}",
            message=f"Purchase from {vendor.name if vendor else 'Unknown'} was deleted.",
            link=reverse('accounting:purchase_list'),
            notif_type='warning',
            category='purchases',
            send_email=False,
        )
        for staff in User.objects.filter(is_staff=True):
            send_notification_sse(staff)
    except Exception as notif_err:
        logger.error(f"Purchase delete notification failed: {notif_err}")

    if is_htmx(request):
        # Trigger client-side reload to preserve filters
        response = HttpResponse()
        response['HX-Trigger'] = json.dumps({
            'showToast': {
                'level': 'success',
                'message': f'Purchase {purchase_name} deleted.'
            },
            'reloadPurchases': ''
        })
        return response

    messages.success(request, f'Purchase {purchase_name} deleted.')
    return redirect_to_staff('purchase_list')


# ============================================================
# ADD PURCHASE ITEM (session-based)
# ============================================================
@csrf_protect
def add_purchase_item(request):
    if request.method != 'POST':
        return HttpResponse("Method not allowed", status=405)

    product_id = request.POST.get('product')
    if not product_id:
        return HttpResponse("Product is required.", status=400)

    try:
        product = get_object_or_404(Product, pk=product_id)
        qty = _safe_decimal(request.POST.get('quantity'), Decimal('1'))
        price = _safe_decimal(request.POST.get('unit_price'), product.purchase_price or Decimal('0'))
        tax = _safe_decimal(request.POST.get('tax_rate'), product.tax_rate or Decimal('0'))
        is_office = request.POST.get('is_office_use') == 'on'

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
            'is_office_use': is_office,
        }

        items = request.session.get('temp_purchase_items', [])
        items.append(item)
        request.session['temp_purchase_items'] = items

        items_total = sum(
            (_safe_decimal(i.get('line_total')) for i in items), Decimal('0')
        )
        return render(
            request, 'purchases/partials/purchase_items.html',
            {'items': items, 'items_total': items_total}
        )

    except Exception as e:
        logger.error(f"Error adding purchase item: {e}")
        return HttpResponse("Error adding item.", status=500)


# ============================================================
# REMOVE PURCHASE ITEM
# ============================================================
@csrf_protect
def remove_purchase_item(request, index):
    items = request.session.get('temp_purchase_items', [])
    try:
        idx = int(index)
        if 0 <= idx < len(items):
            items.pop(idx)
            request.session['temp_purchase_items'] = items
        else:
            return HttpResponse("Invalid index.", status=400)
    except (ValueError, IndexError):
        return HttpResponse("Invalid index.", status=400)

    items_total = sum(
        (_safe_decimal(i.get('line_total')) for i in items), Decimal('0')
    )
    return render(
        request, 'purchases/partials/purchase_items.html',
        {'items': items, 'items_total': items_total}
    )


# ============================================================
# PURCHASE DETAIL
# ============================================================
def purchase_detail(request, pk):
    purchase = get_object_or_404(Purchase.objects.select_related('vendor'), pk=pk)
    # Any linked payments (future-proof)
    context = {
        'purchase': purchase,
    }
    return render(request, 'purchases/purchase_detail.html', context)


# ============================================================
# PURCHASE PRINT
# ============================================================
def purchase_print(request, pk):
    purchase = get_object_or_404(Purchase.objects.select_related('vendor'), pk=pk)
    company = CompanyProfile.get_instance()
    logo_exists = bool(
        company.logo and company.logo.name and company.logo.storage.exists(company.logo.name)
    )
    return render(request, 'purchases/purchase_print.html', {
        'purchase': purchase,
        'company': company,
        'logo_exists': logo_exists,
    })