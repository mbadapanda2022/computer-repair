# accounting/views/reports.py

import csv
import logging
from decimal import Decimal
from datetime import date, timedelta, datetime

from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.db.models import Sum, Q, F, Count, Avg, Max, Min
from django.contrib import messages
from django.utils import timezone
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger

from ..models import *
from .utils import is_htmx
from ..decorators import handle_errors

logger = logging.getLogger(__name__)

# Excel export
try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
except ImportError:
    openpyxl = None


# ============================================================
# HELPER: Export to Excel
# ============================================================
def export_excel(request, data, headers, filename, title, summary=None):
    """Generic Excel export helper."""
    if openpyxl is None:
        return HttpResponse("Openpyxl not installed", status=400)
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title[:31]
    
    # Styles
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    center_align = Alignment(horizontal='center', vertical='center')
    right_align = Alignment(horizontal='right', vertical='center')
    money_format = '#,##0.00'
    
    company = CompanyProfile.get_instance()
    ws.merge_cells(f'A1:{openpyxl.utils.get_column_letter(len(headers))}1')
    ws['A1'] = company.name
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells(f'A2:{openpyxl.utils.get_column_letter(len(headers))}2')
    ws['A2'] = title
    ws['A2'].font = Font(bold=True, size=12)
    ws.merge_cells(f'A3:{openpyxl.utils.get_column_letter(len(headers))}3')
    ws['A3'] = f"Generated: {datetime.now().strftime('%d-%m-%Y %H:%M')}"
    ws['A3'].alignment = Alignment(horizontal="center")
    
    # Headers
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=5, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border
    
    # Data
    row = 6
    for item in data:
        for col, (key, _) in enumerate(headers.items(), 1):
            val = item.get(key, '')
            if isinstance(val, Decimal):
                ws.cell(row=row, column=col, value=float(val))
                ws.cell(row=row, column=col).number_format = money_format
            elif isinstance(val, date):
                ws.cell(row=row, column=col, value=val.strftime("%d-%m-%Y"))
            else:
                ws.cell(row=row, column=col, value=val)
            ws.cell(row=row, column=col).border = thin_border
        row += 1
    
    # Summary row
    if summary:
        row += 1
        for col, (key, _) in enumerate(headers.items(), 1):
            val = summary.get(key, '')
            if isinstance(val, Decimal):
                ws.cell(row=row, column=col, value=float(val))
                ws.cell(row=row, column=col).number_format = money_format
            else:
                ws.cell(row=row, column=col, value=val)
            ws.cell(row=row, column=col).border = thin_border
            ws.cell(row=row, column=col).font = Font(bold=True)
    
    # Column widths
    for idx, header in enumerate(headers.keys(), 1):
        max_len = max(len(str(h)) for h in headers.keys()) + 10
        ws.column_dimensions[openpyxl.utils.get_column_letter(idx)].width = max_len
    
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
    wb.save(response)
    return response


# ============================================================
# 1. SALES REPORT (Enhanced)
# ============================================================

