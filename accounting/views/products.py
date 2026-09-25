# accounting/views/products.py
import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.template.loader import render_to_string
from django.db.models import Q, Sum, Count, F
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.views.decorators.csrf import csrf_protect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.utils import timezone

from ..models import Product, ProductCategory, StockMovement, CompanyProfile
from ..forms import ProductForm, ProductCategoryForm
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
# HELPER: Paginated products context with stats
# ============================================================
def get_paginated_products_context(request, queryset=None):
    if queryset is None:
        queryset = Product.objects.select_related('category').all()

    search = request.GET.get('search', '').strip()
    category_id = request.GET.get('category', '')
    is_active = request.GET.get('is_active', '')
    stock_filter = request.GET.get('stock_filter', '') 
    page_number = request.GET.get('page', 1)

    filtered_qs = queryset.all()
    if search:
        filtered_qs = filtered_qs.filter(
            Q(name__icontains=search) | Q(hsn_code__icontains=search)
        )
    if category_id:
        filtered_qs = filtered_qs.filter(category_id=category_id)
    if is_active != '':
        filtered_qs = filtered_qs.filter(is_active=(is_active.lower() == 'true'))
    if stock_filter == 'low':
        filtered_qs = filtered_qs.filter(
            is_service=False, current_stock__lte=F('low_stock_threshold')
        )
    elif stock_filter == 'out':
        filtered_qs = filtered_qs.filter(is_service=False, current_stock=0)
    elif stock_filter == 'ok':
        filtered_qs = filtered_qs.filter(
            is_service=False, current_stock__gt=F('low_stock_threshold')
        )

    # Stats from filtered queryset
    total_products = filtered_qs.count()
    low_stock_count = filtered_qs.filter(
        is_service=False, current_stock__lte=F('low_stock_threshold')
    ).count()
    service_count = filtered_qs.filter(is_service=True).count()
    active_count = filtered_qs.filter(is_active=True).count()

    # Total stock value (physical only)
    stock_value_data = filtered_qs.filter(is_service=False).aggregate(
        total_value=Sum(F('current_stock') * F('purchase_price'))
    )
    total_stock_value = stock_value_data.get('total_value') or Decimal('0')

    # Pagination
    paginator = Paginator(filtered_qs.order_by('name'), 20)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    categories = ProductCategory.objects.all().order_by('name')

    return {
        'products': page_obj.object_list,
        'page_obj': page_obj,
        'categories': categories,
        'search': search,
        'selected_category_id': category_id,
        'is_active_filter': is_active,
        'stock_filter': stock_filter,
        'total_products': total_products,
        'low_stock_count': low_stock_count,
        'service_count': service_count,
        'active_count': active_count,
        'total_stock_value': total_stock_value,
        'is_htmx': is_htmx(request),
    }


# ============================================================
# 1. PRODUCT LIST
# ============================================================
@login_required
@handle_errors(default_redirect='accounting:product_list')
def product_list(request):
    if request.GET.get('reset'):
        return redirect('accounting:product_list')

    context = get_paginated_products_context(request)
    if is_htmx(request):
        return render(request, 'products/partials/product_table.html', context)
    return render(request, 'products/product_list.html', context)


# ============================================================
# 1b. PRODUCT LIST — PRINT
# ============================================================
@login_required
@handle_errors(default_redirect='accounting:product_list')
def product_list_print(request):
    """Print-friendly products list with current filters."""
    context = get_paginated_products_context(request)
    company = CompanyProfile.get_instance()

    # Get all (no pagination) — reuse filtered qs
    qs = Product.objects.select_related('category').all()
    search = request.GET.get('search', '').strip()
    category_id = request.GET.get('category', '')
    is_active = request.GET.get('is_active', '')
    stock_filter = request.GET.get('stock_filter', '')

    # Resolve category name for display
    category_name = None
    if category_id:
        try:
            category_name = ProductCategory.objects.get(pk=category_id).name
        except (ProductCategory.DoesNotExist, ValueError, TypeError):
            pass

    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(hsn_code__icontains=search))
    if category_id:
        qs = qs.filter(category_id=category_id)
    if is_active != '':
        qs = qs.filter(is_active=(is_active.lower() == 'true'))
    if stock_filter == 'low':
        qs = qs.filter(is_service=False, current_stock__lte=F('low_stock_threshold'))
    elif stock_filter == 'out':
        qs = qs.filter(is_service=False, current_stock=0)
    elif stock_filter == 'ok':
        qs = qs.filter(is_service=False, current_stock__gt=F('low_stock_threshold'))

    return render(request, 'products/product_list_print.html', {
        **context,
        'products': qs.order_by('name'),
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
        'category_name': category_name,
    })


