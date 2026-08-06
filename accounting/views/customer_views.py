# accounting/views/customer_views.py

import json
import csv
import logging
from decimal import Decimal
from datetime import datetime, date, timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_protect
from django.contrib import messages
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Sum, Q, Count, F
from django.utils import timezone
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.core.mail import send_mail
from django.conf import settings

from ..models import *
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from ..forms import CustomerProfileForm, CustomerRepairForm, EmailChangeRequestForm
from ..utils.notification_helpers import send_notification_to_staff, send_notification_sse, otp_helpers
from ..utils.otp_helpers import create_and_send_otp, verify_otp
from .utils import is_htmx, htmx_response, redirect_to_customer, redirect_to_staff, toast_only_response
from ..decorators import handle_errors

# Excel Export
try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
except ImportError:
    openpyxl = None

logger = logging.getLogger(__name__)


# ============================================================
# FIELD VALIDATION (HTMX) – PUBLIC
# ============================================================
@handle_errors(default_redirect='customer:customer_repairs')
def validate_repair_field(request):
    """Real‑time validation for repair fields (guest can access)."""
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    value = request.GET.get(field_name, '')
    form = CustomerRepairForm(data={field_name: value})
    if form.is_valid():
        return HttpResponse('')
    else:
        errors = form.errors.get(field_name, [])
        error_html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
        for err in errors:
            error_html += f'<div>{err}</div>'
        error_html += '</div>'
        return HttpResponse(error_html)


# ============================================================
# CUSTOMER DASHBOARD (ENHANCED)
# ============================================================
@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def dashboard(request):
    """Complete customer dashboard with stats and recent activity."""
    # Ensure Contact exists; if not, create it automatically
    contact, created = Contact.objects.get_or_create(
        user=request.user,
        defaults={
            'name': request.user.get_full_name() or request.user.username,
            'email': request.user.email,
            'contact_type': 'customer'
        }
    )
    customer = contact  
    
    today = date.today()
    month_start = today.replace(day=1)
    
    # Invoices
    invoices = Invoice.objects.filter(customer=customer)
    total_invoices = invoices.count()
    total_paid = invoices.aggregate(Sum('paid_amount'))['paid_amount__sum'] or Decimal('0')
    total_due = invoices.aggregate(Sum('balance_due'))['balance_due__sum'] or Decimal('0')
    paid_invoices = invoices.filter(payment_status='paid').count()
    unpaid_invoices = invoices.filter(payment_status__in=['unpaid', 'partial']).count()
    
    # Month stats
    month_invoices = invoices.filter(date__gte=month_start)
    month_sales = month_invoices.aggregate(Sum('grand_total'))['grand_total__sum'] or Decimal('0')
    month_paid = month_invoices.aggregate(Sum('paid_amount'))['paid_amount__sum'] or Decimal('0')
    
    # Repairs
    repairs = RepairJob.objects.filter(customer=customer)
    total_repairs = repairs.count()
    pending_repairs = repairs.filter(status__in=['pending', 'diagnosis', 'repairing']).count()
    ready_repairs = repairs.filter(status='ready').count()
    completed_repairs = repairs.filter(status='delivered').count()
    cancelled_repairs = repairs.filter(status='cancelled').count()
    
    # Estimate status
    approved_estimates = repairs.filter(estimate_status='approved').count()
    pending_estimates = repairs.filter(estimate_status='pending').count()
    hold_estimates = repairs.filter(estimate_status='on_hold').count()
    rejected_estimates = repairs.filter(estimate_status='rejected').count()
    
    # Payments
    payments = Payment.objects.filter(contact=customer, direction='received')
    total_payments = payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    month_payments = payments.filter(date__gte=month_start).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    
    # Unread notifications
    unread_count = request.user.notifications.filter(is_read=False).count()
    
    # Recent activity
    recent_invoices = invoices.order_by('-date')[:5]
    recent_repairs = repairs.order_by('-date_in')[:5]
    recent_payments = payments.order_by('-date')[:5]
    
    context = {
        'customer': customer,
        'total_invoices': total_invoices,
        'total_paid': total_paid,
        'total_due': total_due,
        'paid_invoices': paid_invoices,
        'unpaid_invoices': unpaid_invoices,
        'month_sales': month_sales,
        'month_paid': month_paid,
        'total_repairs': total_repairs,
        'pending_repairs': pending_repairs,
        'ready_repairs': ready_repairs,
        'completed_repairs': completed_repairs,
        'cancelled_repairs': cancelled_repairs,
        'approved_estimates': approved_estimates,
        'pending_estimates': pending_estimates,
        'hold_estimates': hold_estimates,
        'rejected_estimates': rejected_estimates,
        'total_payments': total_payments,
        'month_payments': month_payments,
        'unread_count': unread_count,
        'recent_invoices': recent_invoices,
        'recent_repairs': recent_repairs,
        'recent_payments': recent_payments,
        'today': today,
        'month_start': month_start,
    }
    
    if is_htmx(request):
        return render(request, 'customer/partials/dashboard_stats.html', context)
    return render(request, 'customer/dashboard.html', context)


# ============================================================
# CUSTOMER DASHBOARD STATS (JSON for Charts)
# ============================================================
@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def dashboard_stats_json(request):
    """Return chart data as JSON for customer dashboard."""
    customer = get_object_or_404(Contact, user=request.user)
    
    try:
        today = date.today()
        invoice_data = []
        payment_data = []
        labels = []
        
        # Last 7 days
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            labels.append(d.strftime('%d %b'))
            
            invoice_data.append(
                float(Invoice.objects.filter(customer=customer, date=d).aggregate(
                    total=Sum('grand_total')
                )['total'] or Decimal('0'))
            )
            
            payment_data.append(
                float(Payment.objects.filter(
                    contact=customer, direction='received', date=d
                ).aggregate(total=Sum('amount'))['total'] or Decimal('0'))
            )
        
        # Invoice Status Distribution
        invoice_status = Invoice.objects.filter(customer=customer).values('payment_status').annotate(
            count=Count('id')
        )
        status_labels = [dict(Invoice.PAYMENT_STATUS).get(s['payment_status'], s['payment_status']) for s in invoice_status]
        status_data = [s['count'] for s in invoice_status]
        
        # Repair Status Distribution
        repair_status = RepairJob.objects.filter(customer=customer).values('status').annotate(
            count=Count('id')
        )
        repair_status_labels = [dict(RepairJob.STATUS_CHOICES).get(s['status'], s['status']) for s in repair_status]
        repair_status_data = [s['count'] for s in repair_status]
        
        return JsonResponse({
            'labels': labels,
            'invoices': invoice_data,
            'payments': payment_data,
            'status_labels': status_labels,
            'status_data': status_data,
            'repair_status_labels': repair_status_labels,
            'repair_status_data': repair_status_data,
        })
        
    except Exception as e:
        logger.error(f"Customer dashboard stats error: {e}")
        return JsonResponse({'error': 'Unable to load statistics.'}, status=500)


