import json
import logging
from decimal import Decimal
from datetime import datetime

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum, F
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.utils import timezone

from ..models import Product, StockMovement, CompanyProfile
from ..forms import StockMovementForm
from .utils import is_htmx, htmx_response, redirect_to_staff
from ..decorators import handle_errors

# Optional Excel export
try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    openpyxl = None

logger = logging.getLogger(__name__)


# ============================================================
# HELPER: Update product stock and optionally latest purchase price
# ============================================================
def update_product_stock(product, quantity, price=None, is_purchase=False):
    """
    Update product stock and optionally the purchase price (if purchase).
    Returns the new current_stock.
    """
    if quantity == 0:
        return product.current_stock

    product.current_stock += quantity
    product.save(update_fields=['current_stock'])

    if is_purchase and price is not None:
        product.purchase_price = price
        product.save(update_fields=['purchase_price'])

    return product.current_stock


# ============================================================
# HELPER: Get auto-selling price (with markup)
# ============================================================
def get_auto_selling_price(product, markup_percent=20):
    """
    Returns the auto-calculated selling price.
    If product.selling_price is set ( > 0 ), use that,
    else use purchase_price + markup_percent.
    """
    if product.selling_price and product.selling_price > 0:
        return product.selling_price
    return product.purchase_price * (1 + Decimal(markup_percent) / 100)