@handle_errors(default_redirect='accounting:sales_report')
def sales_report(request):
    try:
        date_from = request.GET.get('date_from', '').strip()
        date_to = request.GET.get('date_to', '').strip()
        customer_id = request.GET.get('customer', '').strip()
        is_excel = request.GET.get('excel') == '1'
        is_print = request.GET.get('print') == '1'
        group_by = request.GET.get('group_by', '')
        
        invoices = Invoice.objects.select_related('customer').all().order_by('-date')
        
        if date_from:
            invoices = invoices.filter(date__gte=date_from)
        if date_to:
            invoices = invoices.filter(date__lte=date_to)
        if customer_id:
            invoices = invoices.filter(customer_id=customer_id)
        
        # Totals
        total_grand = invoices.aggregate(total=Sum('grand_total'))['total'] or Decimal('0')
        total_tax = invoices.aggregate(total=Sum('tax_amount'))['total'] or Decimal('0')
        total_discount = invoices.aggregate(total=Sum('discount_amount'))['total'] or Decimal('0')
        total_subtotal = invoices.aggregate(total=Sum('subtotal'))['total'] or Decimal('0')
        
        # Group by month
        monthly_data = []
        if group_by == 'month':
            month_data = {}
            for inv in invoices:
                key = inv.date.strftime("%Y-%m")
                if key not in month_data:
                    month_data[key] = {'count': 0, 'total': Decimal('0')}
                month_data[key]['count'] += 1
                month_data[key]['total'] += inv.grand_total
            monthly_data = [{'month': k, 'count': v['count'], 'total': v['total']} for k, v in sorted(month_data.items())]
        
        # Excel Export
        if is_excel:
            headers = {
                'date': 'Date',
                'invoice_number': 'Invoice #',
                'customer_name': 'Customer',
                'subtotal': 'Subtotal (₹)',
                'discount_amount': 'Discount (₹)',
                'tax_amount': 'Tax (₹)',
                'grand_total': 'Grand Total (₹)',
                'payment_status': 'Status'
            }
            data = []
            for inv in invoices:
                data.append({
                    'date': inv.date,
                    'invoice_number': inv.invoice_number,
                    'customer_name': inv.customer.name,
                    'subtotal': inv.subtotal,
                    'discount_amount': inv.discount_amount,
                    'tax_amount': inv.tax_amount,
                    'grand_total': inv.grand_total,
                    'payment_status': inv.get_payment_status_display()
                })
            summary = {
                'grand_total': total_grand,
                'tax_amount': total_tax,
                'discount_amount': total_discount,
                'subtotal': total_subtotal
            }
            return export_excel(request, data, headers, 'sales_report', 'Sales Report', summary)
        
        # Print Mode
        if is_print:
            company = CompanyProfile.get_instance()
            context = {
                'invoices': invoices,
                'total': total_grand,
                'date_from': date_from,
                'date_to': date_to,
                'company': company,
                'title': 'Sales Report',
            }
            return render(request, 'reports/print_report.html', context)
        
        # Normal view
        paginator = Paginator(invoices, 50)
        page = request.GET.get('page', 1)
        try:
            invoices_page = paginator.page(page)
        except (PageNotAnInteger, EmptyPage):
            invoices_page = paginator.page(1)
        
        context = {
            'invoices': invoices_page,
            'page_obj': invoices_page,
            'total_grand': total_grand,
            'total_tax': total_tax,
            'total_discount': total_discount,
            'total_subtotal': total_subtotal,
            'total_count': paginator.count,
            'date_from': date_from,
            'date_to': date_to,
            'customer_id': customer_id,
            'group_by': group_by,
            'monthly_data': monthly_data,
            'customers': Contact.objects.filter(contact_type__in=['customer', 'both']),
        }
        
        if is_htmx(request):
            return render(request, 'reports/partials/sales_report_table.html', context)
        return render(request, 'reports/sales_report.html', context)
        
    except Exception as e:
        logger.error(f"Sales report error: {e}")
        messages.error(request, "Unable to generate sales report.")
        return render(request, 'reports/sales_report.html', {
            'invoices': [], 'total_grand': 0, 'total_tax': 0,
            'total_discount': 0, 'total_subtotal': 0, 'total_count': 0
        })


# ============================================================
# 2. PURCHASE REPORT (Enhanced)
# ============================================================