# ============================================================
# REFRESH DASHBOARD STATS (HTMX)
# ============================================================
@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def refresh_dashboard_stats(request):
    """Refresh dashboard stats via HTMX."""
    customer = get_object_or_404(Contact, user=request.user)
    
    today = date.today()
    month_start = today.replace(day=1)
    
    invoices = Invoice.objects.filter(customer=customer)
    repairs = RepairJob.objects.filter(customer=customer)
    payments = Payment.objects.filter(contact=customer, direction='received')
    
    context = {
        'customer': customer,
        'total_invoices': invoices.count(),
        'total_paid': invoices.aggregate(Sum('paid_amount'))['paid_amount__sum'] or Decimal('0'),
        'total_due': invoices.aggregate(Sum('balance_due'))['balance_due__sum'] or Decimal('0'),
        'paid_invoices': invoices.filter(payment_status='paid').count(),
        'unpaid_invoices': invoices.filter(payment_status__in=['unpaid', 'partial']).count(),
        'pending_repairs': repairs.filter(status__in=['pending', 'diagnosis', 'repairing']).count(),
        'ready_repairs': repairs.filter(status='ready').count(),
        'completed_repairs': repairs.filter(status='delivered').count(),
        'total_payments': payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0'),
        'unread_count': request.user.notifications.filter(is_read=False).count(),
    }
    return render(request, 'customer/partials/dashboard_stats.html', context)


# ============================================================
# INVOICES (With Pagination)
# ============================================================
@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def invoice_list(request):
    customer = get_object_or_404(Contact, user=request.user)
    invoices = Invoice.objects.filter(customer=customer).order_by('-date')

    paginator = Paginator(invoices, 10)
    page = request.GET.get('page', 1)
    try:
        invoices_page = paginator.page(page)
    except (PageNotAnInteger, EmptyPage):
        invoices_page = paginator.page(1)

    context = {
        'invoices': invoices_page,
        'page_obj': invoices_page,
        'total_count': paginator.count,
        'customer': customer,
    }

    if is_htmx(request):
        return render(request, 'customer/partials/invoice_list_table.html', context)
    return render(request, 'customer/invoices.html', context)


@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def invoice_detail(request, pk):
    """
    Customer invoice detail with repair context (if available).
    Shows action taken, diagnosis, and issue description from linked repair job.
    """
    customer = get_object_or_404(Contact, user=request.user)
    invoice = get_object_or_404(Invoice, pk=pk, customer=customer)
    
    # Check if invoice is linked to a repair job
    repair_job = None
    if invoice:
        try:
            repair_job = RepairJob.objects.get(invoice=invoice)
        except RepairJob.DoesNotExist:
            pass
    
    context = {
        'invoice': invoice,
        'repair_job': repair_job,  
    }
    return render(request, 'customer/invoice_detail.html', context)


# ============================================================
# INVOICE PRINT (Customer) — ENHANCED with Repair Context
# ============================================================

@login_required
@handle_errors(default_redirect='customer:customer_invoices')
def invoice_print(request, pk):
    """
    Customer invoice print view with repair context.
    Shows action taken, diagnosis, and issue description from linked repair job.
    """
    # Verify ownership
    customer = get_object_or_404(Contact, user=request.user)
    invoice = get_object_or_404(Invoice, pk=pk, customer=customer)
    
    # Check if invoice is linked to a repair job
    repair_job = None
    try:
        repair_job = RepairJob.objects.get(invoice=invoice)
    except RepairJob.DoesNotExist:
        pass
    
    # Get company profile for header/footer
    company = CompanyProfile.get_instance()
    
    # Check if logo exists
    logo_exists = bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name))
    
    # Get GST breakup
    gst_breakup = invoice.get_gst_breakup() if hasattr(invoice, 'get_gst_breakup') else {
        'total_tax': invoice.tax_amount,
        'cgst': 0,
        'sgst': 0,
        'igst': 0
    }
    
    context = {
        'invoice': invoice,
        'customer': customer,
        'company': company,
        'logo_exists': logo_exists,
        'gst_breakup': gst_breakup,
        'repair_job': repair_job,  
    }
    return render(request, 'customer/invoice_print.html', context)


# ============================================================
# REPAIRS (With Filters, Pagination, Print)
# ============================================================
@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def repair_list(request):
    customer = get_object_or_404(Contact, user=request.user)
    repairs = RepairJob.objects.filter(customer=customer).order_by('-date_in')

    # Filters
    status_filter = request.GET.get('status', '')
    search = request.GET.get('search', '').strip()
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    is_print = request.GET.get('print') == '1'

    if status_filter:
        repairs = repairs.filter(status=status_filter)
    if search:
        repairs = repairs.filter(
            Q(job_number__icontains=search) |
            Q(device_model__icontains=search) |
            Q(issue_description__icontains=search)
        )
    if date_from:
        repairs = repairs.filter(date_in__gte=date_from)
    if date_to:
        repairs = repairs.filter(date_in__lte=date_to)

    # Print Mode 
    if is_print:
        company = CompanyProfile.get_instance()
        context = {
            'repairs': repairs,
            'customer': customer,
            'company': company,
            'logo_exists': bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name)),
            'status_filter': status_filter,
            'search': search,
            'date_from': date_from,
            'date_to': date_to,
            'total_count': repairs.count(),
            'repair_status_choices': RepairJob.STATUS_CHOICES,
        }
        return render(request, 'customer/repair_list_print.html', context)

    # Normal View with Pagination
    paginator = Paginator(repairs, 10)
    page = request.GET.get('page', 1)
    try:
        repairs_page = paginator.page(page)
    except (PageNotAnInteger, EmptyPage):
        repairs_page = paginator.page(1)

    context = {
        'repairs': repairs_page,
        'page_obj': repairs_page,
        'status_filter': status_filter,
        'search': search,
        'date_from': date_from,
        'date_to': date_to,
        'total_count': paginator.count,
        'repair_status_choices': RepairJob.STATUS_CHOICES,
        'customer': customer,
    }

    if is_htmx(request):
        return render(request, 'customer/partials/repair_list_table.html', context)
    return render(request, 'customer/repairs.html', context)