# ============================================================
# HELPER: Get paginated stock movement context (with running balance)
# ============================================================
def get_paginated_stock_context(request, queryset=None):
    """
    Filters, paginates, and annotates stock movements with running balance (Tally style).
    """
    if queryset is None:
        queryset = StockMovement.objects.select_related('product').all().order_by('-date')

    # ---------- Filters ----------
    product_filter = request.GET.get('product', '').strip()
    movement_type = request.GET.get('movement_type', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    reset = request.GET.get('reset', '')
    page_number = request.GET.get('page', 1)

    if reset:
        product_filter = movement_type = date_from = date_to = ''

    if product_filter:
        queryset = queryset.filter(product_id=product_filter)
    if movement_type:
        queryset = queryset.filter(movement_type=movement_type)
    if date_from:
        queryset = queryset.filter(date__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__date__lte=date_to)

    # ---------- Pagination ----------
    paginator = Paginator(queryset, 20)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # ---------- Running Balance (Tally style) ----------
    movements_list = list(page_obj.object_list)
    page_list_reversed = list(reversed(movements_list))
    product_balances = {}
    for movement in page_list_reversed:
        prod_id = movement.product_id
        cum_sum = product_balances.get(prod_id, 0)
        cum_sum += movement.quantity
        product_balances[prod_id] = cum_sum
        movement.running_balance = cum_sum

    # ---------- Summary totals ----------
    total_in = queryset.filter(quantity__gt=0).aggregate(total=Sum('quantity'))['total'] or Decimal('0')
    total_out = queryset.filter(quantity__lt=0).aggregate(total=Sum('quantity'))['total'] or Decimal('0')

    products = Product.objects.filter(is_service=False, is_active=True).order_by('name')
    movement_choices = StockMovement.MOVEMENT_TYPE

    context = {
        'movements': movements_list,
        'page_obj': page_obj,
        'products': products,
        'movement_choices': movement_choices,
        'product_filter': product_filter,
        'movement_type': movement_type,
        'date_from': date_from,
        'date_to': date_to,
        'total_in': total_in,
        'total_out': abs(total_out),   # absolute for display
        'net_change': total_in + total_out,
    }
    return context


# ============================================================
# 1. STOCK ADJUSTMENT LIST (with filters, pagination, running balance)
# ============================================================
@handle_errors(default_redirect='accounting:stock_adjustment_list')
def stock_adjustment_list(request):
    context = get_paginated_stock_context(request)
    if is_htmx(request):
        return render(request, 'stock/partials/stock_movement_table.html', context)
    return render(request, 'stock/stock_adjustment.html', context)


# ============================================================
# 2. STOCK ADJUSTMENT ADD (Manual correction via Modal)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:stock_adjustment_list', htmx_template='stock/partials/stock_adjustment_form.html')
def stock_adjustment_add(request):
    """
    Add a manual stock adjustment (HTMX modal).
    This is used for corrections like damaged items, physical count adjustments, etc.
    """
    template_name = 'stock/partials/stock_adjustment_form.html' if is_htmx(request) else 'stock/stock_adjustment_form.html'

    if request.method == 'POST':
        form = StockMovementForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                movement = form.save()
                # Update product stock
                update_product_stock(movement.product, movement.quantity, is_purchase=False)
                logger.info(f"Stock adjustment added: {movement.product.name} ({movement.quantity}) by {request.user.username}")

                if is_htmx(request):
                    context = get_paginated_stock_context(request)
                    return htmx_response(
                        request,
                        'stock/partials/stock_movement_table.html',
                        context=context,
                        toast={'level': 'success', 'message': f'Stock adjustment for "{movement.product.name}" recorded.'},
                        close_modal=True
                    )
                messages.success(request, f'Stock adjustment for "{movement.product.name}" recorded.')
                return redirect_to_staff('stock_adjustment_list')
        else:
            # Invalid form – keep in modal
            if is_htmx(request):
                return render(request, 'stock/partials/stock_adjustment_form.html', {'form': form})
    else:
        initial = {
            'date': timezone.now(),
            'movement_type': 'adjustment',
        }
        # Pre-fill product when arriving from Stock Dashboard "Add Stock" button
        product_id = request.GET.get('product')
        if product_id:
            initial['product'] = product_id
        form = StockMovementForm(initial=initial)

    return render(request, template_name, {'form': form})


# ============================================================
# 3. STOCK ADJUSTMENT DELETE 
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:stock_adjustment_list')
def stock_adjustment_delete(request, pk):
    movement = get_object_or_404(StockMovement, pk=pk)
    product_name = movement.product.name

    # StockMovement.delete() already reverses the stock effect safely
    # (current_stock -= quantity). Do NOT re-sum movements here — that would
    # wipe out any manual opening stock the product was created with.
    movement.delete()

    logger.info(f"Stock movement {pk} deleted by {request.user.username} (product: {product_name})")

    context = get_paginated_stock_context(request)
    return htmx_response(
        request,
        'stock/partials/stock_movement_table.html',
        context=context,
        toast={'level': 'success', 'message': f'Stock movement for "{product_name}" deleted.'}
    )

# ============================================================
# 4. AJAX: GET PRODUCT PRICE INFO (for Sale/Purchase forms)
# ============================================================
@require_http_methods(["GET"])
def get_product_price_info(request):
    """
    AJAX endpoint to fetch product current stock, purchase price, and auto-selling price.
    Used in purchase/sale forms to auto-fill fields.
    """
    product_id = request.GET.get('product_id')
    if not product_id:
        return JsonResponse({'error': 'Product ID required'}, status=400)

    try:
        product = Product.objects.get(pk=product_id)
        auto_selling_price = get_auto_selling_price(product)

        return JsonResponse({
            'success': True,
            'current_stock': float(product.current_stock),
            'purchase_price': float(product.purchase_price),
            'selling_price': float(product.selling_price or 0),
            'auto_selling_price': float(auto_selling_price),
        })
    except Product.DoesNotExist:
        return JsonResponse({'error': 'Product not found'}, status=404)


# ============================================================
# 5. LIVE PRODUCT SEARCH (for autocomplete in forms)
# ============================================================
def product_search_stock(request):
    """
    HTMX autocomplete: search products by name or HSN.
    Returns a partial with product suggestions including stock info.
    """
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return render(request, 'stock/partials/product_search_results.html', {'products': []})

    products = Product.objects.filter(
        Q(name__icontains=q) | Q(hsn_code__icontains=q),
        is_active=True,
        is_service=False
    )[:10]

    # Annotate with auto-selling price
    for product in products:
        product.auto_price = get_auto_selling_price(product)

    return render(request, 'stock/partials/product_search_results.html', {'products': products})


# ============================================================
# 6. STOCK DASHBOARD (Overview)
# ============================================================
@handle_errors(default_redirect='accounting:stock_adjustment_list')
def stock_dashboard(request):
    """
    A quick overview dashboard showing low stock items, total quantities, etc.
    """
    products = Product.objects.filter(is_service=False).order_by('name')

    total_products = products.count()
    low_stock = products.filter(current_stock__lte=F('low_stock_threshold')).count()
    total_quantity = products.aggregate(total=Sum('current_stock'))['total'] or Decimal('0')

    # Top 5 low stock products
    low_stock_products = products.filter(current_stock__lte=F('low_stock_threshold'))[:5]

    context = {
        'total_products': total_products,
        'low_stock': low_stock,
        'total_quantity': total_quantity,
        'low_stock_products': low_stock_products,
    }
    return render(request, 'stock/stock_dashboard.html', context)


# ============================================================
# 7. EXPORT STOCK MOVEMENTS TO EXCEL (Professional)
# ============================================================
@handle_errors(default_redirect='accounting:stock_adjustment_list')
def export_stock_excel(request):
    """
    Export filtered stock movements to a styled Excel (.xlsx) file.
    Applies the same filters as the list view.
    """
    if openpyxl is None:
        messages.error(request, "Openpyxl library is not installed. Please install it to export Excel.")
        return redirect_to_staff('stock_adjustment_list')

    # Build the filtered queryset (without pagination)
    queryset = StockMovement.objects.select_related('product').all().order_by('-date')

    product_filter = request.GET.get('product', '').strip()
    movement_type = request.GET.get('movement_type', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    if product_filter:
        queryset = queryset.filter(product_id=product_filter)
    if movement_type:
        queryset = queryset.filter(movement_type=movement_type)
    if date_from:
        queryset = queryset.filter(date__date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__date__lte=date_to)

    # Create workbook
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Stock Movements"

    # Styles
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    center_align = Alignment(horizontal='center', vertical='center')
    right_align = Alignment(horizontal='right', vertical='center')

    company = CompanyProfile.get_instance()
    ws.merge_cells('A1:G1')
    ws['A1'] = company.name
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A2:G2')
    ws['A2'] = "Stock Movement Report"
    ws['A2'].font = Font(bold=True, size=12)
    ws.merge_cells('A3:G3')
    ws['A3'] = f"Generated: {datetime.now().strftime('%d-%b-%Y %H:%M')}"
    ws['A3'].alignment = Alignment(horizontal="center")

    # Headers
    headers = ['Date', 'Product', 'Type', 'Quantity', 'Reference', 'Notes', 'Balance (After)']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=5, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = center_align

    # We need to compute running balance for the entire queryset (for export)
    # Since it's a full export, we'll compute running balance per product across all records
    # But to keep it simple, we can show the cumulative sum per product based on chronological order.
    # Convert queryset to list in reverse chronological order for calculation.
    # We'll process oldest to newest for running balance, but we display newest first.

    # For export, we'll display in the same order as the list: newest first.
    # We'll compute the running balance from the bottom up.
    queryset_list = list(queryset)
    # Reverse to get oldest first for cumulative calculation
    reversed_list = list(reversed(queryset_list))
    prod_balance = {}
    for movement in reversed_list:
        prod_id = movement.product_id
        cum = prod_balance.get(prod_id, 0)
        cum += movement.quantity
        prod_balance[prod_id] = cum
        # store it on the movement for later display
        movement.running_balance = cum

    # Now write rows (newest first)
    row = 6
    for movement in queryset_list:
        ws.cell(row=row, column=1, value=movement.date.strftime('%d-%b-%Y %H:%M'))
        ws.cell(row=row, column=2, value=movement.product.name)
        ws.cell(row=row, column=3, value=movement.get_movement_type_display())
        ws.cell(row=row, column=4, value=float(movement.quantity))
        ws.cell(row=row, column=4).number_format = '#,##0.00'
        ws.cell(row=row, column=5, value=movement.reference or '')
        ws.cell(row=row, column=6, value=movement.notes or '')
        # Running balance
        ws.cell(row=row, column=7, value=float(getattr(movement, 'running_balance', 0)))
        ws.cell(row=row, column=7).number_format = '#,##0.00'
        for col in range(1, 8):
            ws.cell(row=row, column=col).border = thin_border
        row += 1

    # Auto-width columns
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except:
                pass
        ws.column_dimensions[col_letter].width = max(min(max_len + 3, 40), 12)

    ws.freeze_panes = 'A6'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="stock_movements_{datetime.now().strftime("%Y%m%d")}.xlsx"'
    wb.save(response)
    return response