@handle_errors(default_redirect='accounting:purchase_report')
def purchase_report(request):
    try:
        date_from = request.GET.get('date_from', '').strip()
        date_to = request.GET.get('date_to', '').strip()
        vendor_id = request.GET.get('vendor', '').strip()
        is_excel = request.GET.get('excel') == '1'
        is_print = request.GET.get('print') == '1'
        
        purchases = Purchase.objects.select_related('vendor').all().order_by('-date')
        
        if date_from:
            purchases = purchases.filter(date__gte=date_from)
        if date_to:
            purchases = purchases.filter(date__lte=date_to)
        if vendor_id:
            purchases = purchases.filter(vendor_id=vendor_id)
        
        total = purchases.aggregate(total=Sum('grand_total'))['total'] or Decimal('0')
        total_tax = purchases.aggregate(total=Sum('tax_amount'))['total'] or Decimal('0')
        total_subtotal = purchases.aggregate(total=Sum('subtotal'))['total'] or Decimal('0')
        
        # Excel Export
        if is_excel:
            headers = {
                'date': 'Date',
                'purchase_number': 'Purchase #',
                'vendor_name': 'Vendor',
                'subtotal': 'Subtotal (₹)',
                'tax_amount': 'Tax (₹)',
                'grand_total': 'Grand Total (₹)',
                'paid': 'Status'
            }
            data = []
            for pur in purchases:
                data.append({
                    'date': pur.date,
                    'purchase_number': pur.purchase_number,
                    'vendor_name': pur.vendor.name,
                    'subtotal': pur.subtotal,
                    'tax_amount': pur.tax_amount,
                    'grand_total': pur.grand_total,
                    'paid': 'Yes' if pur.paid else 'No'
                })
            summary = {'grand_total': total, 'tax_amount': total_tax, 'subtotal': total_subtotal}
            return export_excel(request, data, headers, 'purchase_report', 'Purchase Report', summary)
        
        if is_print:
            company = CompanyProfile.get_instance()
            context = {
                'purchases': purchases,
                'total': total,
                'date_from': date_from,
                'date_to': date_to,
                'company': company,
                'title': 'Purchase Report',
            }
            return render(request, 'reports/print_report.html', context)
        
        paginator = Paginator(purchases, 50)
        page = request.GET.get('page', 1)
        try:
            purchases_page = paginator.page(page)
        except (PageNotAnInteger, EmptyPage):
            purchases_page = paginator.page(1)
        
        context = {
            'purchases': purchases_page,
            'page_obj': purchases_page,
            'total': total,
            'total_tax': total_tax,
            'total_subtotal': total_subtotal,
            'total_count': paginator.count,
            'date_from': date_from,
            'date_to': date_to,
            'vendor_id': vendor_id,
            'vendors': Contact.objects.filter(contact_type__in=['vendor', 'both']),
        }
        
        if is_htmx(request):
            return render(request, 'reports/partials/purchase_report_table.html', context)
        return render(request, 'reports/purchase_report.html', context)
        
    except Exception as e:
        logger.error(f"Purchase report error: {e}")
        messages.error(request, "Unable to generate purchase report.")
        return render(request, 'reports/purchase_report.html', {
            'purchases': [], 'total': 0, 'total_tax': 0, 'total_subtotal': 0
        })


# ============================================================
# 3. GST REPORT (Enhanced)
# ============================================================

