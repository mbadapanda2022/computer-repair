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
from django.views.decorators.http import require_http_methods
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
from ..utils.notification_helpers import send_notification_to_staff, send_notification_sse
from ..utils.otp_helpers import create_and_send_otp, verify_otp
from .utils import is_htmx, htmx_response, redirect_to_customer, redirect_to_staff, toast_only_response
from .statements import _get_combined_opening_balances, _build_combined_rows
from ..decorators import handle_errors


# Excel Export
try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
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
# REAL-TIME VALIDATION FOR CUSTOMER PROFILE (HTMX)
# ============================================================

@login_required
@require_http_methods(["GET"])
def validate_customer_profile_field(request):
    """
    Real-time validation for Customer Profile fields (HTMX).
    Returns error HTML for the specific field.
    """
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    value = request.GET.get(field_name, '').strip()
    
    # Get current customer instance for uniqueness check
    try:
        customer = request.user.customer_contact
    except Contact.DoesNotExist:
        return HttpResponse("")

    # Build data dict with all fields
    data = {}
    for f in CustomerProfileForm.base_fields:
        data[f] = request.GET.get(f, '')

    # Create form instance with current data and instance for uniqueness checks
    form = CustomerProfileForm(data, instance=customer)
    form.is_valid()  # Triggers validation

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
    try:
        customer = get_object_or_404(Contact, user=request.user)
        
        today = date.today()
        invoice_data = []
        payment_data = []
        labels = []
        
        # Last 7 days
        for i in range(6, -1, -1):
            d = today - timedelta(days=i)
            labels.append(d.strftime('%d %b'))
            
            inv_total = Invoice.objects.filter(customer=customer, date=d).aggregate(
                total=Sum('grand_total')
            )['total'] or Decimal('0')
            invoice_data.append(float(inv_total))
            
            pay_total = Payment.objects.filter(
                contact=customer, direction='received', date=d
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')
            payment_data.append(float(pay_total))
        
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
        return JsonResponse({'error': str(e)}, status=500)


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

    # Month stats
    month_invoices = invoices.filter(date__gte=month_start)
    month_sales = month_invoices.aggregate(Sum('grand_total'))['grand_total__sum'] or Decimal('0')
    month_payments = payments.filter(date__gte=month_start).aggregate(Sum('amount'))['amount__sum'] or Decimal('0')

    context = {
        'customer': customer,
        'total_invoices': invoices.count(),
        'total_paid': invoices.aggregate(Sum('paid_amount'))['paid_amount__sum'] or Decimal('0'),
        'total_due': invoices.aggregate(Sum('balance_due'))['balance_due__sum'] or Decimal('0'),
        'paid_invoices': invoices.filter(payment_status='paid').count(),
        'unpaid_invoices': invoices.filter(payment_status__in=['unpaid', 'partial']).count(),
        'pending_repairs': repairs.filter(status__in=['pending', 'received', 'diagnosis', 'repairing']).count(),
        'ready_repairs': repairs.filter(status='ready').count(),
        'completed_repairs': repairs.filter(status='delivered').count(),
        'total_repairs': repairs.count(),
        'total_payments': payments.aggregate(Sum('amount'))['amount__sum'] or Decimal('0'),
        'month_sales': month_sales,
        'month_payments': month_payments,
        'unread_count': request.user.notifications.filter(is_read=False).count(),
    }
    return render(request, 'customer/partials/dashboard_stats.html', context)


@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def invoice_list(request):
    """Customer invoice list with filters, pagination, HTMX, Print, Excel."""
    customer = get_object_or_404(Contact, user=request.user)

    status_filter = request.GET.get('status', '')
    search = request.GET.get('search', '').strip()
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    is_print = request.GET.get('print') == '1'
    is_excel = request.GET.get('excel') == '1'

    invoices = Invoice.objects.filter(customer=customer).order_by('-date')

    if status_filter:
        invoices = invoices.filter(payment_status=status_filter)
    if search:
        invoices = invoices.filter(
            Q(invoice_number__icontains=search)
        )
    if date_from:
        invoices = invoices.filter(date__gte=date_from)
    if date_to:
        invoices = invoices.filter(date__lte=date_to)

    # Excel export
    if is_excel:
        return customer_invoices_excel(request, invoices, customer)

    # Summary stats
    total_invoiced = invoices.aggregate(t=Sum('grand_total'))['t'] or Decimal('0')
    total_paid = invoices.aggregate(t=Sum('paid_amount'))['t'] or Decimal('0')
    total_due = invoices.aggregate(t=Sum('balance_due'))['t'] or Decimal('0')

    # Print mode
    if is_print:
        company = CompanyProfile.get_instance()
        context = {
            'customer': customer,
            'invoices': invoices,
            'total_invoiced': total_invoiced,
            'total_paid': total_paid,
            'total_due': total_due,
            'company': company,
            'logo_exists': bool(company.logo and company.logo.name),
            'status_filter': status_filter,
            'search': search,
            'date_from': date_from,
            'date_to': date_to,
        }
        return render(request, 'customer/invoice_list_print.html', context)

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
        'status_filter': status_filter,
        'search': search,
        'date_from': date_from,
        'date_to': date_to,
        'total_invoiced': total_invoiced,
        'total_paid': total_paid,
        'total_due': total_due,
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
            repair_job = RepairJob.objects.filter(invoice=invoice).first()
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
        repair_job = RepairJob.objects.filter(invoice=invoice).first()
    except RepairJob.DoesNotExist:
        pass
    
    # Get company profile for header/footer
    company = CompanyProfile.get_instance()
    
    # Check if logo exists
    logo_exists = bool(company.logo and company.logo.name)
    
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

    # Reset handling — clear filters
    reset = request.GET.get('reset')
    if reset:
        # For HTMX: render clean list directly
        if is_htmx(request):
            repairs = RepairJob.objects.filter(customer=customer).order_by('-date_in')
            paginator = Paginator(repairs, 10)
            page_obj = paginator.page(1)
            filtered_total = repairs.aggregate(total=Sum('final_amount'))['total'] or Decimal('0')
            context = {
                'repairs': page_obj,
                'page_obj': page_obj,
                'status_filter': '',
                'search': '',
                'date_from': '',
                'date_to': '',
                'total_count': paginator.count,
                'filtered_total': filtered_total,
                'repair_status_choices': RepairJob.STATUS_CHOICES,
                'customer': customer,
            }
            response = render(request, 'customer/partials/repair_list_table.html', context)
            # Update URL via HTMX push
            return response
        # Non-HTMX: redirect to clean URL
        return redirect('customer:customer_repairs')

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

    filtered_total = repairs.aggregate(total=Sum('final_amount'))['total'] or Decimal('0')

    # Print Mode 
    if is_print:
        company = CompanyProfile.get_instance()
        total_amount = repairs.aggregate(total=Sum('final_amount'))['total'] or Decimal('0')
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
            'total_amount': total_amount,
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
        'filtered_total': filtered_total,   
        'repair_status_choices': RepairJob.STATUS_CHOICES,
        'customer': customer,
    }

    if is_htmx(request):
        return render(request, 'customer/partials/repair_list_table.html', context)
    return render(request, 'customer/repairs.html', context)