@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def repair_detail(request, pk):
    customer = get_object_or_404(Contact, user=request.user)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)
    parts = repair.parts.all()
    parts_total = sum(part.line_total for part in parts)

    context = {
        'repair': repair,
        'parts': parts,
        'parts_total': parts_total,
    }
    return render(request, 'customer/repair_detail.html', context)


@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def repair_print(request, pk):
    """
    Customer repair print view.
    Uses customer-specific print template with proper branding.
    """
    # Get customer and verify ownership
    customer = get_object_or_404(Contact, user=request.user)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)
    
    # Get company profile for header/footer
    company = CompanyProfile.get_instance()
    
    # Get parts and calculate totals
    parts = repair.parts.all()
    parts_total = sum(part.line_total for part in parts)
    
    # Check if logo exists
    logo_exists = bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name))
    
    context = {
        'repair': repair,
        'parts': parts,
        'parts_total': parts_total,
        'customer': customer,
        'company': company,
        'logo_exists': logo_exists,
    }
    
    # Use customer-specific print template
    return render(request, 'customer/repair_print.html', context)


# ============================================================
# PAYMENTS (With Filters, Pagination, HTMX, Print, Excel)
# ============================================================

@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def payment_list(request):
    """Customer payment list with filters, pagination, HTMX, Print, Excel."""
    customer = get_object_or_404(Contact, user=request.user)
    
    # Get filter parameters
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    method = request.GET.get('method', '')
    search = request.GET.get('search', '').strip()
    is_print = request.GET.get('print') == '1'
    is_excel = request.GET.get('excel') == '1'
    
    # Only 'received' payments (customer paid to shop)
    payments = Payment.objects.filter(
        contact=customer,
        direction='received'
    ).order_by('-date')
    
    # Apply filters
    if date_from:
        payments = payments.filter(date__gte=date_from)
    if date_to:
        payments = payments.filter(date__lte=date_to)
    if method:
        payments = payments.filter(method=method)
    if search:
        payments = payments.filter(
            Q(invoices__invoice_number__icontains=search) |
            Q(reference__icontains=search) |
            Q(upi_ref__icontains=search) |
            Q(description__icontains=search)
        ).distinct()
    
    # Excel Export
    if is_excel:
        return customer_payments_excel(request, payments, customer)
    
    # Print Mode
    if is_print:
        company = CompanyProfile.get_instance()
        total_amount = payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
        context = {
            'customer': customer,
            'payments': payments,
            'total_amount': total_amount,
            'company': company,
            'logo_exists': bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name)),
            'date_from': date_from,
            'date_to': date_to,
            'method': method,
            'search': search,
        }
        return render(request, 'customer/payment_print.html', context)
    
    # Pagination
    paginator = Paginator(payments, 10)
    page = request.GET.get('page', 1)
    try:
        payments_page = paginator.page(page)
    except (PageNotAnInteger, EmptyPage):
        payments_page = paginator.page(1)
    
    # Summary stats
    total_amount = payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    total_count = paginator.count
    
    context = {
        'customer': customer,
        'payments': payments_page,
        'page_obj': payments_page,
        'total_amount': total_amount,
        'total_count': total_count,
        'date_from': date_from,
        'date_to': date_to,
        'method': method,
        'search': search,
    }
    
    # HTMX partial
    if is_htmx(request):
        return render(request, 'customer/partials/payment_list_table.html', context)
    
    # Full page
    return render(request, 'customer/payments.html', context)


# ============================================================
# PAYMENTS - EXCEL EXPORT
# ============================================================
def customer_payments_excel(request, payments, customer):
    """Export payment list to Excel."""
    if openpyxl is None:
        return toast_only_response(
            {'level': 'danger', 'message': 'Openpyxl library is not installed.'},
            status=400
        )
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Payments"
    
    # Styles
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    center_align = Alignment(horizontal='center', vertical='center')
    money_format = '#,##0.00'
    
    company = CompanyProfile.get_instance()
    ws.merge_cells('A1:G1')
    ws['A1'] = company.name
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A2:G2')
    ws['A2'] = f"Payment History – {customer.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws.merge_cells('A3:G3')
    ws['A3'] = f"Generated: {datetime.now().strftime('%d-%m-%Y %H:%M')}"
    ws['A3'].alignment = Alignment(horizontal="center")
    
    headers = ['Date', 'Invoice', 'Amount (₹)', 'Method', 'UPI Ref', 'Reference', 'Description']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=5, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border
    
    row = 6
    total = Decimal('0')
    for payment in payments:
        invoice_numbers = ', '.join([inv.invoice_number for inv in payment.invoices.all()])
        total += payment.amount
        
        ws.cell(row=row, column=1, value=payment.date.strftime("%d-%m-%Y"))
        ws.cell(row=row, column=2, value=invoice_numbers)
        ws.cell(row=row, column=3, value=float(payment.amount))
        ws.cell(row=row, column=3).number_format = money_format
        ws.cell(row=row, column=4, value=payment.get_method_display())
        ws.cell(row=row, column=5, value=payment.upi_ref or '')
        ws.cell(row=row, column=6, value=payment.reference or '')
        ws.cell(row=row, column=7, value=payment.description or '')
        for col in range(1, 8):
            ws.cell(row=row, column=col).border = thin_border
        row += 1
    
    # Total row
    ws.cell(row=row, column=2, value="Total")
    ws.cell(row=row, column=3, value=float(total))
    ws.cell(row=row, column=3).number_format = money_format
    for col in range(1, 8):
        ws.cell(row=row, column=col).border = thin_border
        ws.cell(row=row, column=col).font = Font(bold=True)
    
    # Column widths
    ws.column_dimensions['A'].width = 15
    ws.column_dimensions['B'].width = 18
    ws.column_dimensions['C'].width = 15
    ws.column_dimensions['D'].width = 15
    ws.column_dimensions['E'].width = 20
    ws.column_dimensions['F'].width = 18
    ws.column_dimensions['G'].width = 35
    ws.freeze_panes = 'A6'
    
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="payments_{customer.name}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
    wb.save(response)
    return response


