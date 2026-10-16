# accounting/views/purchases.py
import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.urls import reverse

from ..models import (
    Purchase, PurchaseItem, Product, Contact, CompanyProfile,
    LedgerEntry, Payment, PurchaseAllocation, BankAccount,
)
from ..forms import PurchaseForm, PurchaseItemForm, ProductForm, PaymentForm
from accounting.utils.notification_helpers import send_notification_to_staff
from .utils import htmx_field_error_response, is_htmx, htmx_response, redirect_to_staff, toast_only_response
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
# HELPER: Session items are scoped PER PURCHASE (scope = pk, or 'new').
# Earlier one global 'temp_purchase_items' key was shared by every bill:
# items left over from creating/editing PO A silently leaked into PO B's
# edit form — and submitting replaced B's items with A's (data corruption).
# ============================================================
def _items_session_key(scope):
    return f'temp_purchase_items_{scope or "new"}'


def _session_items(request, scope):
    return request.session.get(_items_session_key(scope), [])


def _set_session_items(request, scope, items):
    request.session[_items_session_key(scope)] = items


def sync_session_items(request, purchase=None):
    """Load purchase items into (scoped) session for edit mode."""
    scope = str(purchase.pk) if purchase and purchase.pk else 'new'
    key = _items_session_key(scope)
    if purchase and purchase.pk:
        # Fresh GET → always re-seed from DB (bill may have changed elsewhere).
        # POST (invalid re-render) → keep whatever the user was editing.
        # First visit on POST without a seeded key → seed from DB as fallback.
        if request.method == 'GET' or key not in request.session:
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
            request.session[key] = items
    return request.session.get(key, [])


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

    totals = queryset.aggregate(total=Sum('grand_total'), paid=Sum('paid_amount'))
    total_amount = totals['total'] or Decimal('0')
    total_paid = totals['paid'] or Decimal('0')
    total_unpaid = max(Decimal('0'), total_amount - total_paid)

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
        return htmx_field_error_response(field_name, errors)
    except Exception as e:
        logger.error(f"Validation error on {field_name}: {e}")
        return htmx_field_error_response(field_name, ['Server error'])


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

    totals = queryset.aggregate(total=Sum('grand_total'), paid=Sum('paid_amount'))
    total_amount = totals['total'] or Decimal('0')
    total_paid = totals['paid'] or Decimal('0')
    total_unpaid = max(Decimal('0'), total_amount - total_paid)

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
    ws.merge_cells('A1:J1')
    ws['A1'] = f"{company.name or 'A1 Computer Solutions'} — Purchase List"
    ws['A1'].font = Font(bold=True, size=14, color="1F4E78")
    ws['A1'].alignment = center

    ws.merge_cells('A2:J2')
    ws['A2'] = f"Generated: {timezone.now().strftime('%d-%m-%Y %H:%M')} | Total: {queryset.count()}"
    ws['A2'].alignment = center

    headers = ['Purchase #', 'Date', 'Vendor', 'Subtotal', 'Freight',
               'Tax', 'Grand Total', 'Paid', 'Outstanding', 'Status']
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
        total_paid += p.paid_amount

        ws.cell(row=row, column=1, value=p.purchase_number)
        ws.cell(row=row, column=2, value=p.date.strftime('%d-%m-%Y'))
        ws.cell(row=row, column=3, value=p.vendor.name)
        ws.cell(row=row, column=4, value=float(p.subtotal)).number_format = money_fmt
        ws.cell(row=row, column=5, value=float(p.freight_charge or 0)).number_format = money_fmt
        ws.cell(row=row, column=6, value=float(p.tax_amount)).number_format = money_fmt
        ws.cell(row=row, column=7, value=float(p.grand_total)).number_format = money_fmt
        ws.cell(row=row, column=8, value=float(p.paid_amount or 0)).number_format = money_fmt
        ws.cell(row=row, column=9, value=float(p.balance_due)).number_format = money_fmt
        status = p.settlement_status
        ws.cell(row=row, column=10, value=status.capitalize())

        for col in range(1, 11):
            ws.cell(row=row, column=col).border = border
            if col in (4, 5, 6, 7, 8, 9):
                ws.cell(row=row, column=col).alignment = right
        row += 1

    # Totals
    ws.cell(row=row, column=3, value='Totals').font = Font(bold=True)
    ws.cell(row=row, column=4, value=float(total_amt)).number_format = money_fmt
    ws.cell(row=row, column=4).font = Font(bold=True)
    ws.cell(row=row, column=7, value=float(total_amt)).number_format = money_fmt
    ws.cell(row=row, column=7).font = Font(bold=True)
    ws.cell(row=row, column=8, value=float(total_paid)).number_format = money_fmt
    ws.cell(row=row, column=8).font = Font(bold=True)
    ws.cell(row=row, column=9, value=float(total_amt - total_paid)).number_format = money_fmt
    ws.cell(row=row, column=9).font = Font(bold=True)
    for col in range(1, 11):
        ws.cell(row=row, column=col).border = border

    widths = [16, 14, 30, 14, 14, 14, 16, 14, 14, 12]
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
    if 'temp_purchase_items_new' not in request.session:
        request.session['temp_purchase_items_new'] = []

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

                for item_data in _session_items(request, 'new'):
                    PurchaseItem.objects.create(
                        purchase=purchase,
                        product_id=item_data['product_id'],
                        quantity=_safe_decimal(item_data['quantity'], Decimal('1')),
                        unit_price=_safe_decimal(item_data['unit_price']),
                        tax_rate=_safe_decimal(item_data['tax_rate']),
                        is_office_use=item_data.get('is_office_use', False),
                    )

                _set_session_items(request, 'new', [])
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
            items = _session_items(request, 'new')
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
        form = PurchaseForm(initial={'date': timezone.localdate()})
        items = _session_items(request, 'new')
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
    items = sync_session_items(request, purchase)
    scope = str(purchase.pk)

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
                for item_data in _session_items(request, scope):
                    PurchaseItem.objects.create(
                        purchase=purchase,
                        product_id=item_data['product_id'],
                        quantity=_safe_decimal(item_data['quantity'], Decimal('1')),
                        unit_price=_safe_decimal(item_data['unit_price']),
                        tax_rate=_safe_decimal(item_data['tax_rate']),
                        is_office_use=item_data.get('is_office_use', False),
                    )

                _set_session_items(request, scope, [])
                purchase.calculate_totals()
                purchase.save()
                # Bill total may have changed — keep Paid flag honest
                purchase.recalc_settlement()

            logger.info(
                f"Purchase {purchase.purchase_number} updated "
                f"by {request.user.username}"
            )

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
            items = _session_items(request, scope)
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
        items = _session_items(request, scope)
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

        scope = request.POST.get('purchase_id', '').strip() or 'new'
        items = _session_items(request, scope)
        items.append(item)
        _set_session_items(request, scope, items)

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
    scope = request.POST.get('purchase_id', '').strip() or 'new'
    items = _session_items(request, scope)
    try:
        idx = int(index)
        if 0 <= idx < len(items):
            items.pop(idx)
            _set_session_items(request, scope, items)
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

    allocations = (
        purchase.payment_allocations
        .select_related('payment')
        .order_by('-payment__date', '-id')
    )

    # Vendor payments that still have an un-applied remainder — one-click
    # "Apply" here is what settles a bill paid before this feature existed.
    vendor_payments = []
    paid_payments = (
        Payment.objects.filter(
            contact=purchase.vendor,
            direction='paid',
            is_advance=False,
            expense_account__isnull=True,
        )
        .prefetch_related('purchase_allocations')
        .order_by('-date', '-id')
    )
    balance_due = purchase.balance_due
    for p in paid_payments:
        applied = sum((a.amount for a in p.purchase_allocations.all()), Decimal('0'))
        remaining = p.net_amount - applied
        if remaining >= Decimal('0.01') and balance_due > 0:
            vendor_payments.append({
                'payment': p,
                'remaining': remaining,
                'suggested': min(remaining, balance_due),
            })

    context = {
        'purchase': purchase,
        'allocations': allocations,
        'vendor_payments': vendor_payments,
        'balance_due': balance_due,
        'bank_accounts': BankAccount.objects.filter(is_active=True).order_by('name'),
    }
    return render(request, 'purchases/purchase_detail.html', context)