# ============================================================
# 2. PRODUCT CREATE
# ============================================================
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:product_list',
               htmx_template='products/product_form.html')
def product_create(request):
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
                    toast={'level': 'success', 'message': f'Product "{product.name}" created.'},
                    close_modal=True,
                )
            messages.success(request, f"Product '{product.name}' created.")
            return redirect_to_staff('product_list')
        else:
            if is_htmx(request):
                # Validation errors: re-render form INSIDE the modal
                return htmx_response(
                    request,
                    'products/product_form.html',
                    context={'form': form, 'product': None},
                    extra_headers={'HX-Retarget': '#mainModalContent'},
                )
    else:
        form = ProductForm()
    return render(request, 'products/product_form.html', {'form': form})


# ============================================================
# 3. PRODUCT UPDATE
# ============================================================
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:product_list',
               htmx_template='products/product_form.html')
def product_update(request, pk):
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
                    toast={'level': 'success', 'message': f'Product "{product.name}" updated.'},
                    close_modal=True,
                )
            messages.success(request, f"Product '{product.name}' updated.")
            return redirect_to_staff('product_list')
        else:
            if is_htmx(request):
                return htmx_response(
                    request,
                    'products/product_form.html',
                    context={'form': form, 'product': product},
                    extra_headers={'HX-Retarget': '#mainModalContent'},
                )
    else:
        form = ProductForm(instance=product)
    return render(request, 'products/product_form.html', {'form': form, 'product': product})


# ============================================================
# 4. PRODUCT DELETE
# ============================================================
@login_required
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:product_list')
def product_delete(request, pk):
    product = get_object_or_404(Product, pk=pk)

    if (product.invoiceitem_set.exists()
            or product.purchaseitem_set.exists()
            or product.repairpart_set.exists()):
        return toast_only_response(
            {'level': 'danger', 'message': 'Product has transactions. Cannot delete.'},
            status=400,
        )

    name = product.name
    product.delete()
    logger.info(f"Product '{name}' deleted by {request.user.username}")

    context = get_paginated_products_context(request)
    return htmx_response(
        request,
        'products/partials/product_table.html',
        context=context,
        toast={'level': 'success', 'message': f'Product "{name}" deleted.'},
        close_modal=True,  
    )


# ============================================================
# 5. PRODUCT DETAIL MODAL
# ============================================================
@login_required
def product_detail_modal(request, pk):
    product = get_object_or_404(Product, pk=pk)
    recent_movements = StockMovement.objects.filter(
        product=product
    ).order_by('-date')[:10]
    return render(request, 'products/partials/product_detail_modal.html', {
        'product': product,
        'recent_movements': recent_movements,
    })


# ============================================================
# 6. CATEGORY FIELD VALIDATION (HTMX)
# ============================================================
@require_http_methods(["GET"])
def validate_category_field(request):
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    value = request.GET.get(field_name, '').strip()
    errors = []

    if field_name == 'name':
        if not value:
            errors.append("Category name is required.")
        elif ProductCategory.objects.filter(name__iexact=value).exists():
            errors.append("A category with this name already exists.")

    if errors:
        html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
        for err in errors:
            html += f'<div><i class="bi bi-exclamation-circle me-1"></i>{err}</div>'
        html += '</div>'
        return HttpResponse(html)
    return HttpResponse(f'<div id="field-{field_name}" class="invalid-feedback"></div>')