# ============================================================
# CUSTOMER REPAIRS - EXCEL EXPORT
# ============================================================
@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def customer_repairs_excel(request):
    """Customer ki apni repairs ko Excel me export karein (filters ke saath)."""
    if openpyxl is None:
        return toast_only_response(
            {'level': 'danger', 'message': 'Openpyxl library is not installed.'},
            status=400
        )

    customer = get_object_or_404(Contact, user=request.user)

    # Same filters as repair_list
    status_filter = request.GET.get('status', '')
    search = request.GET.get('search', '').strip()
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    repairs = RepairJob.objects.filter(customer=customer).order_by('-date_in')
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

    company = CompanyProfile.get_instance()

    # ===== Workbook =====
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "My Repairs"

    # Styles
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
    center_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left_align = Alignment(horizontal='left', vertical='center', wrap_text=True)
    right_align = Alignment(horizontal='right', vertical='center')

    # 20 columns now (added Submitted, Received, Ready, Received By, Delivered By)
    TOTAL_COLS = 20
    last_col = get_column_letter(TOTAL_COLS)

    # Row 1 - Company Name
    ws.merge_cells(f'A1:{last_col}1')
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = title_font
    ws['A1'].fill = title_fill
    ws['A1'].alignment = center_align
    ws.row_dimensions[1].height = 30

    # Row 2 - Company Info
    ws.merge_cells(f'A2:{last_col}2')
    addr_parts = []
    if company.address: addr_parts.append(company.address)
    if company.phone: addr_parts.append(f"Phone: {company.phone}")
    if company.email: addr_parts.append(f"Email: {company.email}")
    if company.gstin: addr_parts.append(f"GSTIN: {company.gstin}")
    ws['A2'] = " | ".join(addr_parts)
    ws['A2'].font = Font(size=10, italic=True)
    ws['A2'].alignment = center_align
    ws.row_dimensions[2].height = 20

    # Row 3 - Title
    ws.merge_cells(f'A3:{last_col}3')
    ws['A3'] = f"MY REPAIR HISTORY — {customer.name}"
    ws['A3'].font = subtitle_font
    ws['A3'].fill = subtitle_fill
    ws['A3'].alignment = center_align
    ws.row_dimensions[3].height = 25

    # Row 4 - Filters
    ws.merge_cells(f'A4:{last_col}4')
    filter_parts = [f"Generated: {timezone.now().strftime('%d-%m-%Y %H:%M')}"]
    if search: filter_parts.append(f"Search: {search}")
    if status_filter: filter_parts.append(f"Status: {status_filter}")
    if date_from: filter_parts.append(f"From: {date_from}")
    if date_to: filter_parts.append(f"To: {date_to}")
    filter_parts.append(f"Total: {repairs.count()}")
    ws['A4'] = " | ".join(filter_parts)
    ws['A4'].font = Font(size=10, italic=True, color="555555")
    ws['A4'].alignment = center_align
    ws.row_dimensions[4].height = 20

    ws.row_dimensions[5].height = 5

    # Row 6 - Headers (20 columns)
    headers = [
        ('Job #', 14),
        ('Submitted', 13),
        ('Received', 13),
        ('Ready', 13),
        ('Delivered', 13),
        ('Device Model', 22),
        ('Serial #', 16),
        ('Issue', 30),
        ('Diagnosis', 30),
        ('Action Taken', 30),
        ('Status', 22),
        ('Estimate Status', 14),
        ('Estimated Cost (₹)', 16),
        ('Labour (₹)', 12),
        ('Parts (₹)', 12),
        ('Final Amount (₹)', 16),
        ('Received By', 14),
        ('Delivered By', 14),
        ('Recipient', 18),
        ('Invoice #', 14),
    ]

    for col_num, (header, width) in enumerate(headers, 1):
        cell = ws.cell(row=6, column=col_num, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = border_header
        ws.column_dimensions[get_column_letter(col_num)].width = width

    ws.row_dimensions[6].height = 30

    # Data rows
    row_num = 7
    total_est = Decimal('0')
    total_labour = Decimal('0')
    total_parts = Decimal('0')
    total_final = Decimal('0')

    for repair in repairs:
        parts_total = repair.parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')

        row_data = [
            repair.job_number,
            # ---- Timeline (4 dates) ----
            repair.submitted_at.strftime('%d-%m-%Y') if repair.submitted_at else (
                repair.date_in.strftime('%d-%m-%Y') if repair.date_in else ''
            ),
            repair.received_at.strftime('%d-%m-%Y') if repair.received_at else '',
            repair.ready_at.strftime('%d-%m-%Y') if repair.ready_at else '',
            repair.delivery_date.strftime('%d-%m-%Y') if repair.delivery_date else '',
            # ---- Device details ----
            repair.device_model or '',
            repair.serial_number or '',
            repair.issue_description or '',
            repair.diagnosis_report or '',
            repair.action_taken or '',
            # ---- Status ----
            repair.get_status_display(),
            repair.get_estimate_status_display() if repair.estimate_status else 'No Estimate',
            # ---- Amounts ----
            float(repair.estimated_cost) if repair.estimated_cost else 0,
            float(repair.labour_charge or 0),
            float(parts_total),
            float(repair.final_amount or 0),
            # ---- Personnel ----
            repair.received_by or '',
            repair.delivered_by or '',
            repair.delivered_to_name or '',
            # ---- Invoice ----
            repair.invoice.invoice_number if repair.invoice else '',
        ]

        for col_num, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_num, column=col_num, value=value)
            cell.border = border_all
            cell.alignment = left_align

        # Money columns: 13, 14, 15, 16
        for col_idx in [13, 14, 15, 16]:
            ws.cell(row=row_num, column=col_idx).alignment = right_align
            ws.cell(row=row_num, column=col_idx).number_format = money_format

        # Center align: Job#, all dates (2,3,4,5), status (11,12), invoice (20)
        for col_idx in [1, 2, 3, 4, 5, 11, 12, 20]:
            ws.cell(row=row_num, column=col_idx).alignment = center_align

        # Accumulate totals
        total_est += Decimal(str(repair.estimated_cost or 0))
        total_labour += Decimal(str(repair.labour_charge or 0))
        total_parts += parts_total
        total_final += Decimal(str(repair.final_amount or 0))

        # Alternate row fill
        if row_num % 2 == 0:
            alt = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
            for c in range(1, TOTAL_COLS + 1):
                ws.cell(row=row_num, column=c).fill = alt

        row_num += 1

    # ===== Grand Total Row =====
    # Merge cols 1-12 for label
    ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=12)
    total_label = ws.cell(row=row_num, column=1, value="GRAND TOTAL")
    total_label.font = total_font
    total_label.fill = total_fill
    total_label.alignment = right_align
    total_label.border = border_header

    # Fill merged region
    for col_idx in range(2, 13):
        c = ws.cell(row=row_num, column=col_idx)
        c.fill = total_fill
        c.border = border_header

    # Money totals: cols 13, 14, 15, 16
    for col_idx, val in [(13, total_est), (14, total_labour), (15, total_parts), (16, total_final)]:
        c = ws.cell(row=row_num, column=col_idx, value=float(val))
        c.font = total_font
        c.fill = total_fill
        c.alignment = right_align
        c.number_format = money_format
        c.border = border_header

    # Remaining cols 17-20
    for col_idx in [17, 18, 19, 20]:
        c = ws.cell(row=row_num, column=col_idx, value="")
        c.fill = total_fill
        c.border = border_header

    ws.row_dimensions[row_num].height = 22

    # ===== Footer =====
    footer_row = row_num + 2
    ws.merge_cells(start_row=footer_row, start_column=1, end_row=footer_row, end_column=TOTAL_COLS)
    footer_text = f"Auto-generated by {company.name or 'A1 Computer Solutions'} on {timezone.now().strftime('%d-%m-%Y %H:%M')}."
    ws.cell(row=footer_row, column=1, value=footer_text).font = Font(size=9, italic=True, color="777777")
    ws.cell(row=footer_row, column=1).alignment = center_align

    ws.freeze_panes = 'A7'
    ws.auto_filter.ref = f"A6:{last_col}6"

    ws.page_setup.orientation = 'landscape'
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    # Response
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"my_repairs_{timezone.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response

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
            Q(allocations__invoice__invoice_number__icontains=search) |
            Q(reference__icontains=search) |
            Q(upi_ref__icontains=search) |
            Q(description__icontains=search)
        ).distinct()

    # Prefetch for performance (n+1 query avoid karne ke liye)
    payments = payments.prefetch_related('allocations__invoice')
    
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
# PAYMENTS - EXCEL EXPORT (allocations + advance support)
# ============================================================
def customer_payments_excel(request, payments, customer):
    """Export payment list to Excel. Handles advance, partial, discount payments."""
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
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A2:G2')
    ws['A2'] = f"Payment History – {customer.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws.merge_cells('A3:G3')
    ws['A3'] = f"Generated: {datetime.now().strftime('%d-%m-%Y %H:%M')}"
    ws['A3'].alignment = Alignment(horizontal="center")

    headers = ['Date', 'Invoice / Reference', 'Amount (₹)', 'Method', 'UPI Ref', 'Reference', 'Description']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=5, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border

    row = 6
    total = Decimal('0')
    for payment in payments:
        # ============================================================
        # BUILD INVOICE REFERENCE
        # Case 1: Payment has allocations -> list all invoice numbers
        # Case 2: Advance payment -> "Advance"
        # Case 3: No allocations + not advance -> "Direct"
        # ============================================================
        allocations = list(payment.allocations.all())  # prefetched

        if allocations:
            invoice_parts = []
            for alloc in allocations:
                if alloc.invoice:
                    invoice_parts.append(alloc.invoice.invoice_number)
            invoice_display = ', '.join(invoice_parts) if invoice_parts else '—'
        elif payment.is_advance:
            invoice_display = 'Advance Payment'
        else:
            invoice_display = 'Direct Payment'

        # Discount indicator
        if payment.discount_amount and payment.discount_amount > 0:
            invoice_display += f' (Disc: ₹{payment.discount_amount})'

        total += payment.amount

        ws.cell(row=row, column=1, value=payment.date.strftime("%d-%m-%Y"))
        ws.cell(row=row, column=2, value=invoice_display)
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
    ws.column_dimensions['B'].width = 25
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
# INVOICES - EXCEL EXPORT
# ============================================================
def customer_invoices_excel(request, invoices, customer):
    """Export customer invoices to Excel with current filters."""
    if openpyxl is None:
        return toast_only_response(
            {'level': 'danger', 'message': 'Openpyxl library is not installed.'},
            status=400
        )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "My Invoices"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    center_align = Alignment(horizontal='center', vertical='center')
    money_format = '#,##0.00'

    company = CompanyProfile.get_instance()
    ws.merge_cells('A1:H1')
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A2:H2')
    ws['A2'] = f"My Invoices – {customer.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws.merge_cells('A3:H3')
    ws['A3'] = f"Generated: {datetime.now().strftime('%d-%m-%Y %H:%M')}"
    ws['A3'].alignment = Alignment(horizontal="center")

    headers = ['Invoice #', 'Date', 'Due Date', 'GST Type', 'Subtotal',
               'Tax', 'Grand Total', 'Status']
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=5, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border

    row = 6
    total_grand = Decimal('0')
    for inv in invoices:
        total_grand += inv.grand_total
        ws.cell(row=row, column=1, value=inv.invoice_number)
        ws.cell(row=row, column=2, value=inv.date.strftime("%d-%m-%Y"))
        ws.cell(row=row, column=3,
                value=inv.due_date.strftime("%d-%m-%Y") if inv.due_date else '')
        ws.cell(row=row, column=4, value=inv.get_gst_type_display())
        ws.cell(row=row, column=5, value=float(inv.subtotal)).number_format = money_format
        ws.cell(row=row, column=6, value=float(inv.tax_amount)).number_format = money_format
        ws.cell(row=row, column=7, value=float(inv.grand_total)).number_format = money_format
        ws.cell(row=row, column=8, value=inv.get_payment_status_display())
        for col in range(1, 9):
            ws.cell(row=row, column=col).border = thin_border
            if col in (5, 6, 7):
                ws.cell(row=row, column=col).alignment = Alignment(horizontal='right')
        row += 1

    # Total row
    ws.cell(row=row, column=1, value="Total").font = Font(bold=True)
    ws.cell(row=row, column=7, value=float(total_grand)).number_format = money_format
    ws.cell(row=row, column=7).font = Font(bold=True)
    for col in range(1, 9):
        ws.cell(row=row, column=col).border = thin_border

    widths = [18, 14, 14, 18, 14, 14, 16, 14]
    for idx, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = 'A6'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="invoices_{customer.name}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
    wb.save(response)
    return response