@handle_errors(default_redirect='accounting:gst_report')
def gst_report(request):
    try:
        date_from = request.GET.get('date_from', '').strip()
        date_to = request.GET.get('date_to', '').strip()
        is_excel = request.GET.get('excel') == '1'
        is_print = request.GET.get('print') == '1'
        
        invoices = Invoice.objects.filter(gst_type__in=['regular', 'interstate', 'intrastate'])
        purchases = Purchase.objects.filter(gst_type__in=['regular', 'interstate', 'intrastate'])
        
        if date_from:
            invoices = invoices.filter(date__gte=date_from)
            purchases = purchases.filter(date__gte=date_from)
        if date_to:
            invoices = invoices.filter(date__lte=date_to)
            purchases = purchases.filter(date__lte=date_to)
        
        # Sales GST breakdown
        total_sales_tax = invoices.aggregate(Sum('tax_amount'))['tax_amount__sum'] or Decimal('0')
        
        # Detailed GST breakdown
        cgst = Decimal('0')
        sgst = Decimal('0')
        igst = Decimal('0')
        
        for inv in invoices:
            if inv.gst_type == 'intrastate':
                cgst += inv.tax_amount / 2
                sgst += inv.tax_amount / 2
            elif inv.gst_type == 'interstate':
                igst += inv.tax_amount
        
        # Purchase GST (input credit)
        total_purchase_tax = purchases.aggregate(Sum('tax_amount'))['tax_amount__sum'] or Decimal('0')
        
        # Net GST payable
        net_gst = total_sales_tax - total_purchase_tax
        
        if is_excel:
            headers = {
                'gst_type': 'GST Type',
                'amount': 'Amount (₹)'
            }
            data = [
                {'gst_type': 'CGST Collected', 'amount': cgst},
                {'gst_type': 'SGST Collected', 'amount': sgst},
                {'gst_type': 'IGST Collected', 'amount': igst},
                {'gst_type': 'Total Sales Tax', 'amount': total_sales_tax},
                {'gst_type': 'Input Credit (Purchase Tax)', 'amount': total_purchase_tax},
                {'gst_type': 'Net GST Payable', 'amount': net_gst},
            ]
            return export_excel(request, data, headers, 'gst_report', 'GST Report')
        
        if is_print:
            company = CompanyProfile.get_instance()
            context = {
                'invoices': invoices,
                'purchases': purchases,
                'total_sales_tax': total_sales_tax,
                'total_purchase_tax': total_purchase_tax,
                'net_gst': net_gst,
                'cgst': cgst,
                'sgst': sgst,
                'igst': igst,
                'date_from': date_from,
                'date_to': date_to,
                'company': company,
                'title': 'GST Report',
            }
            return render(request, 'reports/print_report.html', context)
        
        context = {
            'invoices': invoices,
            'purchases': purchases,
            'total_sales_tax': total_sales_tax,
            'total_purchase_tax': total_purchase_tax,
            'net_gst': net_gst,
            'cgst': cgst,
            'sgst': sgst,
            'igst': igst,
            'date_from': date_from,
            'date_to': date_to,
            'sales_count': invoices.count(),
            'purchase_count': purchases.count(),
        }
        return render(request, 'reports/gst_report.html', context)
        
    except Exception as e:
        logger.error(f"GST report error: {e}")
        messages.error(request, "Unable to generate GST report.")
        return render(request, 'reports/gst_report.html', {
            'invoices': [], 'purchases': [],
            'total_sales_tax': 0, 'total_purchase_tax': 0,
            'net_gst': 0, 'cgst': 0, 'sgst': 0, 'igst': 0
        })


# ============================================================
# 4. PROFIT & LOSS (Enhanced)
# ============================================================

@handle_errors(default_redirect='accounting:profit_loss')
def profit_loss(request):
    try:
        date_from = request.GET.get('date_from', '').strip()
        date_to = request.GET.get('date_to', '').strip()
        is_excel = request.GET.get('excel') == '1'
        is_print = request.GET.get('print') == '1'
        
        # Sales Income
        sales = Invoice.objects.all()
        if date_from:
            sales = sales.filter(date__gte=date_from)
        if date_to:
            sales = sales.filter(date__lte=date_to)
        sales_total = sales.aggregate(Sum('grand_total'))['grand_total__sum'] or Decimal('0')
        
        # Cost of Goods Sold (from purchases)
        purchases = Purchase.objects.all()
        if date_from:
            purchases = purchases.filter(date__gte=date_from)
        if date_to:
            purchases = purchases.filter(date__lte=date_to)
        purchase_total = purchases.aggregate(Sum('grand_total'))['grand_total__sum'] or Decimal('0')
        
        # Gross Profit
        gross_profit = sales_total - purchase_total
        
        # Repair Income (labour charges from repairs)
        repairs = RepairJob.objects.all()
        if date_from:
            repairs = repairs.filter(date_in__gte=date_from)
        if date_to:
            repairs = repairs.filter(date_in__lte=date_to)
        repair_income = repairs.aggregate(Sum('labour_charge'))['labour_charge__sum'] or Decimal('0')
        
        # Total Income
        total_income = sales_total + repair_income
        
        # Total Expenses (for now, just purchases)
        total_expenses = purchase_total
        
        # Net Profit
        net_profit = gross_profit + repair_income
        
        # Summary data
        summary_data = [
            {'category': 'Total Sales', 'amount': sales_total},
            {'category': 'Repair Income', 'amount': repair_income},
            {'category': 'Total Income', 'amount': total_income},
            {'category': 'Cost of Goods Sold', 'amount': purchase_total},
            {'category': 'Total Expenses', 'amount': total_expenses},
            {'category': 'Gross Profit', 'amount': gross_profit},
            {'category': 'Net Profit', 'amount': net_profit},
        ]
        
        if is_excel:
            headers = {'category': 'Category', 'amount': 'Amount (₹)'}
            return export_excel(request, summary_data, headers, 'profit_loss', 'Profit & Loss Report')
        
        if is_print:
            company = CompanyProfile.get_instance()
            context = {
                'sales_total': sales_total,
                'purchase_total': purchase_total,
                'repair_income': repair_income,
                'gross_profit': gross_profit,
                'net_profit': net_profit,
                'total_income': total_income,
                'total_expenses': total_expenses,
                'date_from': date_from,
                'date_to': date_to,
                'company': company,
                'title': 'Profit & Loss Report',
            }
            return render(request, 'reports/print_profit_loss.html', context)
        
        context = {
            'sales_total': sales_total,
            'purchase_total': purchase_total,
            'repair_income': repair_income,
            'gross_profit': gross_profit,
            'net_profit': net_profit,
            'total_income': total_income,
            'total_expenses': total_expenses,
            'date_from': date_from,
            'date_to': date_to,
        }
        return render(request, 'reports/profit_loss.html', context)
        
    except Exception as e:
        logger.error(f"Profit/Loss report error: {e}")
        messages.error(request, "Unable to generate profit/loss report.")
        return render(request, 'reports/profit_loss.html', {
            'sales_total': 0, 'purchase_total': 0,
            'repair_income': 0, 'gross_profit': 0,
            'net_profit': 0, 'total_income': 0,
            'total_expenses': 0
        })