# ============================================================
# PURCHASE SETTLEMENT — record a fresh payment against the bill
# ============================================================
@csrf_protect
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_record_payment(request, pk):
    purchase = get_object_or_404(
        Purchase.objects.select_related('vendor'), pk=pk
    )

    method = request.POST.get('method') or 'cash'
    data = {
        'direction': 'paid',
        'contact': purchase.vendor_id,
        'amount': request.POST.get('amount') or '0',
        'date': request.POST.get('date') or timezone.localdate(),
        'method': method,
        'bank_account': request.POST.get('bank_account') or '',
        'upi_ref': request.POST.get('upi_ref') or '',
        'reference': request.POST.get('reference') or '',
        'description': request.POST.get('description') or
                       f"Payment against {purchase.purchase_number}",
        'is_advance': '',
        'expense_account': '',
        'discount_amount': '0',
        'discount_type': '',
        'discount_note': '',
    }
    form = PaymentForm(data)
    if not form.is_valid():
        first_error = next((e[0] for e in form.errors.values() if e), None)
        messages.error(request, first_error or "Could not record payment.")
        return redirect('accounting:purchase_detail', pk=purchase.pk)

    with transaction.atomic():
        payment = form.save()
        alloc_amt = min(payment.net_amount, purchase.balance_due)
        if alloc_amt > 0:
            PurchaseAllocation.objects.create(
                payment=payment,
                purchase=purchase,
                amount=alloc_amt,
            )
        payment.update_purchases()

    logger.info(
        "Payment #%s recorded against %s by %s",
        payment.pk, purchase.purchase_number, request.user.username,
    )
    messages.success(
        request,
        f"Payment of ₹{payment.amount} recorded against {purchase.purchase_number}."
    )
    return redirect('accounting:purchase_detail', pk=purchase.pk)