# ============================================================
# STATEMENT (With Filters, Pagination, Print, Excel)
# ============================================================

@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def statement(request):
    """
    Customer portal statement.

    For 'customer' type → shows receivable side only.
    For 'both' type      → shows receivable + payable (combined), with net position.

    Uses the same combined builder as the staff combined_statement view.
    """
    customer = get_object_or_404(Contact, user=request.user)

    # Reset handling
    if request.GET.get('reset'):
        return redirect('customer:customer_statement')

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '').strip()
    is_print = request.GET.get('print') == '1'
    is_excel = request.GET.get('excel') == '1'

    # Excel export
    if is_excel:
        return customer_statement_excel(request, customer)

    # Build combined rows (both sides)
    data = _build_combined_rows(
        customer,
        date_from or None,
        date_to or None,
        txn_type or None,
        search or None,
    )
    rows = data.pop('rows')

    # Which side to show in the simple (non-dual) view?
    # 'both'      -> dual columns
    # 'customer'  -> receivable only
    # 'vendor'    -> payable only
    show_dual = (customer.contact_type == 'both')
    show_payable_only = (customer.contact_type == 'vendor')

    if not show_dual:
        if show_payable_only:
            # Zero out receivable side for pure vendors
            data['opening_recv'] = Decimal('0')
            data['closing_recv'] = Decimal('0')
            data['total_recv_dr'] = Decimal('0')
            data['total_recv_cr'] = Decimal('0')
            data['net_position'] = -data['closing_pay']
        else:
            # Zero out payable side for pure customers
            data['opening_pay'] = Decimal('0')
            data['closing_pay'] = Decimal('0')
            data['total_pay_dr'] = Decimal('0')
            data['total_pay_cr'] = Decimal('0')
            data['net_position'] = data['closing_recv']

    # Pass to context
    context_extra = {'show_payable_only': show_payable_only}

    page_number = request.GET.get('page', 1)
    paginator = Paginator(rows, 10)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    company = CompanyProfile.get_instance()

    context = {
        'customer': customer,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
        'statement_rows': page_obj.object_list,
        'page_obj': page_obj,
        'date_from': date_from,
        'date_to': date_to,
        'txn_type': txn_type,
        'search': search,
        'show_dual': show_dual,
        'show_payable_only': show_payable_only,
        **data,
    }

    # Print mode
    if is_print:
        return render(request, 'customer/statement_print.html', context)

    # HTMX partial
    if is_htmx(request):
        return render(request, 'customer/partials/statement_table.html', context)

    return render(request, 'customer/statement.html', context)


