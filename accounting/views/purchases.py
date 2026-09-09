import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.contrib import messages
from django.db.models import Case, When, Value, IntegerField, Q, F, Sum
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.urls import reverse
from ..models import *
from ..forms import PurchaseForm, PurchaseItemForm, ProductForm
from .utils import is_htmx, htmx_response, redirect_to_staff, toast_only_response
from ..decorators import handle_errors

# Excel Export
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment, numbers
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)


# ============================================================
# HELPER: SYNC SESSION WITH DATABASE ITEMS (FOR EDIT)
# ============================================================
def sync_session_items(request, purchase=None):
    """
    Ensures session contains items from the purchase (for edit mode).
    If purchase is provided and session is empty, loads purchase items.
    """
    session_key = 'temp_purchase_items'
    if purchase:
        if session_key not in request.session or not request.session[session_key]:
            items = []
            for item in purchase.items.all():
                items.append({
                    'product_id': item.product.id,
                    'product_name': item.product.name,
                    'quantity': str(item.quantity),
                    'unit_price': str(item.unit_price),
                    'tax_rate': str(item.tax_rate),
                    'line_total': str(item.line_total),
                    'db_item_id': item.id,
                })
            request.session[session_key] = items
    return request.session.get(session_key, [])


# ============================================================
# HELPER: GET PAGINATED PURCHASES CONTEXT
# ============================================================
def get_paginated_purchases_context(request, queryset=None):
    if queryset is None:
        queryset = Purchase.objects.select_related('vendor').all().order_by('-date')

    search = request.GET.get('search', '').strip()
    vendor_id = request.GET.get('vendor', '')
    paid = request.GET.get('paid', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    reset = request.GET.get('reset', '')
    page_number = request.GET.get('page', 1)

    if reset:
        search = vendor_id = paid = date_from = date_to = ''

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

    vendors = Contact.objects.filter(contact_type__in=['vendor', 'both']).order_by('name')

    total_amount = queryset.aggregate(total=Sum('grand_total'))['total'] or Decimal('0')
    total_paid = queryset.filter(paid=True).aggregate(total=Sum('grand_total'))['total'] or Decimal('0')
    total_unpaid = total_amount - total_paid

    context = {
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
    return context


# ============================================================
# 1. FIELD VALIDATION (HTMX)
# ============================================================
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
            error_html += f'<div>{err}</div>'
        error_html += '</div>'
        return HttpResponse(error_html)
    except Exception as e:
        logger.error(f"Validation error on {field_name}: {e}")
        return HttpResponse(f'<div id="field-{field_name}" class="invalid-feedback d-block">Server error</div>')


# ============================================================
# 2. PRODUCT SEARCH (Autocomplete)
# ============================================================
def purchase_product_search(request):
    q = request.GET.get('q', '').strip()
    products = Product.objects.filter(is_service=False, is_active=True)

    if len(q) >= 2:
        products = products.filter(
            Q(name__icontains=q) |
            Q(hsn_code__icontains=q)
        )
        products = products[:20]

        def relevance_score(product):
            name = product.name.lower()
            hsn = (product.hsn_code or '').lower()
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

    return render(request, 'purchases/partials/product_suggestions.html', {'products': products})


# ============================================================
# 3. PRODUCT QUICK ADD (Modal)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_product_quick_add(request):
    if request.method == 'POST':
        form = ProductForm(request.POST)
        if form.is_valid():
            product = form.save()
            logger.info(f"Product '{product.name}' quick-added in purchase by {request.user.username}")

            response = render(request, 'purchases/partials/product_quick_add_success.html', {'product': product})
            response['HX-Trigger'] = json.dumps({
                'productCreated': {
                    'id': product.id,
                    'name': product.name,
                    'price': str(product.purchase_price),
                    'tax': str(product.tax_rate)
                },
                'closeModal': '',
                'showToast': {
                    'level': 'success',
                    'message': f'Product "{product.name}" created successfully!'
                }
            })
            return response
        else:
            return render(request, 'purchases/partials/product_quick_add_form.html', {'form': form})
    else:
        form = ProductForm()
        return render(request, 'purchases/partials/product_quick_add_form.html', {'form': form})


# ============================================================
# 4. PURCHASE LIST
# ============================================================
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_list(request):
    context = get_paginated_purchases_context(request)
    if is_htmx(request):
        return render(request, 'purchases/partials/purchase_table.html', context)
    return render(request, 'purchases/purchase_list.html', context)


# ============================================================
# 5. PURCHASE CREATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_create(request):
    if 'temp_purchase_items' not in request.session:
        request.session['temp_purchase_items'] = []

    template_name = 'purchases/partials/purchase_form_partial.html' if is_htmx(request) else 'purchases/purchase_form.html'

    if request.method == 'POST':
        form = PurchaseForm(request.POST)
        if form.is_valid():
            purchase = form.save(commit=False)
            purchase.save()  # Save to get ID

            for item_data in request.session.get('temp_purchase_items', []):
                PurchaseItem.objects.create(
                    purchase=purchase,
                    product_id=item_data['product_id'],
                    quantity=Decimal(item_data['quantity']),
                    unit_price=Decimal(item_data['unit_price']),
                    tax_rate=Decimal(item_data['tax_rate']),
                )

            request.session['temp_purchase_items'] = []
            purchase.calculate_totals()
            purchase.save()  # This will trigger sync_purchase_ledger automatically

            logger.info(f"Purchase {purchase.purchase_number} created by {request.user.username}")

            if is_htmx(request):
                messages.success(request, f"Purchase {purchase.purchase_number} created successfully.")
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:purchase_list')
                return response

            messages.success(request, f"Purchase {purchase.purchase_number} created successfully.")
            return redirect('accounting:purchase_list')

        else:
            items = request.session.get('temp_purchase_items', [])
            items_total = sum(Decimal(i['line_total']) for i in items) if items else Decimal('0')
            context = {
                'form': form,
                'items': items,
                'items_total': items_total,
                'products': Product.objects.filter(is_service=False, is_active=True).order_by('name'),
                'purchase': None,
                'is_htmx': is_htmx(request),
            }
            if is_htmx(request):
                return render(request, 'purchases/partials/purchase_form_partial.html', context)
            return render(request, 'purchases/purchase_form.html', context)

    else:
        form = PurchaseForm()
        items = request.session.get('temp_purchase_items', [])
        items_total = sum(Decimal(i['line_total']) for i in items) if items else Decimal('0')
        context = {
            'form': form,
            'items': items,
            'items_total': items_total,
            'products': Product.objects.filter(is_service=False, is_active=True).order_by('name'),
            'purchase': None,
            'is_htmx': is_htmx(request),
        }
        return render(request, template_name, context)


# ============================================================
# 6. PURCHASE UPDATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_update(request, pk):
    purchase = get_object_or_404(Purchase.objects.select_related('vendor'), pk=pk)
    sync_session_items(request, purchase)

    template_name = 'purchases/partials/purchase_form_partial.html' if is_htmx(request) else 'purchases/purchase_form.html'

    if request.method == 'POST':
        form = PurchaseForm(request.POST, instance=purchase)
        if form.is_valid():
            purchase = form.save()
            purchase.items.all().delete()

            for item_data in request.session.get('temp_purchase_items', []):
                PurchaseItem.objects.create(
                    purchase=purchase,
                    product_id=item_data['product_id'],
                    quantity=Decimal(item_data['quantity']),
                    unit_price=Decimal(item_data['unit_price']),
                    tax_rate=Decimal(item_data['tax_rate']),
                )

            request.session['temp_purchase_items'] = []
            purchase.calculate_totals()
            purchase.save()  # This triggers ledger sync

            logger.info(f"Purchase {purchase.purchase_number} updated by {request.user.username}")

            if is_htmx(request):
                messages.success(request, f"Purchase {purchase.purchase_number} updated successfully.")
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:purchase_list')
                return response

            messages.success(request, f"Purchase {purchase.purchase_number} updated successfully.")
            return redirect('accounting:purchase_list')

        else:
            items = request.session.get('temp_purchase_items', [])
            items_total = sum(Decimal(i['line_total']) for i in items) if items else Decimal('0')
            context = {
                'form': form,
                'purchase': purchase,
                'items': items,
                'items_total': items_total,
                'products': Product.objects.filter(is_service=False, is_active=True).order_by('name'),
                'is_htmx': is_htmx(request),
            }
            if is_htmx(request):
                return render(request, 'purchases/partials/purchase_form_partial.html', context)
            return render(request, 'purchases/purchase_form.html', context)

    else:
        form = PurchaseForm(instance=purchase)
        items = request.session.get('temp_purchase_items', [])
        items_total = sum(Decimal(i['line_total']) for i in items) if items else Decimal('0')
        context = {
            'form': form,
            'purchase': purchase,
            'items': items,
            'items_total': items_total,
            'products': Product.objects.filter(is_service=False, is_active=True).order_by('name'),
            'is_htmx': is_htmx(request),
        }
        return render(request, template_name, context)


# ============================================================
# 7. PURCHASE DELETE (CORRECTED – NO MANUAL STOCK REVERSAL)
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:purchase_list')
def purchase_delete(request, pk):
    purchase = get_object_or_404(Purchase, pk=pk)

    # Delete ledger entry (since Purchase.delete does not auto-delete ledger)
    LedgerEntry.objects.filter(reference_id=purchase.id, entry_type='purchase').delete()

    purchase_name = purchase.purchase_number
    purchase.delete()  # This will cascade delete PurchaseItems, which reverse stock automatically

    logger.info(f"Purchase {purchase_name} deleted by {request.user.username}")

    context = get_paginated_purchases_context(request)
    return htmx_response(
        request,
        'purchases/partials/purchase_table.html',
        context=context,
        toast={'level': 'success', 'message': f'Purchase {purchase_name} deleted.'}
    )


# ============================================================
# 8. ADD PURCHASE ITEM (session)
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
        qty = Decimal(request.POST.get('quantity', 1))
        price = Decimal(request.POST.get('unit_price', product.purchase_price))
        tax = Decimal(request.POST.get('tax_rate', product.tax_rate))

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
            'is_office_use': request.POST.get('is_office_use') == 'on'
        }

        items = request.session.get('temp_purchase_items', [])
        items.append(item)
        request.session['temp_purchase_items'] = items

        items_total = sum(Decimal(i['line_total']) for i in items) if items else Decimal('0')
        context = {'items': items, 'items_total': items_total}
        return render(request, 'purchases/partials/purchase_items.html', context)

    except Exception as e:
        logger.error(f"Error adding purchase item: {e}")
        return HttpResponse("Error adding item.", status=500)


# ============================================================
# 9. REMOVE PURCHASE ITEM (session)
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

    items_total = sum(Decimal(i['line_total']) for i in items) if items else Decimal('0')
    context = {'items': items, 'items_total': items_total}
    return render(request, 'purchases/partials/purchase_items.html', context)


# ============================================================
# 10. PURCHASE DETAIL
# ============================================================
def purchase_detail(request, pk):
    purchase = get_object_or_404(Purchase.objects.select_related('vendor'), pk=pk)
    return render(request, 'purchases/purchase_detail.html', {'purchase': purchase})


# ============================================================
# 11. PURCHASE PRINT
# ============================================================
def purchase_print(request, pk):
    purchase = get_object_or_404(Purchase.objects.select_related('vendor'), pk=pk)
    company = CompanyProfile.get_instance()
    logo_exists = bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name))
    context = {
        'purchase': purchase,
        'company': company,
        'logo_exists': logo_exists,
    }
    return render(request, 'purchases/purchase_print.html', context)