# ============================================================
# 7. INLINE CATEGORY ADD (with Cancel support)
# ============================================================
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:product_list')
def add_category_inline(request):
    if request.method == 'POST':
        form = ProductCategoryForm(request.POST)
        if form.is_valid():
            new_cat = form.save()
            categories = ProductCategory.objects.all().order_by('name')
            html = render_to_string('products/partials/category_dropdown.html', {
                'categories': categories,
                'selected_category_id': str(new_cat.id),
            }, request=request)
            response = HttpResponse(html)
            response['HX-Trigger'] = json.dumps({
                'showToast': {
                    'level': 'success',
                    'message': f'Category "{new_cat.name}" added!',
                },
            })
            return response
        else:
            return render(request, 'products/partials/category_inline_form.html', {'form': form})

    # GET
    cancel = request.GET.get('cancel')
    if cancel == '1':
        # Preserve any previously selected category
        current_id = request.GET.get('current', '').strip()
        categories = ProductCategory.objects.all().order_by('name')
        return render(request, 'products/partials/category_dropdown.html', {
            'categories': categories,
            'selected_category_id': current_id,
        })

    form = ProductCategoryForm()
    return render(request, 'products/partials/category_inline_form.html', {'form': form})


# ============================================================
# 8. REAL-TIME FIELD VALIDATION (HTMX)
# ============================================================
@require_http_methods(["GET"])
def validate_product_field(request):
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    value = request.GET.get(field_name, '')
    product_id = request.GET.get('product_id')
    errors = []

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
        if value and (not value.isdigit() or len(value) not in (4, 6, 8)):
            errors.append("HSN code should be 4, 6, or 8 digits.")

    elif field_name == 'purchase_price':
        if not value:
            errors.append("Purchase price is required.")
        else:
            try:
                price = Decimal(value)
                if price < 0:
                    errors.append("Purchase price cannot be negative.")
            except (ValueError, TypeError):
                errors.append("Enter a valid number.")

    elif field_name == 'selling_price':
        if not value:
            errors.append("Selling price is required.")
        else:
            try:
                price = Decimal(value)
                if price < 0:
                    errors.append("Selling price cannot be negative.")
                pp = request.GET.get('purchase_price')
                if pp:
                    try:
                        if price < Decimal(pp):
                            errors.append("Selling price should not be less than purchase price.")
                    except (ValueError, TypeError):
                        pass
            except (ValueError, TypeError):
                errors.append("Enter a valid number.")

    elif field_name == 'tax_rate':
        if value:
            try:
                rate = Decimal(value)
                if rate < 0 or rate > 100:
                    errors.append("Tax rate must be between 0 and 100.")
            except (ValueError, TypeError):
                errors.append("Enter a valid tax rate.")

    elif field_name == 'current_stock':
        if value:
            try:
                stock = Decimal(value)
                if stock < 0:
                    errors.append("Stock cannot be negative.")
                if request.GET.get('is_service') == 'on' and stock > 0:
                    errors.append("Service items cannot have physical stock.")
            except (ValueError, TypeError):
                errors.append("Enter a valid number.")

    elif field_name == 'low_stock_threshold':
        if value:
            try:
                threshold = int(value)
                if threshold < 0:
                    errors.append("Threshold cannot be negative.")
            except (ValueError, TypeError):
                errors.append("Enter a valid integer.")

    if errors:
        html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
        for err in errors:
            html += f'<div><i class="bi bi-exclamation-circle me-1"></i>{err}</div>'
        html += '</div>'
        return HttpResponse(html)
    return HttpResponse(f'<div id="field-{field_name}" class="invalid-feedback"></div>')