# ============================================================
# STATEMENT – EXCEL EXPORT
# ============================================================

def customer_statement_excel(request, customer):
    """Export customer statement to Excel (combined view for 'both' type)."""
    if openpyxl is None:
        return toast_only_response(
            {'level': 'danger', 'message': 'Openpyxl library is not installed.'},
            status=400
        )

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '').strip()

    data = _build_combined_rows(
        customer,
        date_from or None,
        date_to or None,
        txn_type or None,
        search or None,
    )
    rows = data.pop('rows')

    show_dual = (customer.contact_type == 'both')
    show_payable_only = (customer.contact_type == 'vendor')

    if not show_dual:
        if show_payable_only:
            data['opening_recv'] = Decimal('0')
            data['closing_recv'] = Decimal('0')
            data['total_recv_dr'] = Decimal('0')
            data['total_recv_cr'] = Decimal('0')
            data['net_position'] = -data['closing_pay']
        else:
            data['opening_pay'] = Decimal('0')
            data['closing_pay'] = Decimal('0')
            data['total_pay_dr'] = Decimal('0')
            data['total_pay_cr'] = Decimal('0')
            data['net_position'] = data['closing_recv']

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Statement"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    green_fill = PatternFill(start_color="D9EAD3", end_color="D9EAD3", fill_type="solid")
    yellow_fill = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
    net_fill = PatternFill(start_color="E8F0FE", end_color="E8F0FE", fill_type="solid")

    thin = Side(style='thin', color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center', wrap_text=True)
    right = Alignment(horizontal='right', vertical='center')
    money_fmt = '#,##0.00'

    company = CompanyProfile.get_instance()
    total_cols = 9 if show_dual else 6
    last_col = get_column_letter(total_cols)

    ws.merge_cells(f'A1:{last_col}1')
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = Font(bold=True, size=14)
    ws['A1'].alignment = center

    ws.merge_cells(f'A2:{last_col}2')
    ws['A2'] = f"Statement – {customer.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws['A2'].alignment = center

    ws.merge_cells(f'A3:{last_col}3')
    ws['A3'] = f"Period: {date_from or 'Start'} to {date_to or 'Today'}"
    ws['A3'].alignment = center

    if show_dual:
        # 2-tier headers
        # Top-left merged block: A5:B6
        ws.merge_cells('A5:B6')
        ws['A5'] = 'Date / Particulars'
        ws['A5'].font = header_font
        ws['A5'].fill = header_fill
        ws['A5'].alignment = center
        ws['A5'].border = border
        ws['B5'].border = border
        ws['A6'].border = border
        ws['B6'].border = border

        # Receivable block: C5:E5
        ws.merge_cells('C5:E5')
        ws['C5'] = 'Receivable (You Owe Us)'
        ws['C5'].font = header_font
        ws['C5'].fill = green_fill
        ws['C5'].alignment = center
        for col in ['C5', 'D5', 'E5']:
            ws[col].fill = green_fill
            ws[col].border = border

        # Payable block: F5:H5
        ws.merge_cells('F5:H5')
        ws['F5'] = 'Payable (We Owe You)'
        ws['F5'].font = header_font
        ws['F5'].fill = yellow_fill
        ws['F5'].alignment = center
        for col in ['F5', 'G5', 'H5']:
            ws[col].fill = yellow_fill
            ws[col].border = border

        # Net block: I5:I6
        ws.merge_cells('I5:I6')
        ws['I5'] = 'Net'
        ws['I5'].font = header_font
        ws['I5'].fill = net_fill
        ws['I5'].alignment = center
        ws['I5'].border = border
        ws['I6'].border = border
        ws['I6'].fill = net_fill

        # Row 6 sub-headers — ONLY write to non-merged cells (C6:H6)
        sub_headers = {
            3: 'Dr', 4: 'Cr', 5: 'Balance',
            6: 'Dr', 7: 'Cr', 8: 'Balance',
        }
        for col_idx, header_text in sub_headers.items():
            c = ws.cell(row=6, column=col_idx, value=header_text)
            c.font = Font(bold=True, size=10)
            c.alignment = center
            c.border = border
            if 3 <= col_idx <= 5:
                c.fill = green_fill
            else:
                c.fill = yellow_fill

        row = 7
        # Opening
        ws.cell(row=row, column=1, value='—')
        ws.cell(row=row, column=2, value='Opening Balance').font = Font(bold=True)
        ws.cell(row=row, column=3, value=float(data['opening_recv']) if data['opening_recv'] > 0 else '')
        ws.cell(row=row, column=4, value=float(abs(data['opening_recv'])) if data['opening_recv'] < 0 else '')
        ws.cell(row=row, column=5, value=float(data['opening_recv']))
        ws.cell(row=row, column=6, value=float(abs(data['opening_pay'])) if data['opening_pay'] < 0 else '')
        ws.cell(row=row, column=7, value=float(data['opening_pay']) if data['opening_pay'] > 0 else '')
        ws.cell(row=row, column=8, value=float(data['opening_pay']))
        ws.cell(row=row, column=9, value=float(data['opening_recv'] - data['opening_pay']))
        for col in range(1, 10):
            c = ws.cell(row=row, column=col)
            c.border = border
            c.fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
            if col in (3, 4, 5, 6, 7, 8, 9):
                c.number_format = money_fmt
                c.alignment = right
        row += 1

        # Data rows
        for r in rows:
            ws.cell(row=row, column=1, value=r['date'].strftime('%d-%m-%Y'))
            desc = r['description']
            if r['reference']:
                desc += f"  [{r['reference']}]"
            ws.cell(row=row, column=2, value=desc)
            ws.cell(row=row, column=3, value=float(r['recv_dr']) if r['recv_dr'] else '')
            ws.cell(row=row, column=4, value=float(r['recv_cr']) if r['recv_cr'] else '')
            ws.cell(row=row, column=5, value=float(r['running_recv']))
            ws.cell(row=row, column=6, value=float(r['pay_dr']) if r['pay_dr'] else '')
            ws.cell(row=row, column=7, value=float(r['pay_cr']) if r['pay_cr'] else '')
            ws.cell(row=row, column=8, value=float(r['running_pay']))
            ws.cell(row=row, column=9, value=float(r['net']))
            for col in range(1, 10):
                c = ws.cell(row=row, column=col)
                c.border = border
                if col in (3, 4, 5, 6, 7, 8, 9):
                    c.number_format = money_fmt
                    c.alignment = right
                elif col == 1:
                    c.alignment = center
                else:
                    c.alignment = left
            row += 1

        # Totals
        ws.cell(row=row, column=2, value='Period Totals').font = Font(bold=True)
        ws.cell(row=row, column=3, value=float(data['total_recv_dr'])).number_format = money_fmt
        ws.cell(row=row, column=4, value=float(data['total_recv_cr'])).number_format = money_fmt
        ws.cell(row=row, column=6, value=float(data['total_pay_dr'])).number_format = money_fmt
        ws.cell(row=row, column=7, value=float(data['total_pay_cr'])).number_format = money_fmt
        for col in range(1, 10):
            c = ws.cell(row=row, column=col)
            c.border = border
            c.font = Font(bold=True)
            if col in (3, 4, 6, 7):
                c.alignment = right
        row += 1

        # Closing
        ws.cell(row=row, column=2, value='Closing Balance').font = Font(bold=True)
        ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=5)
        ws.cell(row=row, column=3, value=float(data['closing_recv'])).number_format = money_fmt
        ws.cell(row=row, column=3).font = Font(bold=True)
        ws.cell(row=row, column=3).alignment = right
        ws.merge_cells(start_row=row, start_column=6, end_row=row, end_column=8)
        ws.cell(row=row, column=6, value=float(data['closing_pay'])).number_format = money_fmt
        ws.cell(row=row, column=6).font = Font(bold=True)
        ws.cell(row=row, column=6).alignment = right
        ws.cell(row=row, column=9, value=float(data['net_position'])).number_format = money_fmt
        ws.cell(row=row, column=9).font = Font(bold=True)
        for col in range(1, 10):
            c = ws.cell(row=row, column=col)
            c.border = border
            c.fill = net_fill

        widths = [12, 42, 13, 13, 14, 13, 13, 14, 14]
        for idx, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(idx)].width = w
        ws.freeze_panes = 'A7'

    else:
        # Simple view (customer only) — receivable side
        headers = ['Date', 'Particulars', 'Debit (₹)', 'Credit (₹)', 'Balance (₹)']
        for col, h in enumerate(headers, 1):
            c = ws.cell(row=5, column=col, value=h)
            c.font = header_font
            c.fill = header_fill
            c.alignment = center
            c.border = border

        row = 6
        ws.cell(row=row, column=2, value='Opening Balance').font = Font(bold=True)
        ws.cell(row=row, column=5, value=float(data['opening_recv'])).number_format = money_fmt
        for col in range(1, 6):
            ws.cell(row=row, column=col).border = border
        row += 1

        for r in rows:
            ws.cell(row=row, column=1, value=r['date'].strftime('%d-%m-%Y'))
            desc = r['description']
            if r['reference']:
                desc += f"  [{r['reference']}]"
            ws.cell(row=row, column=2, value=desc)
            ws.cell(row=row, column=3, value=float(r['recv_dr']) if r['recv_dr'] else '')
            ws.cell(row=row, column=4, value=float(r['recv_cr']) if r['recv_cr'] else '')
            ws.cell(row=row, column=5, value=float(r['running_recv']))
            for col in range(1, 6):
                c = ws.cell(row=row, column=col)
                c.border = border
                if col in (3, 4, 5):
                    c.number_format = money_fmt
                    c.alignment = right
            row += 1

        ws.cell(row=row, column=2, value='Closing Balance').font = Font(bold=True)
        ws.cell(row=row, column=5, value=float(data['closing_recv'])).number_format = money_fmt
        for col in range(1, 6):
            ws.cell(row=row, column=col).border = border
            ws.cell(row=row, column=col).font = Font(bold=True)

        widths = [15, 50, 15, 15, 15]
        for idx, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(idx)].width = w
        ws.freeze_panes = 'A6'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    safe = customer.name.replace(' ', '_').replace('/', '_')
    response['Content-Disposition'] = f'attachment; filename="statement_{safe}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
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
                job.submitted_at = timezone.now()
                job.save()

                send_notification_to_staff(
                    title=f"New Repair Request: {job.job_number}",
                    message=f"{customer.name} submitted a repair for {job.device_model}. Awaiting device drop-off.",
                    link=reverse('accounting:repair_detail', args=[job.pk]),
                    notif_type='warning',
                    category='repairs',
                    send_email=True,
                )

                if is_htmx(request):
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('customer:customer_repair_detail', args=[job.pk])
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'success',
                            'message': f'Repair job {job.job_number} submitted. Please bring your device to the shop.',
                        },
                    })
                    return response
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
    """
    Customer apni repair job edit kar sakta hai — SIRF tab tak jab tak
    staff ne device physically receive na kiya ho.
    
    Jaise hi staff device receive karke 'received' (ya aage) mark karta hai,
    customer edit nahi kar sakta. Sirf estimate approve/reject kar sakta hai.
    """
    customer = get_object_or_404(Contact, user=request.user)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    if repair.status != 'pending' or repair.received_at is not None:
        messages.error(
            request,
            "This repair has already been received by the shop. "
            "You can no longer edit it. Please contact us directly for any changes."
        )
        return redirect_to_customer('customer_repair_detail', pk=repair.pk)

    if request.method == 'POST':
        form = CustomerRepairForm(request.POST, instance=repair)
        if form.is_valid():
            form.save()
            messages.success(request, f"Repair job {repair.job_number} updated.")

            send_notification_to_staff(
                title=f"Repair Updated by Customer: {repair.job_number}",
                message=f"{customer.name} updated repair for {repair.device_model}",
                link=reverse('accounting:repair_detail', args=[repair.pk]),
                notif_type='info',
                category='repairs',
                send_email=False,
            )

            if is_htmx(request):
                response = HttpResponse()
                response['HX-Redirect'] = reverse('customer:customer_repair_detail', args=[repair.pk])
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'success',
                        'message': f'Repair job {repair.job_number} updated.',
                    },
                })
                return response
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
    """
    Customer apni repair delete kar sakta hai — SIRF tab tak jab tak
    staff ne device physically receive na kiya ho.
    """
    customer = get_object_or_404(Contact, user=request.user)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    # ---- Same permission check ----
    if repair.status != 'pending' or repair.received_at is not None:
        messages.error(
            request,
            "This repair has already been received by the shop. "
            "It cannot be deleted. Please contact us directly."
        )
        if is_htmx(request):
            return toast_only_response(
                {'level': 'danger', 'message': 'Cannot delete - repair already received by shop.'},
                status=400,
            )
        return redirect_to_customer('customer_repair_detail', pk=repair.pk)

    if request.method == 'POST':
        job_number = repair.job_number
        device_model = repair.device_model

        repair.delete()
        messages.success(request, f"Repair job {job_number} deleted.")

        send_notification_to_staff(
            title=f"Repair Deleted by Customer: {job_number}",
            message=f"{customer.name} deleted repair for {device_model}",
            link=reverse('accounting:repair_list'),
            notif_type='warning',
            category='repairs',
            send_email=False,
        )

        if is_htmx(request):
            # Trigger a reload of the repair list (with filters preserved)
            response = HttpResponse()
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'success', 'message': f'Repair job {job_number} deleted.'},
                'closeModal': '',
                'reloadCustomerRepairs': '',
            })
            return response
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
        response = HttpResponse()
        response['HX-Redirect'] = reverse('customer:customer_repair_detail', args=[repair.pk])
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': 'Estimate approved.'},
        })
        return response
    return redirect_to_customer('customer_repair_detail', pk=repair.pk)
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
        response = HttpResponse()
        response['HX-Redirect'] = reverse('customer:customer_repair_detail', args=[repair.pk])
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'info', 'message': 'Estimate put on hold.'},
        })
        return response
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
        response = HttpResponse()
        response['HX-Redirect'] = reverse('customer:customer_repair_detail', args=[repair.pk])
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'warning', 'message': 'Estimate rejected.'},
        })
        return response
    return redirect_to_customer('customer_repair_detail', pk=repair.pk)