# ============================================================
# STATEMENT (With Filters, Pagination, Print, Excel)
# ============================================================

@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def statement(request):
    """Customer statement with filters, pagination, print, and Excel export."""
    customer = get_object_or_404(Contact, user=request.user)

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '').strip()
    is_print = request.GET.get('print') == '1'
    is_excel = request.GET.get('excel') == '1'

    # Excel Export
    if is_excel:
        return customer_statement_excel(request, customer)

    opening_balance = customer.opening_balance

    lines = LedgerLine.objects.filter(contact=customer) \
        .exclude(ledger_entry__entry_type='opening') \
        .select_related('ledger_entry') \
        .order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)
    if txn_type == 'invoice':
        lines = lines.filter(ledger_entry__entry_type='sales')
    elif txn_type == 'payment':
        lines = lines.filter(ledger_entry__entry_type='payment')
    elif txn_type == 'journal':
        lines = lines.filter(ledger_entry__entry_type='journal')
    if search:
        lines = lines.filter(ledger_entry__description__icontains=search)

    running_balance = opening_balance
    rows = []

    for line in lines:
        entry = line.ledger_entry

        invoice = None
        repair = None
        invoice_no = None
        device_model = None
        action_taken = None
        invoice_id = None
        repair_job_id = None

        if entry.entry_type == 'sales' and entry.reference_id:
            try:
                invoice = Invoice.objects.get(pk=entry.reference_id)
                invoice_id = invoice.id
                invoice_no = invoice.invoice_number
                repair = RepairJob.objects.filter(invoice=invoice).first()
                if repair:
                    repair_job_id = repair.id
                    device_model = repair.device_model
                    action_taken = repair.action_taken or ''
            except Invoice.DoesNotExist:
                pass

        # Build description
        if invoice_no and device_model:
            main_desc = f"Inv: {invoice_no} | Device: {device_model}"
        elif invoice_no:
            main_desc = f"Invoice {invoice_no}"
        else:
            main_desc = entry.description

        # Debit / Credit
        if line.debit > 0:
            running_balance += line.debit
            debit = line.debit
            credit = Decimal('0')
        else:
            running_balance -= line.credit
            debit = Decimal('0')
            credit = line.credit

        # Skip zero-amount rows (ghost entries)
        if debit == 0 and credit == 0:
            continue

        rows.append({
            'date': entry.date,
            'description': main_desc,
            'action': action_taken or '',
            'debit': debit,
            'credit': credit,
            'balance': running_balance,
            'invoice_id': invoice_id,
            'repair_job_id': repair_job_id,
        })

    rows.reverse()

    total_debit = sum(row['debit'] for row in rows)
    total_credit = sum(row['credit'] for row in rows)
    closing_balance = running_balance

    # Print mode
    if is_print:
        company = CompanyProfile.get_instance()
        context = {
            'customer': customer,
            'statement_rows': rows,
            'opening_balance': opening_balance,
            'closing_balance': closing_balance,
            'total_debit': total_debit,
            'total_credit': total_credit,
            'date_from': date_from,
            'date_to': date_to,
            'txn_type': txn_type,
            'search': search,
            'company': company,
            'logo_exists': bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name)),
        }
        return render(request, 'customer/statement_print.html', context)

    # Pagination
    paginator = Paginator(rows, 10)
    page = request.GET.get('page', 1)
    try:
        rows_page = paginator.page(page)
    except (PageNotAnInteger, EmptyPage):
        rows_page = paginator.page(1)

    context = {
        'customer': customer,
        'statement_rows': rows_page,
        'page_obj': rows_page,
        'opening_balance': opening_balance,
        'closing_balance': closing_balance,
        'total_debit': total_debit,
        'total_credit': total_credit,
        'date_from': date_from,
        'date_to': date_to,
        'txn_type': txn_type,
        'search': search,
    }

    if is_htmx(request):
        return render(request, 'customer/partials/statement_table.html', context)
    return render(request, 'customer/statement.html', context)