# ============================================================
# 5. STOCK REPORT (Enhanced)
# ============================================================

@handle_errors(default_redirect='accounting:stock_report')
def stock_report(request):
    try:
        category_id = request.GET.get('category', '').strip()
        is_excel = request.GET.get('excel') == '1'
        is_print = request.GET.get('print') == '1'
        
        products = Product.objects.filter(is_service=False).order_by('name')
        
        if category_id:
            products = products.filter(category_id=category_id)
        
        # Calculate stock value
        total_stock_value = Decimal('0')
        for p in products:
            p.stock_value = p.current_stock * p.purchase_price
            total_stock_value += p.stock_value
            p.is_low_stock = p.current_stock <= p.low_stock_threshold
        
        if is_excel:
            headers = {
                'name': 'Product Name',
                'category': 'Category',
                'hsn_code': 'HSN Code',
                'current_stock': 'Stock (Qty)',
                'purchase_price': 'Purchase Price (₹)',
                'selling_price': 'Selling Price (₹)',
                'stock_value': 'Stock Value (₹)',
                'status': 'Status'
            }
            data = []
            for p in products:
                data.append({
                    'name': p.name,
                    'category': p.category.name if p.category else '-',
                    'hsn_code': p.hsn_code or '-',
                    'current_stock': p.current_stock,
                    'purchase_price': p.purchase_price,
                    'selling_price': p.selling_price,
                    'stock_value': p.stock_value,
                    'status': 'Low Stock' if p.is_low_stock else 'OK'
                })
            summary = {'stock_value': total_stock_value}
            return export_excel(request, data, headers, 'stock_report', 'Stock Report', summary)
        
        if is_print:
            company = CompanyProfile.get_instance()
            context = {
                'products': products,
                'total_stock_value': total_stock_value,
                'company': company,
                'title': 'Stock Report',
            }
            return render(request, 'reports/print_report.html', context)
        
        context = {
            'products': products,
            'total_stock_value': total_stock_value,
            'categories': ProductCategory.objects.all(),
            'category_id': category_id,
        }
        return render(request, 'reports/stock_report.html', context)
        
    except Exception as e:
        logger.error(f"Stock report error: {e}")
        messages.error(request, "Unable to generate stock report.")
        return render(request, 'reports/stock_report.html', {'products': [], 'total_stock_value': 0})


# ============================================================
# 6. TRIAL BALANCE (Enhanced)
# ============================================================