# ============================================================
# 12. EXPORT PURCHASES TO EXCEL
# ============================================================
@require_http_methods(["GET"])
def export_purchases_excel(request):
    purchases = Purchase.objects.select_related('vendor').all().order_by('-date')

    wb = Workbook()
    ws = wb.active
    ws.title = "Purchases List"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    center_align = Alignment(horizontal='center', vertical='center')
    left_align = Alignment(horizontal='left', vertical='center')
    right_align = Alignment(horizontal='right', vertical='center')
    money_format = numbers.FORMAT_NUMBER_COMMA_SEPARATED1

    ws.merge_cells('A1:G1')
    title_cell = ws.cell(row=1, column=1, value="📥 Purchase List – A1 Computer Solutions")
    title_cell.font = Font(bold=True, size=14, color="1F4E78")
    title_cell.alignment = center_align
    ws.row_dimensions[1].height = 30

    headers = ['Purchase #', 'Date', 'Vendor', 'Subtotal', 'Tax Amount', 'Grand Total', 'Paid']
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = center_align
        ws.row_dimensions[2].height = 25

    for idx, purchase in enumerate(purchases, start=3):
        row_num = idx
        row_fill = PatternFill(
            start_color="F2F6FC" if idx % 2 == 0 else "FFFFFF",
            end_color="F2F6FC" if idx % 2 == 0 else "FFFFFF",
            fill_type="solid"
        )
        row_data = [
            purchase.purchase_number,
            purchase.date.strftime('%d-%b-%Y'),
            purchase.vendor.name,
            float(purchase.subtotal),
            float(purchase.tax_amount),
            float(purchase.grand_total),
            'Yes' if purchase.paid else 'No'
        ]
        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=value)
            cell.border = thin_border
            cell.fill = row_fill
            if col_idx in (1, 7):
                cell.alignment = center_align
            elif col_idx in (4, 5, 6):
                cell.alignment = right_align
                cell.number_format = money_format
            else:
                cell.alignment = left_align

    for col in ws.columns:
        max_length = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except:
                pass
        ws.column_dimensions[col_letter].width = max(min(max_length + 3, 50), 12)

    ws.freeze_panes = 'A3'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename="purchases.xlsx"'
    wb.save(response)
    return response