# ============================================================
# STATEMENT – EXCEL EXPORT
# ============================================================
def customer_statement_excel(request, customer):
    if openpyxl is None:
        return toast_only_response(
            {'level': 'danger', 'message': 'Openpyxl library is not installed.'},
            status=400
        )

    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '')

    lines = LedgerLine.objects.filter(contact=customer) \
        .exclude(ledger_entry__entry_type='opening') \
        .select_related('ledger_entry') \
        .order_by('ledger_entry__date', 'ledger_entry__id')

    if date_from:
        lines = lines.filter(ledger_entry__date__gte=date_from)
    if date_to:
        lines = lines.filter(ledger_entry__date__lte=date_to)
    if txn_type == 'invoice':
        lines = lines.filter(ledger_entry__entry_type='sales')
    elif txn_type == 'payment':
        lines = lines.filter(ledger_entry__entry_type='payment')
    elif txn_type == 'journal':
        lines = lines.filter(ledger_entry__entry_type='journal')
    if search:
        lines = lines.filter(ledger_entry__description__icontains=search)

    opening = customer.opening_balance
    running_balance = opening

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Statement"

    # Styles
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    center_align = Alignment(horizontal='center', vertical='center')
    money_format = '#,##0.00'

    company = CompanyProfile.get_instance()
    ws.merge_cells('A1:F1')
    ws['A1'] = company.name
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A2:F2')
    ws['A2'] = f"Customer Statement – {customer.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws.merge_cells('A3:F3')
    ws['A3'] = f"Period: {date_from if date_from else 'Start'} to {date_to if date_to else 'Today'}"
    ws['A3'].alignment = Alignment(horizontal="center")

    # Headers with Action column
    headers = ['Date', 'Description', 'Action', 'Debit (₹)', 'Credit (₹)', 'Balance (₹)']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=5, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border

    row = 6
    ws.cell(row=row, column=1, value="")
    ws.cell(row=row, column=2, value="Opening Balance")
    ws.cell(row=row, column=3, value="")
    ws.cell(row=row, column=4, value="")
    ws.cell(row=row, column=5, value="")
    ws.cell(row=row, column=6, value=float(opening))
    ws.cell(row=row, column=6).number_format = money_format
    for col in range(1, 7):
        ws.cell(row=row, column=col).border = thin_border
    row += 1

    for line in lines:
        entry = line.ledger_entry

        invoice = None
        repair = None
        invoice_no = None
        device_model = None
        action_taken = None

        if entry.entry_type == 'sales' and entry.reference_id:
            try:
                invoice = Invoice.objects.get(pk=entry.reference_id)
                invoice_no = invoice.invoice_number
                repair = RepairJob.objects.filter(invoice=invoice).first()
                if repair:
                    device_model = repair.device_model
                    action_taken = repair.action_taken or ''
            except Invoice.DoesNotExist:
                pass

        desc = entry.description
        if invoice_no and device_model:
            desc = f"Inv: {invoice_no} | Device: {device_model}"
        elif invoice_no:
            desc = f"Invoice {invoice_no}"

        if line.debit > 0:
            running_balance += line.debit
            debit = float(line.debit)
            credit = ""
        else:
            running_balance -= line.credit
            debit = ""
            credit = float(line.credit)

        action = action_taken or ''
        if action and len(action) > 80:
            action = action[:80] + '...'

        ws.cell(row=row, column=1, value=entry.date.strftime("%d-%m-%Y"))
        ws.cell(row=row, column=2, value=desc)
        ws.cell(row=row, column=3, value=action)
        ws.cell(row=row, column=4, value=debit if debit else "")
        if debit:
            ws.cell(row=row, column=4).number_format = money_format
        ws.cell(row=row, column=5, value=credit if credit else "")
        if credit:
            ws.cell(row=row, column=5).number_format = money_format
        ws.cell(row=row, column=6, value=float(running_balance))
        ws.cell(row=row, column=6).number_format = money_format
        for col in range(1, 7):
            ws.cell(row=row, column=col).border = thin_border
        row += 1

    ws.cell(row=row, column=2, value="Closing Balance")
    ws.cell(row=row, column=6, value=float(running_balance))
    ws.cell(row=row, column=6).number_format = money_format
    for col in range(1, 7):
        ws.cell(row=row, column=col).border = thin_border
        ws.cell(row=row, column=col).font = Font(bold=True)

    ws.column_dimensions['A'].width = 15
    ws.column_dimensions['B'].width = 35
    ws.column_dimensions['C'].width = 40
    ws.column_dimensions['D'].width = 15
    ws.column_dimensions['E'].width = 15
    ws.column_dimensions['F'].width = 15
    ws.freeze_panes = 'A6'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="statement_{customer.name}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
    wb.save(response)
    return response


# ============================================================
# PROFILE 
# ============================================================

@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def profile(request):
    """Customer profile view – full page for GET, partial for HTMX."""
    customer = get_object_or_404(Contact, user=request.user)

    # ---- STATS ----
    invoices = Invoice.objects.filter(customer=customer)
    repairs = RepairJob.objects.filter(customer=customer)
    payments = Payment.objects.filter(contact=customer, direction='received')

    total_invoices = invoices.count()
    total_repairs = repairs.count()
    total_payments = payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')

    # ---- PROFILE COMPLETION ----
    fields = [customer.name, customer.phone, customer.address, customer.state, customer.gstin, customer.email]
    filled_count = sum(1 for f in fields if f)
    profile_completion = int((filled_count / len(fields)) * 100) if fields else 0

    # ---- RECENT ACTIVITIES ----
    last_invoice = invoices.order_by('-date').first()
    last_repair = repairs.order_by('-date_in').first()

    context = {
        'customer': customer,
        'total_invoices': total_invoices,
        'total_repairs': total_repairs,
        'total_payments': total_payments,
        'profile_completion': profile_completion,
        'last_invoice': last_invoice,
        'last_repair': last_repair,
    }

    # HTMX Request → सिर्फ Partial Content Return करें
    if is_htmx(request):
        return render(request, 'customer/partials/profile_content.html', context)

    # Normal Request → Full Page Return करें
    return render(request, 'customer/profile.html', context)


# ============================================================
# PROFILE UPDATE VIEW 
# ============================================================

@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_profile')
def profile_update(request):
    """
    Update customer profile with HTMX support.
    After successful update, returns profile_content partial with updated stats and profile completion.
    """
    customer = get_object_or_404(Contact, user=request.user)

    if request.method == 'POST':
        form = CustomerProfileForm(request.POST, instance=customer)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated successfully.")

            if is_htmx(request):
                # Recalculate stats and profile completion after update
                invoices = Invoice.objects.filter(customer=customer)
                repairs = RepairJob.objects.filter(customer=customer)
                payments = Payment.objects.filter(contact=customer, direction='received')

                total_invoices = invoices.count()
                total_repairs = repairs.count()
                total_payments = payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')

                fields = [customer.name, customer.phone, customer.address,
                          customer.state, customer.gstin, customer.email]
                filled_count = sum(1 for f in fields if f)
                profile_completion = int((filled_count / len(fields)) * 100) if fields else 0

                last_invoice = invoices.order_by('-date').first()
                last_repair = repairs.order_by('-date_in').first()

                context = {
                    'customer': customer,
                    'total_invoices': total_invoices,
                    'total_repairs': total_repairs,
                    'total_payments': total_payments,
                    'profile_completion': profile_completion,
                    'last_invoice': last_invoice,
                    'last_repair': last_repair,
                }

                return htmx_response(
                    request,
                    'customer/partials/profile_content.html',
                    context=context,
                    toast={'level': 'success', 'message': 'Profile updated successfully.'}
                )
            return redirect_to_customer('customer_profile')
        else:
            # Form invalid – return the edit form partial
            if is_htmx(request):
                return render(request, 'customer/profile_edit.html', {'form': form, 'customer': customer})
    else:
        form = CustomerProfileForm(instance=customer)

    # GET request – render the edit page (full page)
    return render(request, 'customer/profile_edit.html', {'form': form, 'customer': customer})