# ============================================================
# NOTIFICATIONS (Customer) – Corrected with consistent target check
# ============================================================

def render_customer_dropdown(request):
    notifications = request.user.notifications.all().order_by('-created_at')[:10]
    unread_count = request.user.notifications.filter(is_read=False).count()
    return render(request, 'customer/partials/dropdown.html', {
        'notifications': notifications,
        'unread_count': unread_count,
    })

def render_customer_notification_list(request, page_obj=None):
    if page_obj is None:
        notifications = request.user.notifications.all().order_by('-created_at')
        paginator = Paginator(notifications, 20)
        page = request.GET.get('page', 1)
        page_obj = paginator.get_page(page)
    unread_count = request.user.notifications.filter(is_read=False).count()
    return render(request, 'customer/partials/_notification_items.html', {
        'page_obj': page_obj,
        'unread_count': unread_count,
    })

@login_required
def notification_list(request):
    notifications = request.user.notifications.all().order_by('-created_at')
    paginator = Paginator(notifications, 20)
    page = request.GET.get('page', 1)
    page_obj = paginator.get_page(page)
    unread_count = request.user.notifications.filter(is_read=False).count()
    if request.htmx:
        return render(request, 'customer/partials/_notification_items.html', {
            'page_obj': page_obj,
            'unread_count': unread_count,
        })
    return render(request, 'customer/notification_list.html', {
        'notifications': page_obj,
        'page_obj': page_obj,
        'unread_count': unread_count,
    })