# ============================================================
# 9. GET PRODUCT PRICE (JSON API)
# ============================================================
@login_required
def get_product_price(request):
    product_id = request.GET.get('product_id') or request.GET.get('id')
    if not product_id:
        return JsonResponse({'error': 'No product ID'}, status=400)
    try:
        product = Product.objects.get(pk=product_id)
        return JsonResponse({
            'id': product.id,
            'name': product.name,
            'selling_price': float(product.selling_price),
            'purchase_price': float(product.purchase_price),
            'tax_rate': float(product.tax_rate),
            'unit': product.unit,
            'unit_display': product.get_unit_display(),
            'is_service': product.is_service,
            'current_stock': float(product.current_stock),
        })
    except Product.DoesNotExist:
        return JsonResponse({'error': 'Product not found'}, status=404)
    except Exception as e:
        logger.error(f"Error fetching product price: {e}")
        return JsonResponse({'error': 'Server error'}, status=500)


# ============================================================
# 10. STOCK HISTORY (fixed template path)
# ============================================================
@login_required
def product_stock_history(request, pk):
    product = get_object_or_404(Product, pk=pk)

    page_number = request.GET.get('page', 1)
    movements_qs = StockMovement.objects.filter(product=product).order_by('-date')

    paginator = Paginator(movements_qs, 50)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    context = {
        'product': product,
        'movements': page_obj.object_list,
        'page_obj': page_obj,
        'total_count': paginator.count,
    }

    if is_htmx(request):
        return render(request, 'products/partials/stock_history.html', context)
    return render(request, 'products/partials/stock_history.html', context)


# ============================================================
# 11. PRODUCT SEARCH — autocomplete (fixed template)
# ============================================================
@login_required
def product_search(request):
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return render(request, 'products/partials/product_suggestions.html', {'products': []})
    products = Product.objects.filter(
        Q(name__icontains=q) | Q(hsn_code__icontains=q),
        is_active=True,
    )[:10]
    return render(request, 'products/partials/product_suggestions.html', {'products': products})


# ============================================================
# 12. QUICK ADD PRODUCT
# ============================================================
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:product_list')
def product_quick_add(request):
    if request.method == 'POST':
        form = ProductForm(request.POST)
        if form.is_valid():
            product = form.save()
            return render(request, 'products/partials/product_quick_add_success.html', {
                'product': product,
            })
        else:
            return render(request, 'products/partials/product_quick_add_form.html', {'form': form})
    form = ProductForm()
    return render(request, 'products/partials/product_quick_add_form.html', {'form': form})