# ============================================================
# PASSWORD CHANGE VIEW 
# ============================================================

@login_required
@handle_errors(default_redirect='customer:customer_profile')
def customer_password_change(request):
    """
    Customer Password Change View – HTMX Support with Toast
    """
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)  # Keep user logged in
            messages.success(request, "Your password has been changed successfully!")

            if is_htmx(request):
                # Success – Return profile content with toast
                # Re-fetch customer and stats
                from ..models import Contact, Invoice, RepairJob, Payment
                from decimal import Decimal
                from django.db.models import Sum

                customer = get_object_or_404(Contact, user=request.user)
                invoices = Invoice.objects.filter(customer=customer)
                repairs = RepairJob.objects.filter(customer=customer)
                payments = Payment.objects.filter(contact=customer, direction='received')

                total_invoices = invoices.count()
                total_repairs = repairs.count()
                total_payments = payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')

                # Profile Completion (optional)
                fields = [customer.name, customer.phone, customer.address,
                          customer.state, customer.gstin, customer.email]
                filled_count = sum(1 for f in fields if f)
                profile_completion = int((filled_count / len(fields)) * 100) if fields else 0

                last_invoice = invoices.order_by('-date').first()
                last_repair = repairs.order_by('-date_in').first()

                context = {
                    'customer': customer,
                    'total_invoices': total_invoices,
                    'total_repairs': total_repairs,
                    'total_payments': total_payments,
                    'profile_completion': profile_completion,
                    'last_invoice': last_invoice,
                    'last_repair': last_repair,
                }

                return htmx_response(
                    request,
                    'customer/partials/profile_content.html',
                    context=context,
                    toast={'level': 'success', 'message': 'Password changed successfully!', 'title': 'Security Updated'}
                )
            return redirect('customer:customer_profile')
        else:
            # Form invalid – return partial with errors
            if is_htmx(request):
                return render(request, 'customer/partials/_password_change_form.html', {'form': form}, status=400)
    else:
        # GET request – show empty form
        form = PasswordChangeForm(request.user)

    if is_htmx(request):
        return render(request, 'customer/partials/_password_change_form.html', {'form': form})
    return render(request, 'customer/password_change.html', {'form': form})


# ============================================================
# CUSTOMER REPAIR CREATE / UPDATE / DELETE
# ============================================================

# ---------- 1. REPAIR CREATE (Customer) ----------
@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs', htmx_template='customer/repair_create.html')
def repair_create(request):
    customer = get_object_or_404(Contact, user=request.user)

    if request.method == 'POST':
        form = CustomerRepairForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                job = form.save(commit=False)
                job.customer = customer
                job.status = 'pending'
                job.date_in = timezone.now().date()
                job.save()

                send_notification_to_staff(
                    title=f"New Repair Request: {job.job_number}",
                    message=f"{customer.name} submitted a repair for {job.device_model}",
                    link=reverse('accounting:repair_detail', args=[job.pk]),
                    notif_type='warning',
                    category='repairs',
                    send_email=True
                )

                if is_htmx(request):
                    return htmx_response(
                        request,
                        'customer/partials/repair_list_table.html',
                        context={'repairs': RepairJob.objects.filter(customer=customer).order_by('-date_in')[:10]},
                        toast={'level': 'success', 'message': f'Repair job {job.job_number} created.'}
                    )
                messages.success(request, f"Repair job {job.job_number} created successfully.")
                return redirect_to_customer('customer_repairs')
        else:
            if is_htmx(request):
                return render(request, 'customer/repair_create.html', {'form': form})
    else:
        form = CustomerRepairForm()

    return render(request, 'customer/repair_create.html', {'form': form})


# ---------- 2. REPAIR UPDATE (Customer) ----------
@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs', htmx_template='customer/repair_edit.html')
def repair_update(request, pk):
    customer = get_object_or_404(Contact, user=request.user)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    if repair.status not in ['pending', 'diagnosis']:
        messages.error(request, "This repair job has already been processed and cannot be edited.")
        return redirect_to_customer('customer_repair_detail', pk=repair.pk)

    if request.method == 'POST':
        form = CustomerRepairForm(request.POST, instance=repair)
        if form.is_valid():
            form.save()
            messages.success(request, f"Repair job {repair.job_number} updated.")
            
            send_notification_to_staff(
                title=f"Repair Updated by Customer: {repair.job_number}",
                message=f"{customer.name} has updated repair for {repair.device_model}",
                link=reverse('accounting:repair_detail', args=[repair.pk]),
                notif_type='info',
                category='repairs',
                send_email=False
            )
            
            if is_htmx(request):
                return htmx_response(
                    request,
                    'customer/repair_detail.html',
                    context={'repair': repair, 'parts': repair.parts.all()},
                    toast={'level': 'success', 'message': f'Repair job {repair.job_number} updated.'}
                )
            return redirect_to_customer('customer_repair_detail', pk=repair.pk)
        else:
            if is_htmx(request):
                return render(request, 'customer/repair_edit.html', {'form': form, 'repair': repair})
    else:
        form = CustomerRepairForm(instance=repair)

    return render(request, 'customer/repair_edit.html', {'form': form, 'repair': repair})