@login_required
def notification_mark_all_read(request):
    if request.method != 'POST':
        return HttpResponse("Method not allowed", status=405)
    count = request.user.notifications.filter(is_read=False).update(is_read=True)
    if request.htmx:
        target = request.headers.get('HX-Target', '')
        if 'dropdown' in target.lower():
            return render_customer_dropdown(request)
        return render_customer_notification_list(request)
    messages.success(request, f"{count} marked read.")
    return redirect('customer:customer_notifications')

@login_required
def notification_mark_read(request, pk):
    if request.method != 'POST':
        return HttpResponse("Method not allowed", status=405)
    notif = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notif.is_read = True
    notif.save()
    if request.htmx:
        target = request.headers.get('HX-Target', '')
        if 'dropdown' in target.lower():
            return render_customer_dropdown(request)
        return render_customer_notification_list(request)
    return HttpResponse("Marked read", status=200)

@login_required
def notification_delete(request, pk):
    if request.method != 'DELETE':
        return HttpResponse("Method not allowed", status=405)
    notif = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notif.delete()
    if request.htmx:
        target = request.headers.get('HX-Target', '')
        if 'dropdown' in target.lower():
            return render_customer_dropdown(request)
        return render_customer_notification_list(request)
    return HttpResponse("Deleted", status=200)