# ============================================================
# 13. EXPORT PRODUCTS TO EXCEL (filter-aware)
# ============================================================
@login_required
@require_http_methods(["GET"])
@handle_errors(default_redirect='accounting:product_list')
def export_products_excel(request):
    if Workbook is None:
        messages.error(request, "Openpyxl library is not installed on the server.")
        return redirect('accounting:product_list')

    search = request.GET.get('search', '').strip()
    category_id = request.GET.get('category', '')
    is_active = request.GET.get('is_active', '')
    stock_filter = request.GET.get('stock_filter', '')

    products = Product.objects.select_related('category').all().order_by('name')
    if search:
        products = products.filter(Q(name__icontains=search) | Q(hsn_code__icontains=search))
    if category_id:
        products = products.filter(category_id=category_id)
    if is_active != '':
        products = products.filter(is_active=(is_active.lower() == 'true'))
    if stock_filter == 'low':
        products = products.filter(is_service=False, current_stock__lte=F('low_stock_threshold'))
    elif stock_filter == 'out':
        products = products.filter(is_service=False, current_stock=0)
    elif stock_filter == 'ok':
        products = products.filter(is_service=False, current_stock__gt=F('low_stock_threshold'))

    company = CompanyProfile.get_instance()
    wb = Workbook()
    ws = wb.active
    ws.title = "Products"

    title_font = Font(bold=True, size=16, color="FFFFFF")
    title_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    subtitle_font = Font(bold=True, size=12, color="FFFFFF")
    subtitle_fill = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
    header_font = Font(bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
    total_font = Font(bold=True, size=11, color="FFFFFF")
    total_fill = PatternFill(start_color="375623", end_color="375623", fill_type="solid")

    thin = Side(style='thin', color="999999")
    medium = Side(style='medium', color="000000")
    border_all = Border(left=thin, right=thin, top=thin, bottom=thin)
    border_header = Border(left=medium, right=medium, top=medium, bottom=medium)
    money_format = '#,##0.00'
    number_format = '#,##0.###'
    center_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left_align = Alignment(horizontal='left', vertical='center', wrap_text=True)
    right_align = Alignment(horizontal='right', vertical='center')

    TOTAL_COLS = 14
    last_col = get_column_letter(TOTAL_COLS)

    ws.merge_cells(f'A1:{last_col}1')
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = title_font
    ws['A1'].fill = title_fill
    ws['A1'].alignment = center_align
    ws.row_dimensions[1].height = 30

    ws.merge_cells(f'A2:{last_col}2')
    parts = []
    if company.address: parts.append(company.address)
    if company.phone: parts.append(f"Phone: {company.phone}")
    if company.email: parts.append(f"Email: {company.email}")
    if company.gstin: parts.append(f"GSTIN: {company.gstin}")
    ws['A2'] = " | ".join(parts)
    ws['A2'].font = Font(size=10, italic=True)
    ws['A2'].alignment = center_align
    ws.row_dimensions[2].height = 20

    ws.merge_cells(f'A3:{last_col}3')
    ws['A3'] = "PRODUCT INVENTORY REPORT"
    ws['A3'].font = subtitle_font
    ws['A3'].fill = subtitle_fill
    ws['A3'].alignment = center_align
    ws.row_dimensions[3].height = 25

    ws.merge_cells(f'A4:{last_col}4')
    fparts = [f"Generated: {timezone.now().strftime('%d-%m-%Y %H:%M')}"]
    if search: fparts.append(f"Search: {search}")
    if category_id:
        try:
            cat = ProductCategory.objects.get(pk=category_id)
            fparts.append(f"Category: {cat.name}")
        except ProductCategory.DoesNotExist:
            pass
    if is_active == 'true': fparts.append("Status: Active Only")
    elif is_active == 'false': fparts.append("Status: Inactive Only")
    if stock_filter: fparts.append(f"Stock: {stock_filter}")
    fparts.append(f"Total Records: {products.count()}")
    ws['A4'] = " | ".join(fparts)
    ws['A4'].font = Font(size=10, italic=True, color="555555")
    ws['A4'].alignment = center_align
    ws.row_dimensions[4].height = 20
    ws.row_dimensions[5].height = 5

    headers = [
        ('#', 6), ('Product Name', 32), ('HSN/SAC', 12), ('Category', 20),
        ('Unit', 10), ('Type', 10), ('Purchase Price (₹)', 16),
        ('Selling Price (₹)', 16), ('Tax Rate (%)', 12), ('Current Stock', 14),
        ('Low Stock Alert', 14), ('Stock Status', 14),
        ('Stock Value (₹)', 16), ('Active', 10),
    ]
    for col_num, (header, width) in enumerate(headers, 1):
        cell = ws.cell(row=6, column=col_num, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = border_header
        ws.column_dimensions[get_column_letter(col_num)].width = width
    ws.row_dimensions[6].height = 30

    row_num = 7
    total_stock_value = Decimal('0')
    total_physical_stock = Decimal('0')
    low_stock_rows = 0

    for idx, product in enumerate(products, 1):
        if product.is_service:
            stock_status = "Service"
            stock_value = Decimal('0')
        else:
            stock_value = (product.current_stock or Decimal('0')) * (product.purchase_price or Decimal('0'))
            total_stock_value += stock_value
            total_physical_stock += (product.current_stock or Decimal('0'))
            if product.current_stock <= product.low_stock_threshold:
                stock_status = "LOW"
                low_stock_rows += 1
            else:
                stock_status = "OK"

        row_data = [
            idx,
            product.name or '',
            product.hsn_code or '',
            product.category.name if product.category else '',
            product.get_unit_display(),
            'Service' if product.is_service else 'Product',
            float(product.purchase_price or 0),
            float(product.selling_price or 0),
            float(product.tax_rate or 0),
            float(product.current_stock or 0) if not product.is_service else 0,
            product.low_stock_threshold if not product.is_service else 0,
            stock_status,
            float(stock_value),
            'Yes' if product.is_active else 'No',
        ]

        for col_num, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_num, column=col_num, value=value)
            cell.border = border_all
            cell.alignment = left_align

        for col_idx in (7, 8, 13):
            ws.cell(row=row_num, column=col_idx).alignment = right_align
            ws.cell(row=row_num, column=col_idx).number_format = money_format
        for col_idx in (9, 10, 11):
            ws.cell(row=row_num, column=col_idx).alignment = center_align
            ws.cell(row=row_num, column=col_idx).number_format = number_format
        for col_idx in (1, 3, 5, 6, 12, 14):
            ws.cell(row=row_num, column=col_idx).alignment = center_align

        status_cell = ws.cell(row=row_num, column=12)
        if stock_status == "LOW":
            status_cell.font = Font(bold=True, color="C00000")
        elif stock_status == "OK":
            status_cell.font = Font(color="1F7A1F")
        else:
            status_cell.font = Font(color="666666", italic=True)

        if not product.is_active:
            grey = PatternFill(start_color="F0F0F0", end_color="F0F0F0", fill_type="solid")
            for c in range(1, TOTAL_COLS + 1):
                ws.cell(row=row_num, column=c).fill = grey
        elif row_num % 2 == 0:
            alt = PatternFill(start_color="F2F6FC", end_color="F2F6FC", fill_type="solid")
            for c in range(1, TOTAL_COLS + 1):
                ws.cell(row=row_num, column=c).fill = alt

        row_num += 1

    # Grand total row
    ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=9)
    tl = ws.cell(row=row_num, column=1, value="GRAND TOTAL")
    tl.font = total_font
    tl.fill = total_fill
    tl.alignment = right_align
    tl.border = border_header
    for col_idx in range(2, 10):
        c = ws.cell(row=row_num, column=col_idx)
        c.fill = total_fill
        c.border = border_header

    c = ws.cell(row=row_num, column=10, value=float(total_physical_stock))
    c.font = total_font
    c.fill = total_fill
    c.alignment = center_align
    c.number_format = number_format
    c.border = border_header

    c = ws.cell(row=row_num, column=11, value=f"{low_stock_rows} LOW")
    c.font = total_font
    c.fill = total_fill
    c.alignment = center_align
    c.border = border_header

    c = ws.cell(row=row_num, column=12, value="")
    c.fill = total_fill
    c.border = border_header

    c = ws.cell(row=row_num, column=13, value=float(total_stock_value))
    c.font = total_font
    c.fill = total_fill
    c.alignment = right_align
    c.number_format = money_format
    c.border = border_header

    c = ws.cell(row=row_num, column=14, value="")
    c.fill = total_fill
    c.border = border_header

    ws.row_dimensions[row_num].height = 24

    footer_row = row_num + 2
    ws.merge_cells(start_row=footer_row, start_column=1, end_row=footer_row, end_column=TOTAL_COLS)
    txt = (f"Auto-generated by {company.name or 'A1 Computer Solutions'} on "
           f"{timezone.now().strftime('%d-%m-%Y %H:%M')}. "
           f"Total Products: {products.count()} | "
           f"Total Stock Value: ₹{total_stock_value:,.2f}")
    fc = ws.cell(row=footer_row, column=1, value=txt)
    fc.font = Font(size=9, italic=True, color="777777")
    fc.alignment = center_align

    ws.freeze_panes = 'A7'
    ws.auto_filter.ref = f"A6:{last_col}6"
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"products_{timezone.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