# ---------- 3. REPAIR DELETE (Customer) ----------
@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def repair_delete(request, pk):
    customer = get_object_or_404(Contact, user=request.user)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    if repair.status not in ['pending', 'diagnosis']:
        messages.error(request, "This repair job has already been processed and cannot be deleted.")
        if is_htmx(request):
            return toast_only_response(
                {'level': 'danger', 'message': 'Cannot delete processed repair.'},
                status=400
            )
        return redirect_to_customer('customer_repair_detail', pk=repair.pk)

    if request.method == 'POST':
        job_number = repair.job_number
        device_model = repair.device_model
        
        repair.delete()
        messages.success(request, f"Repair job {job_number} deleted.")
        
        send_notification_to_staff(
            title=f"Repair Deleted by Customer: {job_number}",
            message=f"{customer.name} has deleted repair for {device_model}",
            link=reverse('accounting:repair_list'),
            notif_type='warning',
            category='repairs',
            send_email=False
        )
        
        if is_htmx(request):
            return htmx_response(
                request,
                'customer/partials/repair_list_table.html',
                context={'repairs': RepairJob.objects.filter(customer=customer).order_by('-date_in')[:10]},
                toast={'level': 'success', 'message': f'Repair job {job_number} deleted.'}
            )
        return redirect_to_customer('customer_repairs')

    return render(request, 'customer/repair_confirm_delete.html', {'repair': repair})


# ============================================================
# ESTIMATE APPROVAL / HOLD / REJECT
# ============================================================

# ---------- 4. ESTIMATE APPROVE ----------
@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def repair_estimate_approve(request, pk):
    customer = get_object_or_404(Contact, user=request.user)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    if repair.estimate_status == 'approved':
        messages.warning(request, "This estimate has already been approved.")
        return redirect_to_customer('customer_repair_detail', pk=repair.pk)

    with transaction.atomic():
        repair.estimate_status = 'approved'
        repair.estimate_approved_at = timezone.now()
        repair.estimate_approved_by = request.user
        if repair.estimated_cost and repair.estimated_cost > 0:
            repair.status = 'repairing'
        repair.save()

        send_notification_to_staff(
            title=f"Estimate Approved: {repair.job_number}",
            message=f"{customer.name} approved the estimate for {repair.device_model}",
            link=reverse('accounting:repair_detail', args=[repair.pk]),
            notif_type='success',
            category='repairs',
            send_email=True
        )

        messages.success(request, f"Estimate for job {repair.job_number} approved.")

    if is_htmx(request):
        return htmx_response(
            request,
            'customer/repair_detail.html',
            context={'repair': repair, 'parts': repair.parts.all()},
            toast={'level': 'success', 'message': 'Estimate approved.'}
        )
    return redirect_to_customer('customer_repair_detail', pk=repair.pk)


# ---------- 5. ESTIMATE HOLD ----------
@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def repair_estimate_hold(request, pk):
    customer = get_object_or_404(Contact, user=request.user)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    if repair.estimate_status == 'approved':
        messages.warning(request, "This estimate is already approved and cannot be held.")
        return redirect_to_customer('customer_repair_detail', pk=repair.pk)

    repair.estimate_status = 'on_hold'
    repair.save()

    send_notification_to_staff(
        title=f"Estimate On Hold: {repair.job_number}",
        message=f"{customer.name} put the estimate on hold for {repair.device_model}",
        link=reverse('accounting:repair_detail', args=[repair.pk]),
        notif_type='warning',
        category='repairs',
        send_email=False
    )

    messages.info(request, f"Estimate for job {repair.job_number} is on hold.")

    if is_htmx(request):
        return htmx_response(
            request,
            'customer/repair_detail.html',
            context={'repair': repair, 'parts': repair.parts.all()},
            toast={'level': 'info', 'message': 'Estimate put on hold.'}
        )
    return redirect_to_customer('customer_repair_detail', pk=repair.pk)


# ---------- 6. ESTIMATE REJECT ----------
@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def repair_estimate_reject(request, pk):
    customer = get_object_or_404(Contact, user=request.user)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    if repair.estimate_status == 'approved':
        messages.warning(request, "This estimate is already approved and cannot be rejected.")
        return redirect_to_customer('customer_repair_detail', pk=repair.pk)

    repair.estimate_status = 'rejected'
    repair.save()

    send_notification_to_staff(
        title=f"Estimate Rejected: {repair.job_number}",
        message=f"{customer.name} rejected the estimate for {repair.device_model}",
        link=reverse('accounting:repair_detail', args=[repair.pk]),
        notif_type='error',
        category='repairs',
        send_email=False
    )

    messages.warning(request, f"Estimate for job {repair.job_number} has been rejected.")

    if is_htmx(request):
        return htmx_response(
            request,
            'customer/repair_detail.html',
            context={'repair': repair, 'parts': repair.parts.all()},
            toast={'level': 'warning', 'message': 'Estimate rejected.'}
        )
    return redirect_to_customer('customer_repair_detail', pk=repair.pk)


# ============================================================
# NOTIFICATIONS (Customer Specific)
# ============================================================

@login_required
def notification_list(request):
    """Customer notification list page."""
    notifications = request.user.notifications.all().order_by('-created_at')
    unread_count = request.user.notifications.filter(is_read=False).count()
    paginator = Paginator(notifications, 20)
    page = request.GET.get('page', 1)
    try:
        page_obj = paginator.page(page)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)
    context = {
        'notifications': page_obj,
        'page_obj': page_obj,
        'unread_count': unread_count,
    }
    if is_htmx(request):
        return render(request, 'customer/partials/_notification_items.html', context)
    return render(request, 'customer/notification_list.html', context)


@login_required
def notification_mark_all_read(request):
    """Mark all notifications as read for the current customer."""
    if request.method != 'POST':
        return HttpResponse("Method not allowed", status=405)

    count = request.user.notifications.filter(is_read=False).update(is_read=True)
    notifications = request.user.notifications.all().order_by('-created_at')[:20]
    unread_count = request.user.notifications.filter(is_read=False).count()

    if is_htmx(request):
        return render(request, 'notifications/partials/_notification_items.html', {
            'notifications': notifications,
            'unread_count': unread_count,
            'page_obj': None,
        })

    messages.success(request, f"{count} notifications marked as read.")
    return redirect('customer:customer_notifications')


