import json
import logging
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.template.loader import render_to_string
from django.db.models import Q, Sum
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.views.decorators.csrf import csrf_protect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods

from ..models import Product, ProductCategory, StockMovement
from ..forms import ProductForm, ProductCategoryForm
from .utils import is_htmx, htmx_response, redirect_to_staff, toast_only_response
from ..decorators import handle_errors

# Excel Export Imports
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment, numbers
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)


# ============================================================
# HELPER: GET PAGINATED PRODUCTS CONTEXT
# ============================================================
def get_paginated_products_context(request, queryset=None):
    """
    Shared logic to filter, paginate, and prepare product list context.
    """
    if queryset is None:
        queryset = Product.objects.select_related('category').all().order_by('name')

    # Filters
    search = request.GET.get('search', '').strip()
    category_id = request.GET.get('category', '')
    is_active = request.GET.get('is_active', '')
    page_number = request.GET.get('page', 1)

    if search:
        queryset = queryset.filter(
            Q(name__icontains=search) |
            Q(hsn_code__icontains=search)
        )
    if category_id:
        queryset = queryset.filter(category_id=category_id)
    if is_active != '':
        is_active_bool = is_active.lower() == 'true'
        queryset = queryset.filter(is_active=is_active_bool)

    # Pagination (20 per page)
    paginator = Paginator(queryset, 20)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # All categories for filter dropdown
    categories = ProductCategory.objects.all().order_by('name')

    context = {
        'products': page_obj.object_list,
        'page_obj': page_obj,
        'categories': categories,
        'search': search,
        'selected_category_id': category_id,
        'is_active_filter': is_active,
    }
    return context


# ============================================================
# 1. PRODUCT LIST (WITH PAGINATION & FILTERS)
# ============================================================
@handle_errors(default_redirect='accounting:product_list')
def product_list(request):
    """List products with search, category filters, and pagination."""
    context = get_paginated_products_context(request)
    if is_htmx(request):
        return render(request, 'products/partials/product_table.html', context)
    return render(request, 'products/product_list.html', context)


# ============================================================
# 2. PRODUCT CREATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:product_list', htmx_template='products/product_form.html')
def product_create(request):
    """Create a new product via HTMX modal."""
    if request.method == 'POST':
        form = ProductForm(request.POST)
        if form.is_valid():
            product = form.save()
            logger.info(f"Product '{product.name}' created by {request.user.username}")

            if is_htmx(request):
                context = get_paginated_products_context(request)
                return htmx_response(
                    request,
                    'products/partials/product_table.html',
                    context=context,
                    toast={'level': 'success', 'message': f'Product "{product.name}" created successfully.'},
                    close_modal=True
                )
            messages.success(request, f"Product '{product.name}' created successfully.")
            return redirect_to_staff('product_list')
        else:
            if is_htmx(request):
                # Keep form in modal on error
                response = render(request, 'products/product_form.html', {'form': form})
                response['HX-Retarget'] = '#mainModalContent'
                return response
    else:
        form = ProductForm()
    return render(request, 'products/product_form.html', {'form': form})


# ============================================================
# 3. PRODUCT UPDATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:product_list', htmx_template='products/product_form.html')
def product_update(request, pk):
    """Update an existing product."""
    product = get_object_or_404(Product, pk=pk)

    if request.method == 'POST':
        form = ProductForm(request.POST, instance=product)
        if form.is_valid():
            form.save()
            logger.info(f"Product '{product.name}' updated by {request.user.username}")

            if is_htmx(request):
                context = get_paginated_products_context(request)
                return htmx_response(
                    request,
                    'products/partials/product_table.html',
                    context=context,
                    toast={'level': 'success', 'message': f'Product "{product.name}" updated successfully.'},
                    close_modal=True
                )
            messages.success(request, f"Product '{product.name}' updated successfully.")
            return redirect_to_staff('product_list')
        else:
            if is_htmx(request):
                response = render(request, 'products/product_form.html', {'form': form, 'product': product})
                response['HX-Retarget'] = '#mainModalContent'
                return response
    else:
        form = ProductForm(instance=product)
    return render(request, 'products/product_form.html', {'form': form, 'product': product})


# ============================================================
# 4. PRODUCT DELETE
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:product_list')
def product_delete(request, pk):
    """Delete a product only if it has no transaction history."""
    product = get_object_or_404(Product, pk=pk)

    # Check if product is referenced in any transaction
    if (product.invoiceitem_set.exists() or
        product.purchaseitem_set.exists() or
        product.repairpart_set.exists()):
        return toast_only_response(
            {'level': 'danger', 'message': 'Product has transactions, cannot delete.'},
            status=400
        )

    product_name = product.name
    product.delete()
    logger.info(f"Product '{product_name}' deleted by {request.user.username}")

    context = get_paginated_products_context(request)
    return htmx_response(
        request,
        'products/partials/product_table.html',
        context=context,
        toast={'level': 'success', 'message': f'Product "{product_name}" deleted successfully.'}
    )