@handle_errors(default_redirect='accounting:trial_balance')
def trial_balance(request):
    try:
        as_on = request.GET.get('as_on', '').strip()
        is_excel = request.GET.get('excel') == '1'
        is_print = request.GET.get('print') == '1'
        
        if as_on:
            as_on_date = date.fromisoformat(as_on)
        else:
            as_on_date = date.today()
        
        # Get all ledger accounts with balances
        accounts = LedgerLine.objects.values('account').annotate(
            total_dr=Sum('debit'),
            total_cr=Sum('credit')
        ).order_by('account')
        
        total_dr = Decimal('0')
        total_cr = Decimal('0')
        
        account_data = []
        for acc in accounts:
            dr = acc['total_dr'] or Decimal('0')
            cr = acc['total_cr'] or Decimal('0')
            total_dr += dr
            total_cr += cr
            account_data.append({
                'account': acc['account'],
                'debit': dr,
                'credit': cr,
            })
        
        if is_excel:
            headers = {'account': 'Account', 'debit': 'Debit (₹)', 'credit': 'Credit (₹)'}
            summary = {'debit': total_dr, 'credit': total_cr}
            return export_excel(request, account_data, headers, 'trial_balance', 'Trial Balance', summary)
        
        if is_print:
            company = CompanyProfile.get_instance()
            context = {
                'accounts': account_data,
                'total_dr': total_dr,
                'total_cr': total_cr,
                'as_on': as_on_date,
                'company': company,
                'title': 'Trial Balance',
            }
            return render(request, 'reports/print_report.html', context)
        
        context = {
            'accounts': account_data,
            'total_dr': total_dr,
            'total_cr': total_cr,
            'as_on': as_on_date,
        }
        return render(request, 'reports/trial_balance.html', context)
        
    except Exception as e:
        logger.error(f"Trial balance error: {e}")
        messages.error(request, "Unable to generate trial balance.")
        return render(request, 'reports/trial_balance.html', {'accounts': [], 'total_dr': 0, 'total_cr': 0})


# ============================================================
# 7. BALANCE SHEET (Enhanced)
# ============================================================