@login_required
def notification_mark_read(request, pk):
    """Mark a single notification as read."""
    if request.method != 'POST':
        return HttpResponse("Method not allowed", status=405)

    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notification.is_read = True
    notification.save()

    notifications = request.user.notifications.all().order_by('-created_at')[:20]
    unread_count = request.user.notifications.filter(is_read=False).count()

    if is_htmx(request):
        return render(request, 'notifications/partials/_notification_items.html', {
            'notifications': notifications,
            'unread_count': unread_count,
            'page_obj': None,
        })

    return HttpResponse("Marked as read", status=200)


@login_required
def notification_delete(request, pk):
    """Delete a single notification."""
    if request.method != 'DELETE':
        return HttpResponse("Method not allowed", status=405)

    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notification.delete()

    notifications = request.user.notifications.all().order_by('-created_at')[:20]
    unread_count = request.user.notifications.filter(is_read=False).count()

    if is_htmx(request):
        return render(request, 'notifications/partials/_notification_items.html', {
            'notifications': notifications,
            'unread_count': unread_count,
            'page_obj': None,
        })

    return HttpResponse("Deleted", status=200)


@login_required
def notification_delete_all(request):
    """Delete all notifications for the current customer."""
    if request.method != 'DELETE':
        return HttpResponse("Method not allowed", status=405)

    count = request.user.notifications.count()
    request.user.notifications.all().delete()

    if is_htmx(request):
        return render(request, 'notifications/partials/_notification_items.html', {
            'notifications': [],
            'unread_count': 0,
            'page_obj': None,
        })

    messages.success(request, f"{count} notifications deleted.")
    return redirect('customer:customer_notifications')


@login_required
def notification_dropdown(request):
    """Customer portal notification dropdown (HTMX partial)."""
    try:
        notifications = request.user.notifications.all()[:10]
        unread_count = request.user.notifications.filter(is_read=False).count()
        return render(request, 'customer/partials/dropdown.html', {
            'notifications': notifications,
            'unread_count': unread_count,
        })
    except Exception as e:
        logger.error(f"Customer notification dropdown error: {e}")
        return HttpResponse(
            '<div class="dropdown-item text-danger">Error loading notifications</div>',
            status=500
        )


@login_required
def unread_count_text(request):
    """Customer portal unread count as plain text."""
    try:
        count = request.user.notifications.filter(is_read=False).count()
        return HttpResponse(str(count))
    except Exception:
        return HttpResponse("0")
    
    
# ============================================================
# EMAIL CHANGE (Professional OTP-based)
# ============================================================

@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_profile')
def email_change_request(request):
    """Step 1: User requests email change by entering new email."""
    customer = get_object_or_404(Contact, user=request.user)

    if request.method == 'POST':
        form = EmailChangeRequestForm(request.user, request.POST)
        if form.is_valid():
            new_email = form.cleaned_data['new_email']
            # Send OTP to the new email
            success = create_and_send_otp(request.user, new_email, 'change_email')
            if success:
                # Store new email in session for verification
                request.session['pending_new_email'] = new_email
                request.session['otp_purpose'] = 'change_email'
                request.session['pending_user_id'] = request.user.id
                messages.success(request, f"OTP sent to {new_email}. Please verify to complete email change.")
                if is_htmx(request):
                    return htmx_response(
                        request,
                        'customer/partials/email_change_otp.html',
                        context={'email': new_email},
                        toast={'level': 'success', 'message': 'OTP sent to new email.'}
                    )
                return redirect('customer:email_change_verify')
            else:
                messages.error(request, "Failed to send OTP. Please try again.")
        else:
            if is_htmx(request):
                return render(request, 'customer/partials/email_change_form.html', {'form': form}, status=400)
    else:
        form = EmailChangeRequestForm(request.user)

    if is_htmx(request):
        return render(request, 'customer/partials/email_change_form.html', {'form': form})
    return render(request, 'customer/email_change.html', {'form': form, 'customer': customer})


@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_profile')
def email_change_verify(request):
    """Step 2: Verify OTP sent to new email, then update email."""
    user = request.user
    customer = get_object_or_404(Contact, user=user)

    new_email = request.session.get('pending_new_email')
    purpose = request.session.get('otp_purpose')

    if not new_email or purpose != 'change_email':
        messages.error(request, "Invalid session. Please request email change again.")
        return redirect('customer:email_change_request')

    if request.method == 'POST':
        otp = request.POST.get('otp', '').strip()
        if not otp or len(otp) != 6:
            messages.error(request, "Please enter a valid 6-digit OTP.")
            if is_htmx(request):
                response = render(request, 'customer/partials/email_change_otp.html', {'email': new_email}, status=400)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'danger',
                        'message': 'Please enter a valid 6-digit OTP.'
                    }
                })
                return response
            return render(request, 'customer/email_change_otp.html', {'email': new_email})

        # Verify OTP
        verified_user = verify_otp(new_email, otp, 'change_email')
        if verified_user and verified_user.id == user.id:
            with transaction.atomic():
                # Update User email
                user.email = new_email
                user.save()
                # Update Contact email
                customer.email = new_email
                customer.save()

                # Clean session
                request.session.pop('pending_new_email', None)
                request.session.pop('otp_purpose', None)

                # Invalidate old OTPs for this user (optional)
                EmailOTP.objects.filter(user=user, purpose='change_email').delete()

                # Send confirmation to new email (and alert to old if possible)
                try:
                    send_mail(
                        subject="Your email has been changed",
                        message=f"Your A1 Computer Solutions account email was changed to {new_email}. If you didn't request this, please contact support immediately.",
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[new_email],
                        fail_silently=True,
                    )
                except:
                    pass

                messages.success(request, "Your email has been updated successfully! Please login again.")
                # Logout user for security
                logout(request)

                if is_htmx(request):
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:login')
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'success',
                            'message': 'Email changed! Please login with your new email.',
                            'title': 'Security Updated'
                        }
                    })
                    return response
                return redirect('accounting:login')
        else:
            messages.error(request, "Invalid or expired OTP. Please try again.")
            if is_htmx(request):
                response = render(request, 'customer/partials/email_change_otp.html', {'email': new_email}, status=400)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'danger',
                        'message': 'Invalid or expired OTP. Please try again.'
                    }
                })
                return response
            return render(request, 'customer/email_change_otp.html', {'email': new_email})

    return render(request, 'customer/email_change_otp.html', {'email': new_email})