# ============================================================
# 5. PRODUCT DETAIL MODAL
# ============================================================
def product_detail_modal(request, pk):
    """Show product details with recent stock movements."""
    product = get_object_or_404(Product, pk=pk)
    recent_movements = StockMovement.objects.filter(product=product).order_by('-date')[:10]
    return render(request, 'products/partials/product_detail_modal.html', {
        'product': product,
        'recent_movements': recent_movements,
    })


# ============================================================
# 6. INLINE CATEGORY CREATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:product_list')
def add_category_inline(request):
    """Add a new category inline without leaving the page."""
    if request.method == 'POST':
        form = ProductCategoryForm(request.POST)
        if form.is_valid():
            new_cat = form.save()
            logger.info(f"Category '{new_cat.name}' created by {request.user.username}")
            # Return only the updated dropdown HTML
            categories = ProductCategory.objects.all().order_by('name')
            html = render_to_string('products/partials/category_dropdown.html', {
                'categories': categories,
                'selected_category_id': new_cat.id,
            }, request=request)
            response = HttpResponse(html)
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'success', 'message': f'Category "{new_cat.name}" added.'},
                'closeModal': ''
            })
            return response
        else:
            if is_htmx(request):
                return render(request, 'products/partials/category_inline_form.html', {'form': form})
    else:
        form = ProductCategoryForm()
        return render(request, 'products/partials/category_inline_form.html', {'form': form})


# ============================================================
# 7. REAL-TIME FIELD VALIDATION (HTMX)
# ============================================================
def validate_product_field(request):
    """
    HTMX endpoint: validate a single product field.
    Returns error HTML with d-block class so it's visible.
    """
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    value = request.GET.get(field_name, '')
    product_id = request.GET.get('product_id')
    errors = []

    # Common validation
    if field_name == 'name':
        if not value:
            errors.append("Product name is required.")
        else:
            qs = Product.objects.filter(name__iexact=value)
            if product_id:
                qs = qs.exclude(pk=product_id)
            if qs.exists():
                errors.append("A product with this name already exists.")

    elif field_name == 'hsn_code':
        if value:
            # Basic HSN format check (4 or 8 digits)
            if not value.isdigit() or len(value) not in [4, 6, 8]:
                errors.append("HSN code should be 4, 6, or 8 digits.")

    elif field_name == 'purchase_price':
        if not value:
            errors.append("Purchase price is required.")
        else:
            try:
                price = Decimal(value)
                if price < 0:
                    errors.append("Purchase price cannot be negative.")
            except:
                errors.append("Enter a valid number.")

    elif field_name == 'selling_price':
        if not value:
            errors.append("Selling price is required.")
        else:
            try:
                price = Decimal(value)
                if price < 0:
                    errors.append("Selling price cannot be negative.")
                # Check against purchase price (if available)
                purchase_price = request.GET.get('purchase_price')
                if purchase_price:
                    try:
                        pp = Decimal(purchase_price)
                        if price < pp:
                            errors.append("Selling price should not be less than purchase price.")
                    except:
                        pass
            except:
                errors.append("Enter a valid number.")

    elif field_name == 'tax_rate':
        if value:
            try:
                rate = Decimal(value)
                if rate < 0 or rate > 100:
                    errors.append("Tax rate must be between 0 and 100.")
            except:
                errors.append("Enter a valid tax rate.")

    elif field_name == 'current_stock':
        if value:
            try:
                stock = Decimal(value)
                if stock < 0:
                    errors.append("Stock cannot be negative.")
                # Check if service item has stock
                is_service = request.GET.get('is_service')
                if is_service == 'on' and stock > 0:
                    errors.append("Service items cannot have physical stock.")
            except:
                errors.append("Enter a valid number.")

    elif field_name == 'low_stock_threshold':
        if value:
            try:
                threshold = int(value)
                if threshold < 0:
                    errors.append("Threshold cannot be negative.")
            except:
                errors.append("Enter a valid integer.")

    # Return HTML with d-block for visibility
    if errors:
        error_html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
        for err in errors:
            error_html += f'<div>{err}</div>'
        error_html += '</div>'
        return HttpResponse(error_html)
    else:
        return HttpResponse(f'<div id="field-{field_name}" class="invalid-feedback"></div>')


# ============================================================
# 8. GET PRODUCT PRICE (SINGLE DEFINITION - FOR REPAIRS/PURCHASES)
# ============================================================
@login_required
def get_product_price(request):
    """
    AJAX/HTMX endpoint: Return product details (price, tax, name).
    Used in Invoice, Purchase, and Repair forms.
    """
    product_id = request.GET.get('product_id') or request.GET.get('id')
    if not product_id:
        return JsonResponse({'error': 'No product ID provided'}, status=400)

    try:
        product = Product.objects.get(pk=product_id)
        return JsonResponse({
            'selling_price': float(product.selling_price),
            'purchase_price': float(product.purchase_price),
            'tax_rate': float(product.tax_rate),
            'name': product.name,
            'unit': product.unit,
            'is_service': product.is_service,
            'current_stock': float(product.current_stock),
        })
    except Product.DoesNotExist:
        return JsonResponse({'error': 'Product not found'}, status=404)
    except Exception as e:
        logger.error(f"Error fetching product price for {product_id}: {e}")
        return JsonResponse({'error': 'Server error'}, status=500)