@handle_errors(default_redirect='accounting:balance_sheet')
def balance_sheet(request):
    try:
        as_on = request.GET.get('as_on', '').strip()
        is_excel = request.GET.get('excel') == '1'
        is_print = request.GET.get('print') == '1'
        
        if as_on:
            as_on_date = date.fromisoformat(as_on)
        else:
            as_on_date = date.today()
        
        # ===== ASSETS =====
        
        # 1. Cash Balance
        cash_balance = LedgerLine.objects.filter(account='Cash').aggregate(
            total=Sum('debit') - Sum('credit')
        )['total'] or Decimal('0')
        
        # 2. Bank Balances (all bank accounts)
        bank_balance = LedgerLine.objects.filter(account__icontains='Bank').aggregate(
            total=Sum('debit') - Sum('credit')
        )['total'] or Decimal('0')
        
        # 3. Accounts Receivable (Customer Balances)
        ar_total = Decimal('0')
        customers = Contact.objects.filter(contact_type__in=['customer', 'both'])
        for cust in customers:
            bal = cust.balance
            if bal > 0:
                ar_total += bal
        
        # 4. Inventory Value
        inventory_value = Decimal('0')
        for p in Product.objects.filter(is_service=False):
            inventory_value += p.current_stock * p.purchase_price
        
        # Total Assets
        total_assets = cash_balance + bank_balance + ar_total + inventory_value
        
        # ===== LIABILITIES =====
        
        # 1. Accounts Payable (Vendor Balances)
        ap_total = Decimal('0')
        vendors = Contact.objects.filter(contact_type__in=['vendor', 'both'])
        for ven in vendors:
            bal = ven.balance
            if bal < 0:
                ap_total += abs(bal)
        
        # Total Liabilities
        total_liabilities = ap_total
        
        # ===== EQUITY =====
        
        # 1. Capital/Profit (simplified)
        sales_total = Invoice.objects.aggregate(Sum('grand_total'))['grand_total__sum'] or Decimal('0')
        purchase_total = Purchase.objects.aggregate(Sum('grand_total'))['grand_total__sum'] or Decimal('0')
        profit = sales_total - purchase_total
        repair_income = RepairJob.objects.aggregate(Sum('labour_charge'))['labour_charge__sum'] or Decimal('0')
        net_profit = profit + repair_income
        
        # Total Equity
        total_equity = net_profit
        
        # Accounting Equation: Assets = Liabilities + Equity
        total_liabilities_equity = total_liabilities + total_equity
        difference = total_assets - total_liabilities_equity
        
        if is_excel:
            # Asset data
            asset_data = [
                {'item': 'Cash', 'amount': cash_balance},
                {'item': 'Bank Accounts', 'amount': bank_balance},
                {'item': 'Accounts Receivable', 'amount': ar_total},
                {'item': 'Inventory', 'amount': inventory_value},
                {'item': 'Total Assets', 'amount': total_assets},
            ]
            liability_data = [
                {'item': 'Accounts Payable', 'amount': ap_total},
                {'item': 'Total Liabilities', 'amount': total_liabilities},
                {'item': 'Net Profit', 'amount': net_profit},
                {'item': 'Total Equity', 'amount': total_equity},
                {'item': 'Total Liabilities + Equity', 'amount': total_liabilities_equity},
            ]
            # Combine for Excel
            combined = asset_data + liability_data
            headers = {'item': 'Item', 'amount': 'Amount (₹)'}
            return export_excel(request, combined, headers, 'balance_sheet', f'Balance Sheet as on {as_on_date}')
        
        if is_print:
            company = CompanyProfile.get_instance()
            context = {
                'cash_balance': cash_balance,
                'bank_balance': bank_balance,
                'ar_total': ar_total,
                'inventory_value': inventory_value,
                'ap_total': ap_total,
                'net_profit': net_profit,
                'total_assets': total_assets,
                'total_liabilities': total_liabilities,
                'total_equity': total_equity,
                'total_liabilities_equity': total_liabilities_equity,
                'difference': difference,
                'as_on': as_on_date,
                'company': company,
                'title': 'Balance Sheet',
            }
            return render(request, 'reports/print_balance_sheet.html', context)
        
        context = {
            'cash_balance': cash_balance,
            'bank_balance': bank_balance,
            'ar_total': ar_total,
            'inventory_value': inventory_value,
            'ap_total': ap_total,
            'net_profit': net_profit,
            'total_assets': total_assets,
            'total_liabilities': total_liabilities,
            'total_equity': total_equity,
            'total_liabilities_equity': total_liabilities_equity,
            'difference': difference,
            'as_on': as_on_date,
        }
        return render(request, 'reports/balance_sheet.html', context)
        
    except Exception as e:
        logger.error(f"Balance sheet error: {e}")
        messages.error(request, "Unable to generate balance sheet.")
        return render(request, 'reports/balance_sheet.html', {
            'cash_balance': 0, 'bank_balance': 0,
            'ar_total': 0, 'inventory_value': 0,
            'ap_total': 0, 'net_profit': 0,
            'total_assets': 0, 'total_liabilities': 0,
            'total_equity': 0, 'total_liabilities_equity': 0,
            'difference': 0
        })


# ============================================================
# 8. AGING REPORT (Enhanced)
# ============================================================