# ============================================================
# 14. CATEGORY MANAGEMENT — Helper
# ============================================================
def get_category_table_context(request):
    """Shared context for category table (used by list + CRUD)."""
    search = request.GET.get('search', '').strip()

    categories_qs = ProductCategory.objects.annotate(
        product_count=Count(
            'products',
            filter=Q(products__is_deleted=False)
        )
    ).order_by('name')

    if search:
        categories_qs = categories_qs.filter(
            Q(name__icontains=search) | Q(description__icontains=search)
        )

    categories_list = list(categories_qs)

    return {
        'categories': categories_list,
        'search': search,
        'total_categories': len(categories_list),
        'categories_in_use': sum(1 for c in categories_list if c.product_count > 0),
        'empty_categories': sum(1 for c in categories_list if c.product_count == 0),
        'is_htmx': is_htmx(request),
    }


# ============================================================
# 15. CATEGORY LIST
# ============================================================
@login_required
@handle_errors(default_redirect='accounting:product_list')
def category_list(request):
    context = get_category_table_context(request)
    if is_htmx(request):
        return render(request, 'products/partials/category_table.html', context)
    return render(request, 'products/category_list.html', context)


# ============================================================
# 16. CATEGORY CREATE (HTMX modal)
# ============================================================
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:category_list')
def category_create(request):
    if request.method == 'POST':
        form = ProductCategoryForm(request.POST)
        if form.is_valid():
            cat = form.save()
            logger.info(
                f"Category '{cat.name}' created by {request.user.username}"
            )
            if is_htmx(request):
                context = get_category_table_context(request)
                return htmx_response(
                    request,
                    'products/partials/category_table.html',
                    context=context,
                    toast={
                        'level': 'success',
                        'message': f'Category "{cat.name}" created.'
                    },
                    close_modal=True,
                )
            messages.success(request, f'Category "{cat.name}" created.')
            return redirect_to_staff('category_list')
        else:
            if is_htmx(request):
                return htmx_response(
                    request,
                    'products/partials/category_form.html',
                    context={'form': form, 'category': None},
                    extra_headers={'HX-Retarget': '#mainModalContent'},
                )
    else:
        form = ProductCategoryForm()

    return render(
        request,
        'products/partials/category_form.html',
        {'form': form, 'category': None}
    )