@login_required
def notification_delete_all(request):
    if request.method != 'DELETE':
        return HttpResponse("Method not allowed", status=405)
    request.user.notifications.all().delete()
    if request.htmx:
        target = request.headers.get('HX-Target', '')
        if 'dropdown' in target.lower():
            return render_customer_dropdown(request)
        return render_customer_notification_list(request)
    messages.success(request, "All notifications deleted.")
    return redirect('customer:customer_notifications')

@login_required
def notification_dropdown(request):
    try:
        return render_customer_dropdown(request)
    except Exception:
        return HttpResponse('<div class="dropdown-item text-danger">Error loading</div>', status=500)

@login_required
def unread_count_text(request):
    return HttpResponse(str(request.user.notifications.filter(is_read=False).count()))


# ============================================================
# EMAIL CHANGE (Professional OTP-based)
# ============================================================

@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_profile')
def email_change_request(request):
    """Step 1: User requests email change by entering new email."""
    customer = get_object_or_404(Contact, user=request.user)
    if request.method == 'POST' and request.headers.get('Content-Type') == 'application/json':
        try:
            data = json.loads(request.body)
            if data.get('resend'):
                new_email = request.session.get('pending_new_email')
                if not new_email:
                    return JsonResponse({'error': 'Session expired.'}, status=400)
                last_otp = EmailOTP.objects.filter(email=new_email, purpose='change_email').order_by('-created_at').first()
                if last_otp:
                    diff = (timezone.now() - last_otp.created_at).total_seconds()
                    if diff < 60:
                        return JsonResponse({'error': f'Please wait {60-int(diff)} seconds.'}, status=429)
                success = create_and_send_otp(request.user, new_email, 'change_email')
                if success:
                    return JsonResponse({'success': True})
                return JsonResponse({'error': 'Failed to send OTP.'}, status=500)
        except (json.JSONDecodeError, KeyError, AttributeError):
            pass
        return JsonResponse({'error': 'Invalid request.'}, status=400)
    
    if request.method == 'POST':
        form = EmailChangeRequestForm(request.user, request.POST)
        if form.is_valid():
            new_email = form.cleaned_data['new_email']
            success = create_and_send_otp(request.user, new_email, 'change_email')
            if success:
                request.session['pending_new_email'] = new_email
                request.session['pending_user_id'] = request.user.id
                request.session['otp_purpose'] = 'change_email'
                
                if is_htmx(request):
                    response = render(request, 'customer/partials/email_change_otp.html', {'email': new_email})
                    response['HX-Retarget'] = '#profile-container'
                    response['HX-Reswap'] = 'innerHTML'
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {'level': 'success', 'message': f'OTP sent to {new_email}'}
                    })
                    return response
                messages.success(request, f"OTP sent to {new_email}")
                return redirect('customer:email_change_verify')
            else:
                if is_htmx(request):
                    response = render(request, 'customer/partials/email_change_form.html', {'form': form})
                    response['HX-Retarget'] = '#profile-container'
                    response['HX-Reswap'] = 'innerHTML'
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {'level': 'danger', 'message': 'Failed to send OTP. Please try again.'}
                    })
                    return response
                messages.error(request, "Failed to send OTP.")
        else:
            if is_htmx(request):
                response = render(request, 'customer/partials/email_change_form.html', {'form': form})
                response['HX-Retarget'] = '#profile-container'
                response['HX-Reswap'] = 'innerHTML'
                return response
    else:
        form = EmailChangeRequestForm(request.user)
    
    if is_htmx(request):
        return render(request, 'customer/partials/email_change_form.html', {'form': form})
    
    return render(request, 'customer/email_change.html', {'form': form, 'customer': customer})