# ============================================================
# 9. STOCK MOVEMENT HISTORY
# ============================================================
def product_stock_history(request, pk):
    """Display stock movement history for a product."""
    product = get_object_or_404(Product, pk=pk)
    movements = StockMovement.objects.filter(product=product).order_by('-date')[:50]
    if is_htmx(request):
        return render(request, 'products/partials/stock_history.html', {'movements': movements, 'product': product})
    return render(request, 'products/stock_history.html', {'movements': movements, 'product': product})


# ============================================================
# 10. PRODUCT SEARCH (LIVE AUTOCOMPLETE)
# ============================================================
@login_required
def product_search(request):
    """
    Live product search for autocomplete in purchase forms.
    Searches by name AND HSN code.
    """
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return render(request, 'purchases/partials/product_suggestions.html', {'products': []})

    products = Product.objects.filter(
        Q(name__icontains=q) |
        Q(hsn_code__icontains=q),
        is_active=True
    )[:10]

    return render(request, 'purchases/partials/product_suggestions.html', {'products': products})


# ============================================================
# 11. QUICK ADD PRODUCT (MODAL) – FOR PURCHASE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:product_list')
def product_quick_add(request):
    """Quick add product via HTMX modal."""
    if request.method == 'POST':
        form = ProductForm(request.POST)
        if form.is_valid():
            product = form.save()
            # Return success with product data
            return render(request, 'products/partials/product_quick_add_success.html', {
                'product': product
            })
        else:
            # Return form with errors
            return render(request, 'products/partials/product_quick_add_form.html', {
                'form': form
            })
    else:
        form = ProductForm()
        return render(request, 'products/partials/product_quick_add_form.html', {
            'form': form
        })


# ============================================================
# 12. EXPORT PRODUCTS TO EXCEL (PROFESSIONAL)
# ============================================================
@login_required
@require_http_methods(["GET"])
def export_products_excel(request):
    """
    Export all products to a professional styled Excel (.xlsx) file.
    Features: Colors, Borders, Number Format, Auto-width, Freeze Panes.
    """
    products = Product.objects.select_related('category').all().order_by('name')

    wb = Workbook()
    ws = wb.active
    ws.title = "Products List"

    # ---------- STYLES ----------
    # Header Style (Dark Blue)
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )
    center_align = Alignment(horizontal='center', vertical='center')
    left_align = Alignment(horizontal='left', vertical='center')
    right_align = Alignment(horizontal='right', vertical='center')
    money_format = numbers.FORMAT_NUMBER_COMMA_SEPARATED1

    # ---------- TITLE ----------
    ws.merge_cells('A1:K1')
    title_cell = ws.cell(row=1, column=1, value="📦 Product Inventory – A1 Computer Solutions")
    title_cell.font = Font(bold=True, size=14, color="1F4E78")
    title_cell.alignment = center_align
    ws.row_dimensions[1].height = 30

    # ---------- HEADERS (Row 2) ----------
    headers = [
        'ID', 'Name', 'HSN Code', 'Category', 'Unit', 
        'Purchase Price', 'Selling Price', 'Tax Rate (%)', 
        'Current Stock', 'Low Stock Threshold', 'Type', 'Active'
    ]

    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = center_align
        ws.row_dimensions[2].height = 25

    # ---------- DATA ROWS ----------
    for idx, product in enumerate(products, start=3):
        row_num = idx
        # Alternate row colors
        if idx % 2 == 0:
            row_fill = PatternFill(start_color="F2F6FC", end_color="F2F6FC", fill_type="solid")
        else:
            row_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

        row_data = [
            product.id,
            product.name,
            product.hsn_code or '',
            product.category.name if product.category else '',
            product.get_unit_display(),
            float(product.purchase_price),
            float(product.selling_price),
            float(product.tax_rate),
            float(product.current_stock),
            product.low_stock_threshold,
            'Service' if product.is_service else 'Product',
            'Yes' if product.is_active else 'No'
        ]

        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_num, column=col_idx, value=value)
            cell.border = thin_border
            cell.fill = row_fill

            # Alignments
            if col_idx in (1, 5, 8, 9, 10, 11, 12):  # ID, Unit, Tax, Stock, Threshold, Type, Active
                cell.alignment = center_align
            elif col_idx in (6, 7):  # Price columns -> Right aligned with currency
                cell.alignment = right_align
                cell.number_format = money_format
            else:
                cell.alignment = left_align

    # ---------- AUTO-WIDTH ----------
    for col in ws.columns:
        max_length = 0
        column_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except:
                pass
        adjusted_width = max(min(max_length + 3, 50), 12)
        ws.column_dimensions[column_letter].width = adjusted_width

    # ---------- FREEZE HEADER ----------
    ws.freeze_panes = 'A3'

    # ---------- RESPONSE ----------
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = 'attachment; filename="products.xlsx"'
    wb.save(response)
    return response