@handle_errors(default_redirect='accounting:aging_report')
def aging_report(request):
    try:
        as_on_str = request.GET.get('as_on')
        if not as_on_str:
            as_on = date.today()
        else:
            as_on = date.fromisoformat(as_on_str)
        
        report_type = request.GET.get('type', 'customer')
        is_excel = request.GET.get('excel') == '1'
        is_print = request.GET.get('print') == '1'
        
        aging_data = []
        totals = {
            '0_30': Decimal('0'),
            '31_60': Decimal('0'),
            '61_90': Decimal('0'),
            '90_plus': Decimal('0'),
            'total': Decimal('0'),
        }
        
        if report_type == 'customer':
            contacts = Contact.objects.filter(contact_type__in=['customer', 'both'])
            for contact in contacts:
                invoices = Invoice.objects.filter(
                    customer=contact,
                    payment_status__in=['unpaid', 'partial'],
                    due_date__isnull=False
                )
                row = {
                    'name': contact.name,
                    'range_0_30': Decimal('0'),
                    'range_31_60': Decimal('0'),
                    'range_61_90': Decimal('0'),
                    'range_90_plus': Decimal('0'),
                    'total': Decimal('0'),
                }
                total_due = Decimal('0')
                for inv in invoices:
                    due = inv.balance_due
                    if due <= 0:
                        continue
                    days_diff = (as_on - inv.due_date).days if inv.due_date else 0
                    if days_diff <= 30:
                        row['range_0_30'] += due
                    elif days_diff <= 60:
                        row['range_31_60'] += due
                    elif days_diff <= 90:
                        row['range_61_90'] += due
                    else:
                        row['range_90_plus'] += due
                    total_due += due
                row['total'] = total_due
                if total_due > 0:
                    aging_data.append(row)
                    totals['0_30'] += row['range_0_30']
                    totals['31_60'] += row['range_31_60']
                    totals['61_90'] += row['range_61_90']
                    totals['90_plus'] += row['range_90_plus']
                    totals['total'] += total_due
        
        else:
            contacts = Contact.objects.filter(contact_type__in=['vendor', 'both'])
            for contact in contacts:
                purchases = Purchase.objects.filter(
                    vendor=contact,
                    paid=False
                )
                row = {
                    'name': contact.name,
                    'range_0_30': Decimal('0'),
                    'range_31_60': Decimal('0'),
                    'range_61_90': Decimal('0'),
                    'range_90_plus': Decimal('0'),
                    'total': Decimal('0'),
                }
                total_due = Decimal('0')
                for pur in purchases:
                    due = pur.grand_total
                    if due <= 0:
                        continue
                    days_diff = (as_on - pur.date).days
                    if days_diff <= 30:
                        row['range_0_30'] += due
                    elif days_diff <= 60:
                        row['range_31_60'] += due
                    elif days_diff <= 90:
                        row['range_61_90'] += due
                    else:
                        row['range_90_plus'] += due
                    total_due += due
                row['total'] = total_due
                if total_due > 0:
                    aging_data.append(row)
                    totals['0_30'] += row['range_0_30']
                    totals['31_60'] += row['range_31_60']
                    totals['61_90'] += row['range_61_90']
                    totals['90_plus'] += row['range_90_plus']
                    totals['total'] += total_due
        
        aging_data.sort(key=lambda x: x['total'], reverse=True)
        
        if is_excel:
            headers = {
                'name': 'Customer/Vendor',
                'range_0_30': '0-30 Days (₹)',
                'range_31_60': '31-60 Days (₹)',
                'range_61_90': '61-90 Days (₹)',
                'range_90_plus': '90+ Days (₹)',
                'total': 'Total (₹)'
            }
            summary = {
                'range_0_30': totals['0_30'],
                'range_31_60': totals['31_60'],
                'range_61_90': totals['61_90'],
                'range_90_plus': totals['90_plus'],
                'total': totals['total']
            }
            return export_excel(request, aging_data, headers, 'aging_report', 'Aging Report', summary)
        
        if is_print:
            company = CompanyProfile.get_instance()
            context = {
                'aging_data': aging_data,
                'totals': totals,
                'as_on': as_on,
                'type': report_type,
                'company': company,
                'title': 'Aging Report',
            }
            return render(request, 'reports/print_report.html', context)
        
        context = {
            'aging_data': aging_data,
            'totals': totals,
            'as_on': as_on,
            'type': report_type,
        }
        return render(request, 'reports/aging_report.html', context)
        
    except Exception as e:
        logger.error(f"Aging report error: {e}")
        messages.error(request, "Unable to generate aging report.")
        return render(request, 'reports/aging_report.html', {
            'aging_data': [],
            'totals': {'0_30': 0, '31_60': 0, '61_90': 0, '90_plus': 0, 'total': 0},
            'as_on': date.today(),
            'type': 'customer',
        })