@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_profile')
def email_change_verify(request):
    """Step 2: Verify OTP and update email."""
    user = request.user
    customer = get_object_or_404(Contact, user=user)
    
    new_email = request.session.get('pending_new_email')
    purpose = request.session.get('otp_purpose')
    
    if not new_email or purpose != 'change_email':
        messages.error(request, "Invalid session. Please request email change again.")
        if is_htmx(request):
            response = render(request, 'customer/partials/email_change_form.html', {'form': EmailChangeRequestForm(user)})
            response['HX-Retarget'] = '#profile-container'
            response['HX-Reswap'] = 'innerHTML'
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'danger', 'message': 'Invalid session. Please try again.'}
            })
            return response
        return redirect('customer:email_change_request')
    
    if request.method == 'POST':
        otp = request.POST.get('otp', '').strip()
        if not otp or len(otp) != 6:
            if is_htmx(request):
                response = render(request, 'customer/partials/email_change_otp.html', {'email': new_email})
                response['HX-Retarget'] = '#profile-container'
                response['HX-Reswap'] = 'innerHTML'
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'danger', 'message': 'Please enter a valid 6-digit OTP.'}
                })
                return response
            messages.error(request, "Please enter a valid 6-digit OTP.")
            return render(request, 'customer/email_change_otp.html', {'email': new_email})
        
        verified_user = verify_otp(new_email, otp, 'change_email')
        
        if verified_user and verified_user.id == user.id:
            with transaction.atomic():
                user.email = new_email
                user.save()
                customer.email = new_email
                customer.save()
                
                request.session.pop('pending_new_email', None)
                request.session.pop('otp_purpose', None)
                
                EmailOTP.objects.filter(user=user, purpose='change_email').delete()
                
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
            
            messages.success(request, "Email updated successfully. Please login with your new email.")
            return redirect('accounting:login')
        else:
            if is_htmx(request):
                response = render(request, 'customer/partials/email_change_otp.html', {'email': new_email})
                response['HX-Retarget'] = '#profile-container'
                response['HX-Reswap'] = 'innerHTML'
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'danger', 'message': 'Invalid or expired OTP. Please try again.'}
                })
                return response
            messages.error(request, "Invalid or expired OTP. Please try again.")
            return render(request, 'customer/email_change_otp.html', {'email': new_email})
    
    if is_htmx(request):
        return render(request, 'customer/partials/email_change_otp.html', {'email': new_email})
    
    return render(request, 'customer/email_change_otp.html', {'email': new_email})