# ============================================================
# 17. CATEGORY UPDATE (HTMX modal)
# ============================================================
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:category_list')
def category_update(request, pk):
    category = get_object_or_404(ProductCategory, pk=pk)

    if request.method == 'POST':
        form = ProductCategoryForm(request.POST, instance=category)
        if form.is_valid():
            form.save()
            logger.info(
                f"Category '{category.name}' updated by {request.user.username}"
            )
            if is_htmx(request):
                context = get_category_table_context(request)
                return htmx_response(
                    request,
                    'products/partials/category_table.html',
                    context=context,
                    toast={
                        'level': 'success',
                        'message': f'Category "{category.name}" updated.'
                    },
                    close_modal=True,
                )
            messages.success(request, f'Category "{category.name}" updated.')
            return redirect_to_staff('category_list')
        else:
            if is_htmx(request):
                return htmx_response(
                    request,
                    'products/partials/category_form.html',
                    context={'form': form, 'category': category},
                    extra_headers={'HX-Retarget': '#mainModalContent'},
                )
    else:
        form = ProductCategoryForm(instance=category)

    return render(
        request,
        'products/partials/category_form.html',
        {'form': form, 'category': category}
    )


# ============================================================
# 18. CATEGORY DELETE (HTMX, with product safety check)
# ============================================================
@login_required
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:category_list')
def category_delete(request, pk):
    category = get_object_or_404(ProductCategory, pk=pk)

    # Safety: block delete if any active product uses this category
    active_products = category.products.filter(is_deleted=False).count()
    if active_products > 0:
        return toast_only_response(
            {
                'level': 'danger',
                'message': (
                    f'Cannot delete "{category.name}" — '
                    f'{active_products} product(s) are using this category. '
                    f'Reassign them first.'
                )
            },
            status=400,
        )

    name = category.name
    category.delete()
    logger.info(
        f"Category '{name}' deleted by {request.user.username}"
    )

    context = get_category_table_context(request)
    return htmx_response(
        request,
        'products/partials/category_table.html',
        context=context,
        toast={'level': 'success', 'message': f'Category "{name}" deleted.'},
    )