# ============================================================
# PURCHASE SETTLEMENT — apply an existing (unallocated) vendor payment
# ============================================================
@csrf_protect
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_apply_payment(request, pk):
    purchase = get_object_or_404(Purchase, pk=pk)
    payment = get_object_or_404(Payment, pk=request.POST.get('payment_id') or 0)

    def _fail(msg):
        messages.error(request, msg)
        return redirect('accounting:purchase_detail', pk=purchase.pk)

    if payment.direction != 'paid':
        return _fail("Only 'Paid to Supplier' payments can settle a purchase bill.")
    if payment.contact_id != purchase.vendor_id:
        return _fail(f"Payment #{payment.pk} belongs to a different party.")
    if payment.is_advance:
        return _fail(
            "This is an advance payment — advance is NOT auto-applied; "
            "pay the balance against the bill with a normal payment instead."
        )
    if payment.expense_account_id:
        return _fail("This payment is booked as a direct expense, not a vendor settlement.")

    applied = sum(
        (a.amount for a in payment.purchase_allocations.all()), Decimal('0')
    )
    remaining = payment.net_amount - applied
    if remaining < Decimal('0.01'):
        return _fail(f"Payment #{payment.pk} is fully applied already.")

    requested = _safe_decimal(
        request.POST.get('amount'), default=remaining
    )
    amount = min(requested, remaining, purchase.balance_due)
    if amount <= 0:
        return _fail("Nothing left to apply — this bill has no balance due.")

    with transaction.atomic():
        existing = payment.purchase_allocations.filter(purchase=purchase).first()
        if existing:
            existing.amount = (existing.amount + amount).quantize(Decimal('0.01'))
            existing.save(update_fields=['amount'])
        else:
            PurchaseAllocation.objects.create(
                payment=payment, purchase=purchase, amount=amount,
            )
        payment.update_purchases()

    messages.success(
        request,
        f"Applied ₹{amount} from Payment #{payment.pk} to {purchase.purchase_number}."
    )
    return redirect('accounting:purchase_detail', pk=purchase.pk)


# ============================================================
# PURCHASE SETTLEMENT — manual mark paid / unpaid toggle
# (for bills settled out-of-band; blocked while allocations exist)
# ============================================================
@csrf_protect
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_toggle_paid(request, pk):
    purchase = get_object_or_404(Purchase, pk=pk)

    if purchase.paid:
        if purchase.payment_allocations.exists():
            messages.error(
                request,
                "This bill is settled through payment allocations — delete the "
                "linked payment(s) instead of unmarking manually.",
            )
        else:
            Purchase.objects.filter(pk=pk).update(paid=False)
            messages.success(request, f"{purchase.purchase_number} marked unpaid.")
    else:
        Purchase.objects.filter(pk=pk).update(
            paid=True, paid_amount=purchase.grand_total
        )
        messages.success(
            request,
            f"{purchase.purchase_number} marked paid "
            "(out-of-band settlement — ledger/vendor balance unchanged).",
        )

    return redirect('accounting:purchase_detail', pk=purchase.pk)


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