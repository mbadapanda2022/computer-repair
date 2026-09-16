# accounting/views/customer_views.py
"""
Customer Portal Views — Production Grade.

Features:
─────────
✔ Dashboard with full stats + charts
✔ Invoice list/detail/print/Excel (with filters)
✔ Repairs list/detail/print/Excel + full CRUD by customer
✔ Estimate approve / hold / reject
✔ Payments list/print/Excel
✔ Statement (dual view for contact_type='both')
✔ Profile view/edit with completion meter
✔ Password change
✔ Email change with OTP
✔ Notifications (list/dropdown/mark-read/delete)
✔ Real-time HTMX field validation

Supports contact_type in {'customer', 'vendor', 'both'}.
"""

import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.mail import send_mail
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

# Excel export
try:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    openpyxl = None

from ..decorators import handle_errors
from ..forms import (
    CustomerProfileForm,
    CustomerRepairForm,
    EmailChangeRequestForm,
)
from ..models import (
    CompanyProfile,
    Contact,
    EmailOTP,
    Invoice,
    Notification,
    Payment,
    RepairJob,
)
from ..utils.notification_helpers import (
    get_unread_count,
    send_notification_to_staff,
)
from ..utils.otp_helpers import (
    OTP_RESEND_COOLDOWN_SECONDS,
    create_and_send_otp,
    verify_otp,
)
from .statements import _build_combined_rows
from .utils import (
    htmx_response,
    is_htmx,
    redirect_to_customer,
    redirect_to_staff,
    toast_only_response,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════
PAGE_SIZE = 10
DROPDOWN_LIMIT = 10
NOTIFICATION_PAGE_SIZE = 20

# Session keys (must match auth.py)
SK_PENDING_USER_ID = 'pending_user_id'
SK_PENDING_EMAIL = 'pending_email'
SK_OTP_PURPOSE = 'otp_purpose'
SK_PENDING_NEW_EMAIL = 'pending_new_email'


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════
def _get_customer(request):
    """
    Fetch the Contact linked to request.user.
    Raises Http404 if missing (which handle_errors will catch).
    """
    return get_object_or_404(Contact, user=request.user)


def _paginate(request, queryset, per_page=PAGE_SIZE):
    """Paginate with safe page fallbacks."""
    paginator = Paginator(queryset, per_page)
    page_number = request.GET.get('page', 1)
    try:
        return paginator.page(page_number), paginator
    except (PageNotAnInteger, EmptyPage):
        return paginator.page(1), paginator


def _check_repair_editable(repair):
    """
    Customer can edit/delete a repair ONLY while it's 'pending' and
    staff hasn't physically received it yet.
    """
    return repair.status == 'pending' and repair.received_at is None


# ════════════════════════════════════════════════════════════
# 1. REAL-TIME FIELD VALIDATION (HTMX)
# ════════════════════════════════════════════════════════════
@handle_errors(default_redirect='customer:customer_repairs')
def validate_repair_field(request):
    """Public endpoint — used by guest/customer repair form."""
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    value = request.GET.get(field_name, '')
    form = CustomerRepairForm(data={field_name: value})

    if form.is_valid():
        return HttpResponse('')

    errors = form.errors.get(field_name, [])
    html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
    html += ''.join(f'<div>{err}</div>' for err in errors)
    html += '</div>'
    return HttpResponse(html)


@login_required
@require_http_methods(["GET"])
def validate_customer_profile_field(request):
    """HTMX validation for profile fields with uniqueness checks."""
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    customer = _get_customer(request)

    # Build data dict with ALL fields so cross-field rules work
    data = {f: request.GET.get(f, '') for f in CustomerProfileForm.base_fields}
    data[field_name] = request.GET.get(field_name, '').strip()

    form = CustomerProfileForm(data, instance=customer)
    form.is_valid()   # triggers field clean() methods

    errors = form.errors.get(field_name, [])
    html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
    html += ''.join(f'<div>{err}</div>' for err in errors)
    html += '</div>'
    return HttpResponse(html)


# ════════════════════════════════════════════════════════════
# 2. DASHBOARD
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def dashboard(request):
    """
    Customer dashboard — full stats + recent activity.

    Automatically creates Contact if missing (defensive).
    """
    contact, _ = Contact.objects.get_or_create(
        user=request.user,
        defaults={
            'name': request.user.get_full_name() or request.user.username,
            'email': request.user.email,
            'contact_type': 'customer',
        },
    )

    today = date.today()
    month_start = today.replace(day=1)

    # ── Invoices ─────────────────────────────────────
    invoices = Invoice.objects.filter(customer=contact)
    agg = invoices.aggregate(
        total_paid=Sum('paid_amount'),
        total_due=Sum('balance_due'),
        month_total=Sum('grand_total', filter=Q(date__gte=month_start)),
        month_paid=Sum('paid_amount', filter=Q(date__gte=month_start)),
    )

    # ── Repairs ──────────────────────────────────────
    repairs = RepairJob.objects.filter(customer=contact)

    # ── Payments ─────────────────────────────────────
    payments = Payment.objects.filter(contact=contact, direction='received')

    # ── Context ──────────────────────────────────────
    context = {
        'customer': contact,

        # Invoices
        'total_invoices': invoices.count(),
        'paid_invoices': invoices.filter(payment_status='paid').count(),
        'unpaid_invoices': invoices.filter(payment_status__in=['unpaid', 'partial']).count(),
        'total_paid': agg['total_paid'] or Decimal('0'),
        'total_due': agg['total_due'] or Decimal('0'),
        'month_sales': agg['month_total'] or Decimal('0'),
        'month_paid': agg['month_paid'] or Decimal('0'),

        # Repairs
        'total_repairs': repairs.count(),
        'pending_repairs': repairs.filter(
            status__in=['pending', 'received', 'diagnosis', 'repairing']
        ).count(),
        'ready_repairs': repairs.filter(status='ready').count(),
        'completed_repairs': repairs.filter(status='delivered').count(),
        'cancelled_repairs': repairs.filter(status='cancelled').count(),

        # Estimate status
        'approved_estimates': repairs.filter(estimate_status='approved').count(),
        'pending_estimates': repairs.filter(estimate_status='pending').count(),
        'hold_estimates': repairs.filter(estimate_status='on_hold').count(),
        'rejected_estimates': repairs.filter(estimate_status='rejected').count(),

        # Payments
        'total_payments': payments.aggregate(t=Sum('amount'))['t'] or Decimal('0'),
        'month_payments': payments.filter(date__gte=month_start).aggregate(
            t=Sum('amount')
        )['t'] or Decimal('0'),

        # Misc
        'unread_count': get_unread_count(request.user),
        'recent_invoices': invoices.order_by('-date')[:5],
        'recent_repairs': repairs.order_by('-date_in')[:5],
        'recent_payments': payments.order_by('-date')[:5],
        'today': today,
        'month_start': month_start,
    }

    if is_htmx(request):
        return render(request, 'customer/partials/dashboard_stats.html', context)
    return render(request, 'customer/dashboard.html', context)


@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def refresh_dashboard_stats(request):
    """HTMX-only — refresh just the stats block."""
    # Just call dashboard's HTMX branch
    return dashboard(request)


@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def dashboard_stats_json(request):
    """
    Chart data (JSON) — last 7 days + status distributions.
    Optimized: uses single grouped queries instead of per-day loops.
    """
    customer = _get_customer(request)

    today = date.today()
    week_ago = today - timedelta(days=6)

    # ── Daily invoices (single query, group by date) ─────
    inv_qs = (
        Invoice.objects
        .filter(customer=customer, date__gte=week_ago)
        .values('date')
        .annotate(total=Sum('grand_total'))
    )
    inv_map = {row['date']: float(row['total'] or 0) for row in inv_qs}

    # ── Daily payments (single query) ────────────────────
    pay_qs = (
        Payment.objects
        .filter(contact=customer, direction='received', date__gte=week_ago)
        .values('date')
        .annotate(total=Sum('amount'))
    )
    pay_map = {row['date']: float(row['total'] or 0) for row in pay_qs}

    # ── Build 7-day labels ───────────────────────────────
    labels, invoice_data, payment_data = [], [], []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        labels.append(d.strftime('%d %b'))
        invoice_data.append(inv_map.get(d, 0.0))
        payment_data.append(pay_map.get(d, 0.0))

    # ── Status distributions ─────────────────────────────
    inv_status_qs = (
        Invoice.objects.filter(customer=customer)
        .values('payment_status').annotate(count=Count('id'))
    )
    inv_status_map = dict(Invoice.PAYMENT_STATUS)
    status_labels = [inv_status_map.get(r['payment_status'], r['payment_status']) for r in inv_status_qs]
    status_data = [r['count'] for r in inv_status_qs]

    repair_status_qs = (
        RepairJob.objects.filter(customer=customer)
        .values('status').annotate(count=Count('id'))
    )
    rep_status_map = dict(RepairJob.STATUS_CHOICES)
    repair_status_labels = [rep_status_map.get(r['status'], r['status']) for r in repair_status_qs]
    repair_status_data = [r['count'] for r in repair_status_qs]

    return JsonResponse({
        'labels': labels,
        'invoices': invoice_data,
        'payments': payment_data,
        'status_labels': status_labels,
        'status_data': status_data,
        'repair_status_labels': repair_status_labels,
        'repair_status_data': repair_status_data,
    })


# ════════════════════════════════════════════════════════════
# 3. INVOICES
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def invoice_list(request):
    """
    Customer invoice list — filter, paginate, print, excel, HTMX.
    Query params:
      - status: paid | partial | unpaid
      - search: invoice_number
      - date_from, date_to: YYYY-MM-DD
      - print=1  → print view
      - excel=1  → xlsx download
    """
    customer = _get_customer(request)

    status_filter = request.GET.get('status', '').strip()
    search = request.GET.get('search', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    qs = (
        Invoice.objects
        .filter(customer=customer)
        .select_related('customer')
        .order_by('-date', '-id')
    )

    if status_filter:
        qs = qs.filter(payment_status=status_filter)
    if search:
        qs = qs.filter(invoice_number__icontains=search)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    # Excel export
    if request.GET.get('excel') == '1':
        return _customer_invoices_excel(customer, qs, request)

    # Summary stats
    agg = qs.aggregate(
        total_invoiced=Sum('grand_total'),
        total_paid=Sum('paid_amount'),
        total_due=Sum('balance_due'),
    )

    # Print mode
    if request.GET.get('print') == '1':
        company = CompanyProfile.get_instance()
        return render(request, 'customer/invoice_list_print.html', {
            'customer': customer,
            'invoices': qs,
            'company': company,
            'logo_exists': bool(company.logo and company.logo.name),
            'total_invoiced': agg['total_invoiced'] or Decimal('0'),
            'total_paid': agg['total_paid'] or Decimal('0'),
            'total_due': agg['total_due'] or Decimal('0'),
            'status_filter': status_filter,
            'search': search,
            'date_from': date_from,
            'date_to': date_to,
        })

    # Paginated
    page_obj, paginator = _paginate(request, qs)

    context = {
        'customer': customer,
        'invoices': page_obj,
        'page_obj': page_obj,
        'total_count': paginator.count,
        'status_filter': status_filter,
        'search': search,
        'date_from': date_from,
        'date_to': date_to,
        'total_invoiced': agg['total_invoiced'] or Decimal('0'),
        'total_paid': agg['total_paid'] or Decimal('0'),
        'total_due': agg['total_due'] or Decimal('0'),
    }

    if is_htmx(request):
        return render(request, 'customer/partials/invoice_list_table.html', context)
    return render(request, 'customer/invoices.html', context)


@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def invoice_detail(request, pk):
    """Invoice detail with optional repair context."""
    customer = _get_customer(request)
    invoice = get_object_or_404(
        Invoice.objects.select_related('customer'),
        pk=pk, customer=customer,
    )
    # Prefetch items with product for efficiency
    invoice_items = invoice.items.select_related('product').all()
    repair_job = RepairJob.objects.filter(invoice=invoice).first()

    return render(request, 'customer/invoice_detail.html', {
        'invoice': invoice,
        'invoice_items': invoice_items,
        'repair_job': repair_job,
    })


@login_required
@handle_errors(default_redirect='customer:customer_invoices')
def invoice_print(request, pk):
    """Print-friendly invoice view."""
    customer = _get_customer(request)
    invoice = get_object_or_404(
        Invoice.objects.select_related('customer'),
        pk=pk, customer=customer,
    )

    company = CompanyProfile.get_instance()
    repair_job = RepairJob.objects.filter(invoice=invoice).first()

    # GST breakup
    gst_breakup = (
        invoice.get_gst_breakup()
        if hasattr(invoice, 'get_gst_breakup')
        else {'total_tax': invoice.tax_amount, 'cgst': 0, 'sgst': 0, 'igst': 0}
    )

    return render(request, 'customer/invoice_print.html', {
        'invoice': invoice,
        'invoice_items': invoice.items.select_related('product').all(),
        'customer': customer,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
        'gst_breakup': gst_breakup,
        'repair_job': repair_job,
    })


# ════════════════════════════════════════════════════════════
# 4. REPAIRS
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def repair_list(request):
    """Customer repair list — filter, paginate, print, excel, HTMX."""
    customer = _get_customer(request)

    # Reset — clear filters
    if request.GET.get('reset'):
        return redirect('customer:customer_repairs')

    status_filter = request.GET.get('status', '').strip()
    search = request.GET.get('search', '').strip()
    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()

    qs = (
        RepairJob.objects
        .filter(customer=customer)
        .select_related('invoice')
        .order_by('-date_in', '-id')
    )

    if status_filter:
        qs = qs.filter(status=status_filter)
    if search:
        qs = qs.filter(
            Q(job_number__icontains=search) |
            Q(device_model__icontains=search) |
            Q(issue_description__icontains=search)
        )
    if date_from:
        qs = qs.filter(date_in__gte=date_from)
    if date_to:
        qs = qs.filter(date_in__lte=date_to)

    filtered_total = qs.aggregate(t=Sum('final_amount'))['t'] or Decimal('0')

    # Print mode
    if request.GET.get('print') == '1':
        company = CompanyProfile.get_instance()
        return render(request, 'customer/repair_list_print.html', {
            'repairs': qs,
            'customer': customer,
            'company': company,
            'logo_exists': bool(company.logo and company.logo.name),
            'status_filter': status_filter,
            'search': search,
            'date_from': date_from,
            'date_to': date_to,
            'total_count': qs.count(),
            'total_amount': filtered_total,
            'repair_status_choices': RepairJob.STATUS_CHOICES,
        })

    # Paginated
    page_obj, paginator = _paginate(request, qs)

    context = {
        'repairs': page_obj,
        'page_obj': page_obj,
        'customer': customer,
        'status_filter': status_filter,
        'search': search,
        'date_from': date_from,
        'date_to': date_to,
        'total_count': paginator.count,
        'filtered_total': filtered_total,
        'repair_status_choices': RepairJob.STATUS_CHOICES,
    }

    if is_htmx(request):
        return render(request, 'customer/partials/repair_list_table.html', context)
    return render(request, 'customer/repairs.html', context)


@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def repair_detail(request, pk):
    """Repair detail with parts breakdown."""
    customer = _get_customer(request)
    repair = get_object_or_404(
        RepairJob.objects.select_related('invoice'),
        pk=pk, customer=customer,
    )
    parts = repair.parts.select_related('product').all()
    parts_total = sum(p.line_total for p in parts)

    return render(request, 'customer/repair_detail.html', {
        'repair': repair,
        'parts': parts,
        'parts_total': parts_total,
    })


@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def repair_print(request, pk):
    """Print-friendly repair view."""
    customer = _get_customer(request)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    company = CompanyProfile.get_instance()
    parts = repair.parts.select_related('product').all()
    parts_total = sum(p.line_total for p in parts)

    return render(request, 'customer/repair_print.html', {
        'repair': repair,
        'parts': parts,
        'parts_total': parts_total,
        'customer': customer,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
    })


# ════════════════════════════════════════════════════════════
# 5. REPAIR CRUD (Customer actions)
# ════════════════════════════════════════════════════════════
@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs',
               htmx_template='customer/repair_create.html')
def repair_create(request):
    """Customer submits a new repair request via portal."""
    customer = _get_customer(request)

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

            # Notify staff
            send_notification_to_staff(
                title=f"New Repair Request: {job.job_number}",
                message=(
                    f"{customer.name} submitted a repair for "
                    f"{job.device_model}. Awaiting device drop-off."
                ),
                link=reverse('accounting:repair_detail', args=[job.pk]),
                notif_type='warning',
                category='repairs',
                send_email=True,
            )

            if is_htmx(request):
                response = HttpResponse()
                response['HX-Redirect'] = reverse(
                    'customer:customer_repair_detail', args=[job.pk],
                )
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'success',
                        'message': (
                            f'Repair job {job.job_number} submitted. '
                            f'Please bring your device to the shop.'
                        ),
                    },
                })
                return response

            messages.success(
                request,
                f'Repair job {job.job_number} submitted. Please bring your device.',
            )
            return redirect('customer:customer_repair_detail', pk=job.pk)

        # Form invalid
        if is_htmx(request):
            return render(request, 'customer/repair_create.html', {'form': form},
                          status=400)

    else:
        form = CustomerRepairForm()

    return render(request, 'customer/repair_create.html', {'form': form})


@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs',
               htmx_template='customer/repair_edit.html')
def repair_update(request, pk):
    """
    Customer edits their repair — only while status='pending'
    and staff hasn't received the device yet.
    """
    customer = _get_customer(request)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    if not _check_repair_editable(repair):
        msg = (
            "This repair has already been received by the shop. "
            "You can no longer edit it. Please contact us directly."
        )
        if is_htmx(request):
            return toast_only_response({'level': 'warning', 'message': msg})
        messages.error(request, msg)
        return redirect('customer:customer_repair_detail', pk=repair.pk)

    if request.method == 'POST':
        form = CustomerRepairForm(request.POST, instance=repair)
        if form.is_valid():
            form.save()

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
                response['HX-Redirect'] = reverse(
                    'customer:customer_repair_detail', args=[repair.pk],
                )
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'success',
                        'message': f'Repair {repair.job_number} updated.',
                    },
                })
                return response

            messages.success(request, f"Repair {repair.job_number} updated.")
            return redirect('customer:customer_repair_detail', pk=repair.pk)

        if is_htmx(request):
            return render(request, 'customer/repair_edit.html',
                          {'form': form, 'repair': repair}, status=400)

    else:
        form = CustomerRepairForm(instance=repair)

    return render(request, 'customer/repair_edit.html',
                  {'form': form, 'repair': repair})


@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def repair_delete(request, pk):
    """Customer deletes their repair — same edit-restriction rule."""
    customer = _get_customer(request)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    if not _check_repair_editable(repair):
        msg = (
            "This repair has already been received by the shop. "
            "It cannot be deleted. Please contact us directly."
        )
        if is_htmx(request):
            return toast_only_response({'level': 'danger', 'message': msg}, status=400)
        messages.error(request, msg)
        return redirect('customer:customer_repair_detail', pk=repair.pk)

    if request.method == 'POST':
        job_number = repair.job_number
        device_model = repair.device_model
        repair.delete()

        send_notification_to_staff(
            title=f"Repair Deleted by Customer: {job_number}",
            message=f"{customer.name} deleted repair for {device_model}",
            link=reverse('accounting:repair_list'),
            notif_type='warning',
            category='repairs',
            send_email=False,
        )

        if is_htmx(request):
            response = HttpResponse()
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'success', 'message': f'Repair {job_number} deleted.'},
                'closeModal': '',
                'reloadCustomerRepairs': '',
            })
            return response

        messages.success(request, f"Repair {job_number} deleted.")
        return redirect('customer:customer_repairs')

    return render(request, 'customer/repair_confirm_delete.html', {'repair': repair})


# ════════════════════════════════════════════════════════════
# 6. ESTIMATE APPROVE / HOLD / REJECT
# ════════════════════════════════════════════════════════════
@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def repair_estimate_approve(request, pk):
    """Customer approves the estimate → move repair to 'repairing'."""
    customer = _get_customer(request)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    if repair.estimate_status == 'approved':
        messages.warning(request, "This estimate is already approved.")
        return redirect('customer:customer_repair_detail', pk=repair.pk)

    with transaction.atomic():
        repair.estimate_status = 'approved'
        repair.estimate_approved_at = timezone.now()
        repair.estimate_approved_by = request.user
        repair.approval_source = 'portal'
        if repair.estimated_cost and repair.estimated_cost > 0:
            repair.status = 'repairing'
        repair.save(update_fields=[
            'estimate_status', 'estimate_approved_at', 'estimate_approved_by',
            'approval_source', 'status',
        ])

    send_notification_to_staff(
        title=f"Estimate Approved: {repair.job_number}",
        message=f"{customer.name} approved the estimate for {repair.device_model}",
        link=reverse('accounting:repair_detail', args=[repair.pk]),
        notif_type='success',
        category='repairs',
        send_email=True,
    )

    if is_htmx(request):
        response = HttpResponse()
        response['HX-Redirect'] = reverse('customer:customer_repair_detail', args=[repair.pk])
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': 'Estimate approved.'},
        })
        return response

    messages.success(request, f"Estimate for {repair.job_number} approved.")
    return redirect('customer:customer_repair_detail', pk=repair.pk)


@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def repair_estimate_hold(request, pk):
    """Customer puts estimate on hold."""
    customer = _get_customer(request)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    if repair.estimate_status == 'approved':
        messages.warning(request, "This estimate is already approved.")
        return redirect('customer:customer_repair_detail', pk=repair.pk)

    repair.estimate_status = 'on_hold'
    repair.save(update_fields=['estimate_status'])

    send_notification_to_staff(
        title=f"Estimate On Hold: {repair.job_number}",
        message=f"{customer.name} put the estimate on hold for {repair.device_model}",
        link=reverse('accounting:repair_detail', args=[repair.pk]),
        notif_type='warning',
        category='repairs',
        send_email=False,
    )

    if is_htmx(request):
        response = HttpResponse()
        response['HX-Redirect'] = reverse('customer:customer_repair_detail', args=[repair.pk])
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'info', 'message': 'Estimate put on hold.'},
        })
        return response

    messages.info(request, f"Estimate for {repair.job_number} is on hold.")
    return redirect('customer:customer_repair_detail', pk=repair.pk)


@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def repair_estimate_reject(request, pk):
    """Customer rejects the estimate."""
    customer = _get_customer(request)
    repair = get_object_or_404(RepairJob, pk=pk, customer=customer)

    if repair.estimate_status == 'approved':
        messages.warning(request, "This estimate is already approved.")
        return redirect('customer:customer_repair_detail', pk=repair.pk)

    repair.estimate_status = 'rejected'
    repair.save(update_fields=['estimate_status'])

    send_notification_to_staff(
        title=f"Estimate Rejected: {repair.job_number}",
        message=f"{customer.name} rejected the estimate for {repair.device_model}",
        link=reverse('accounting:repair_detail', args=[repair.pk]),
        notif_type='error',
        category='repairs',
        send_email=False,
    )

    if is_htmx(request):
        response = HttpResponse()
        response['HX-Redirect'] = reverse('customer:customer_repair_detail', args=[repair.pk])
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'warning', 'message': 'Estimate rejected.'},
        })
        return response

    messages.warning(request, f"Estimate for {repair.job_number} rejected.")
    return redirect('customer:customer_repair_detail', pk=repair.pk)


# ════════════════════════════════════════════════════════════
# 7. PAYMENTS
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def payment_list(request):
    """Customer payment list — filter, paginate, print, excel."""
    customer = _get_customer(request)

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    method = request.GET.get('method', '').strip()
    search = request.GET.get('search', '').strip()

    qs = (
        Payment.objects
        .filter(contact=customer, direction='received')
        .prefetch_related('allocations__invoice')
        .order_by('-date', '-id')
    )

    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if method:
        qs = qs.filter(method=method)
    if search:
        qs = qs.filter(
            Q(allocations__invoice__invoice_number__icontains=search) |
            Q(reference__icontains=search) |
            Q(upi_ref__icontains=search) |
            Q(description__icontains=search)
        ).distinct()

    # Excel
    if request.GET.get('excel') == '1':
        return _customer_payments_excel(customer, qs)

    total_amount = qs.aggregate(t=Sum('amount'))['t'] or Decimal('0')

    # Print
    if request.GET.get('print') == '1':
        company = CompanyProfile.get_instance()
        return render(request, 'customer/payment_print.html', {
            'customer': customer,
            'payments': qs,
            'total_amount': total_amount,
            'company': company,
            'logo_exists': bool(company.logo and company.logo.name),
            'date_from': date_from,
            'date_to': date_to,
            'method': method,
            'search': search,
        })

    # Paginated
    page_obj, paginator = _paginate(request, qs)

    context = {
        'customer': customer,
        'payments': page_obj,
        'page_obj': page_obj,
        'total_amount': total_amount,
        'total_count': paginator.count,
        'date_from': date_from,
        'date_to': date_to,
        'method': method,
        'search': search,
    }

    if is_htmx(request):
        return render(request, 'customer/partials/payment_list_table.html', context)
    return render(request, 'customer/payments.html', context)


# ════════════════════════════════════════════════════════════
# 8. STATEMENT (dual view for 'both' type)
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def statement(request):
    """
    Customer statement.

    For 'customer' type → receivable side only.
    For 'both'          → receivable + payable with net position.
    """
    customer = _get_customer(request)

    if request.GET.get('reset'):
        return redirect('customer:customer_statement')

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '').strip()

    # Excel
    if request.GET.get('excel') == '1':
        return _customer_statement_excel(customer, request)

    # Build rows (receivable + payable sides)
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

    # Adjust totals for the visible side
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

    page_obj, _ = _paginate(request, rows, per_page=PAGE_SIZE)

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

    # Print
    if request.GET.get('print') == '1':
        return render(request, 'customer/statement_print.html', context)

    if is_htmx(request):
        return render(request, 'customer/partials/statement_table.html', context)
    return render(request, 'customer/statement.html', context)


# ════════════════════════════════════════════════════════════
# 9. PROFILE
# ════════════════════════════════════════════════════════════
def _profile_context(customer, request):
    """Build the shared profile context (used by multiple views)."""
    invoices = Invoice.objects.filter(customer=customer)
    repairs = RepairJob.objects.filter(customer=customer)
    payments = Payment.objects.filter(contact=customer, direction='received')

    fields = [
        customer.name, customer.phone, customer.address,
        customer.state, customer.gstin, customer.email,
    ]
    filled = sum(1 for f in fields if f)
    completion = int((filled / len(fields)) * 100) if fields else 0

    return {
        'customer': customer,
        'total_invoices': invoices.count(),
        'total_repairs': repairs.count(),
        'total_payments': payments.aggregate(t=Sum('amount'))['t'] or Decimal('0'),
        'profile_completion': completion,
        'last_invoice': invoices.order_by('-date').first(),
        'last_repair': repairs.order_by('-date_in').first(),
    }


@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def profile(request):
    """Profile page — full page for GET, partial for HTMX."""
    customer = _get_customer(request)
    context = _profile_context(customer, request)

    if is_htmx(request):
        return render(request, 'customer/partials/profile_content.html', context)
    return render(request, 'customer/profile.html', context)


@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_profile')
def profile_update(request):
    """Update customer profile — HTMX aware."""
    customer = _get_customer(request)

    if request.method == 'POST':
        form = CustomerProfileForm(request.POST, instance=customer)
        if form.is_valid():
            form.save()
            # refresh from DB to reflect changes
            customer.refresh_from_db()
            messages.success(request, "Profile updated successfully.")

            if is_htmx(request):
                return htmx_response(
                    request,
                    'customer/partials/profile_content.html',
                    context=_profile_context(customer, request),
                    toast={'level': 'success', 'message': 'Profile updated successfully.'},
                )
            return redirect('customer:customer_profile')

        # Form invalid
        if is_htmx(request):
            return render(request, 'customer/profile_edit.html',
                          {'form': form, 'customer': customer}, status=400)

    else:
        form = CustomerProfileForm(instance=customer)

    return render(request, 'customer/profile_edit.html',
                  {'form': form, 'customer': customer})


# ════════════════════════════════════════════════════════════
# 10. PASSWORD CHANGE
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='customer:customer_profile')
def customer_password_change(request):
    """Change password (logged in) — HTMX aware."""
    if request.method == 'POST':
        form = PasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)   # keep session alive
            customer = _get_customer(request)
            messages.success(request, "Password changed successfully!")

            if is_htmx(request):
                return htmx_response(
                    request,
                    'customer/partials/profile_content.html',
                    context=_profile_context(customer, request),
                    toast={
                        'level': 'success',
                        'message': 'Password changed successfully!',
                        'title': 'Security Updated',
                    },
                )
            return redirect('customer:customer_profile')

        # Invalid
        if is_htmx(request):
            return render(
                request,
                'customer/partials/_password_change_form.html',
                {'form': form},
                status=400,
            )
    else:
        form = PasswordChangeForm(user=request.user)

    if is_htmx(request):
        return render(request, 'customer/partials/_password_change_form.html', {'form': form})
    return render(request, 'customer/password_change.html', {'form': form})


# ════════════════════════════════════════════════════════════
# 11. EMAIL CHANGE (OTP-based)
# ════════════════════════════════════════════════════════════
@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_profile')
def email_change_request(request):
    """
    Step 1: Request email change.

    Accepts:
      - POST (form): submits new_email → sends OTP
      - POST (JSON {"resend": true}): resends OTP (60s cooldown)
      - GET: render form or OTP partial if session active
    """
    customer = _get_customer(request)

    # ── JSON resend ──────────────────────────────────
    if (request.method == 'POST'
            and request.headers.get('Content-Type') == 'application/json'):
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON.'}, status=400)

        if not payload.get('resend'):
            return JsonResponse({'error': 'Invalid request.'}, status=400)

        new_email = request.session.get(SK_PENDING_NEW_EMAIL)
        if not new_email:
            return JsonResponse({'error': 'Session expired.'}, status=400)

        # Cooldown
        last_otp = (
            EmailOTP.objects
            .filter(email=new_email, purpose='change_email')
            .order_by('-created_at')
            .first()
        )
        if last_otp:
            elapsed = (timezone.now() - last_otp.created_at).total_seconds()
            if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
                wait = OTP_RESEND_COOLDOWN_SECONDS - int(elapsed)
                return JsonResponse(
                    {'error': f'Please wait {wait} seconds.'},
                    status=429,
                )

        if create_and_send_otp(request.user, new_email, 'change_email'):
            return JsonResponse({'success': True})
        return JsonResponse({'error': 'Failed to send OTP.'}, status=500)

    # ── Form submit ──────────────────────────────────
    if request.method == 'POST':
        form = EmailChangeRequestForm(request.user, request.POST)
        if form.is_valid():
            new_email = form.cleaned_data['new_email']
            if create_and_send_otp(request.user, new_email, 'change_email'):
                # Session
                request.session[SK_PENDING_NEW_EMAIL] = new_email
                request.session[SK_PENDING_USER_ID] = request.user.id
                request.session[SK_OTP_PURPOSE] = 'change_email'

                if is_htmx(request):
                    response = render(
                        request,
                        'customer/partials/email_change_otp.html',
                        {'email': new_email},
                    )
                    response['HX-Retarget'] = '#profile-container'
                    response['HX-Reswap'] = 'innerHTML'
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'success',
                            'message': f'OTP sent to {new_email}',
                        },
                    })
                    return response

                messages.success(request, f"OTP sent to {new_email}")
                return redirect('customer:email_change_verify')

            # Send failed
            if is_htmx(request):
                response = render(
                    request,
                    'customer/partials/email_change_form.html',
                    {'form': form},
                )
                response['HX-Retarget'] = '#profile-container'
                response['HX-Reswap'] = 'innerHTML'
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'danger',
                        'message': 'Failed to send OTP. Please try again.',
                    },
                })
                return response

            messages.error(request, "Failed to send OTP. Please try again.")

        # Form invalid
        if is_htmx(request):
            response = render(
                request,
                'customer/partials/email_change_form.html',
                {'form': form},
            )
            response['HX-Retarget'] = '#profile-container'
            response['HX-Reswap'] = 'innerHTML'
            return response

    else:
        form = EmailChangeRequestForm(request.user)

    if is_htmx(request):
        return render(request, 'customer/partials/email_change_form.html', {'form': form})
    return render(request, 'customer/email_change.html',
                  {'form': form, 'customer': customer})


@csrf_protect
@login_required
@handle_errors(default_redirect='customer:customer_profile')
def email_change_verify(request):
    """
    Step 2: Verify OTP and update email.

    On success:
      - Update User.email + Contact.email
      - Delete all old 'change_email' OTPs
      - Send confirmation email
      - Logout user (force re-login with new email)
    """
    user = request.user
    customer = _get_customer(request)

    new_email = request.session.get(SK_PENDING_NEW_EMAIL)
    purpose = request.session.get(SK_OTP_PURPOSE)

    # Session expired
    if not new_email or purpose != 'change_email':
        messages.error(request, "Invalid session. Please request email change again.")
        if is_htmx(request):
            response = render(
                request,
                'customer/partials/email_change_form.html',
                {'form': EmailChangeRequestForm(user)},
            )
            response['HX-Retarget'] = '#profile-container'
            response['HX-Reswap'] = 'innerHTML'
            response['HX-Trigger'] = json.dumps({
                'showToast': {
                    'level': 'danger',
                    'message': 'Invalid session. Please try again.',
                },
            })
            return response
        return redirect('customer:email_change_request')

    if request.method == 'POST':
        otp = (request.POST.get('otp') or '').strip()

        # Malformed
        if len(otp) != 6 or not otp.isdigit():
            if is_htmx(request):
                response = render(
                    request,
                    'customer/partials/email_change_otp.html',
                    {'email': new_email},
                )
                response['HX-Retarget'] = '#profile-container'
                response['HX-Reswap'] = 'innerHTML'
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'danger',
                        'message': 'Please enter a valid 6-digit OTP.',
                    },
                })
                return response
            messages.error(request, "Please enter a valid 6-digit OTP.")
            return render(request, 'customer/email_change_otp.html', {'email': new_email})

        # Verify
        verified_user = verify_otp(new_email, otp, 'change_email')

        if verified_user and verified_user.id == user.id:
            with transaction.atomic():
                user.email = new_email
                user.save(update_fields=['email'])
                customer.email = new_email
                customer.save(update_fields=['email'])

                # Cleanup session + old OTPs
                for key in (SK_PENDING_NEW_EMAIL, SK_PENDING_USER_ID, SK_OTP_PURPOSE):
                    request.session.pop(key, None)
                EmailOTP.objects.filter(user=user, purpose='change_email').delete()

            # Confirmation email (fail-safe)
            try:
                send_mail(
                    subject="Your email has been changed",
                    message=(
                        f"Your A1 Computer Solutions account email was changed to "
                        f"{new_email}. If you didn't request this, please contact "
                        f"support immediately."
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[new_email],
                    fail_silently=True,
                )
            except Exception:
                logger.exception("Email change confirmation email failed")

            # Force re-login
            from django.contrib.auth import logout
            logout(request)

            if is_htmx(request):
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:login')
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'success',
                        'message': 'Email changed! Please login with your new email.',
                        'title': 'Security Updated',
                    },
                })
                return response

            messages.success(
                request,
                "Email updated successfully. Please login with your new email.",
            )
            return redirect('accounting:login')

        # Invalid OTP
        if is_htmx(request):
            response = render(
                request,
                'customer/partials/email_change_otp.html',
                {'email': new_email},
            )
            response['HX-Retarget'] = '#profile-container'
            response['HX-Reswap'] = 'innerHTML'
            response['HX-Trigger'] = json.dumps({
                'showToast': {
                    'level': 'danger',
                    'message': 'Invalid or expired OTP. Please try again.',
                },
            })
            return response
        messages.error(request, "Invalid or expired OTP. Please try again.")
        return render(request, 'customer/email_change_otp.html', {'email': new_email})

    # GET
    if is_htmx(request):
        return render(request, 'customer/partials/email_change_otp.html',
                      {'email': new_email})
    return render(request, 'customer/email_change_otp.html', {'email': new_email})


# ════════════════════════════════════════════════════════════
# 12. NOTIFICATIONS (Customer)
# ════════════════════════════════════════════════════════════
def _customer_base_queryset(request):
    return request.user.notifications.all()


def _render_customer_dropdown(request):
    notifications = list(_customer_base_queryset(request)[:DROPDOWN_LIMIT])
    return render(request, 'customer/partials/dropdown.html', {
        'notifications': notifications,
        'unread_count': get_unread_count(request.user),
    })


def _render_customer_list(request, page_obj=None):
    if page_obj is None:
        paginator = Paginator(_customer_base_queryset(request), NOTIFICATION_PAGE_SIZE)
        page_obj = paginator.get_page(request.GET.get('page', 1))
    return render(request, 'customer/partials/_notification_items.html', {
        'page_obj': page_obj,
        'unread_count': get_unread_count(request.user),
    })


def _customer_htmx_partial(request):
    target = request.headers.get('HX-Target', '').lower()
    if 'dropdown' in target:
        return _render_customer_dropdown(request)
    return _render_customer_list(request)


@login_required
@handle_errors(default_redirect='customer:customer_dashboard')
def notification_list(request):
    paginator = Paginator(_customer_base_queryset(request), NOTIFICATION_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    if is_htmx(request):
        return _render_customer_list(request, page_obj)

    return render(request, 'customer/notification_list.html', {
        'notifications': page_obj,
        'page_obj': page_obj,
        'unread_count': get_unread_count(request.user),
    })


@login_required
def notification_dropdown(request):
    try:
        return _render_customer_dropdown(request)
    except Exception:
        logger.exception("Customer dropdown failed | user=%s", request.user.id)
        return HttpResponse(
            '<div class="dropdown-item text-danger">Error loading</div>',
            status=500,
        )


@login_required
def notification_mark_all_read(request):
    if request.method != 'POST':
        return HttpResponse("Method not allowed", status=405)
    request.user.notifications.filter(is_read=False).update(is_read=True)
    if is_htmx(request):
        return _customer_htmx_partial(request)
    messages.success(request, "All notifications marked read.")
    return redirect('customer:customer_notifications')


@login_required
def notification_mark_read(request, pk):
    if request.method != 'POST':
        return HttpResponse("Method not allowed", status=405)
    notif = get_object_or_404(Notification, pk=pk, recipient=request.user)
    if not notif.is_read:
        notif.is_read = True
        notif.save(update_fields=['is_read'])
    if is_htmx(request):
        return _customer_htmx_partial(request)
    return HttpResponse("Marked read", status=200)


@login_required
def notification_delete(request, pk):
    if request.method != 'DELETE':
        return HttpResponse("Method not allowed", status=405)
    notif = get_object_or_404(Notification, pk=pk, recipient=request.user)
    notif.delete()
    if is_htmx(request):
        return _customer_htmx_partial(request)
    return HttpResponse("Deleted", status=200)


@login_required
def notification_delete_all(request):
    if request.method != 'DELETE':
        return HttpResponse("Method not allowed", status=405)
    request.user.notifications.all().delete()
    if is_htmx(request):
        return _customer_htmx_partial(request)
    messages.success(request, "All notifications deleted.")
    return redirect('customer:customer_notifications')


@login_required
def unread_count_text(request):
    return HttpResponse(str(get_unread_count(request.user)))


# ════════════════════════════════════════════════════════════
# 13. EXCEL EXPORT HELPERS
# ════════════════════════════════════════════════════════════
def _require_openpyxl():
    if openpyxl is None:
        return toast_only_response(
            {'level': 'danger', 'message': 'Openpyxl library is not installed.'},
            status=400,
        )
    return None


def _customer_repairs_excel(request, customer, qs):
    """Customer repairs → Excel. Same filters as list view."""
    err = _require_openpyxl()
    if err:
        return err

    company = CompanyProfile.get_instance()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "My Repairs"

    # Styles
    title_font = Font(bold=True, size=16, color="FFFFFF")
    title_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
    total_font = Font(bold=True, size=11, color="FFFFFF")
    total_fill = PatternFill(start_color="375623", end_color="375623", fill_type="solid")
    thin = Side(style='thin', color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    money = '#,##0.00'
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center', wrap_text=True)
    right = Alignment(horizontal='right', vertical='center')

    total_cols = 20
    last_col = get_column_letter(total_cols)

    # Row 1 - Company
    ws.merge_cells(f'A1:{last_col}1')
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = title_font
    ws['A1'].fill = title_fill
    ws['A1'].alignment = center
    ws.row_dimensions[1].height = 30

    # Row 2 - Subtitle
    ws.merge_cells(f'A2:{last_col}2')
    ws['A2'] = f"MY REPAIR HISTORY — {customer.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws['A2'].alignment = center
    ws.row_dimensions[2].height = 25

    # Row 3 - Generated info
    ws.merge_cells(f'A3:{last_col}3')
    ws['A3'] = f"Generated: {timezone.now().strftime('%d-%m-%Y %H:%M')} | Total: {qs.count()}"
    ws['A3'].font = Font(size=10, italic=True, color="555555")
    ws['A3'].alignment = center

    # Row 5 - Headers
    headers = [
        ('Job #', 14), ('Submitted', 13), ('Received', 13), ('Ready', 13),
        ('Delivered', 13), ('Device Model', 22), ('Serial #', 16),
        ('Issue', 30), ('Diagnosis', 30), ('Action Taken', 30),
        ('Status', 22), ('Estimate Status', 14),
        ('Estimated (₹)', 16), ('Labour (₹)', 12),
        ('Parts (₹)', 12), ('Final (₹)', 16),
        ('Received By', 14), ('Delivered By', 14),
        ('Recipient', 18), ('Invoice #', 14),
    ]
    for col, (h, w) in enumerate(headers, 1):
        c = ws.cell(row=5, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center
        c.border = border
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.row_dimensions[5].height = 30

    # Data rows
    row_num = 6
    totals = {'est': Decimal('0'), 'lab': Decimal('0'), 'parts': Decimal('0'), 'fin': Decimal('0')}

    for repair in qs:
        parts_total = sum(p.line_total for p in repair.parts.all())
        row_data = [
            repair.job_number,
            repair.submitted_at.strftime('%d-%m-%Y') if repair.submitted_at
                else (repair.date_in.strftime('%d-%m-%Y') if repair.date_in else ''),
            repair.received_at.strftime('%d-%m-%Y') if repair.received_at else '',
            repair.ready_at.strftime('%d-%m-%Y') if repair.ready_at else '',
            repair.delivery_date.strftime('%d-%m-%Y') if repair.delivery_date else '',
            repair.device_model or '',
            repair.serial_number or '',
            repair.issue_description or '',
            repair.diagnosis_report or '',
            repair.action_taken or '',
            repair.get_status_display(),
            repair.get_estimate_status_display() if repair.estimate_status else 'No Estimate',
            float(repair.estimated_cost or 0),
            float(repair.labour_charge or 0),
            float(parts_total),
            float(repair.final_amount or 0),
            repair.received_by or '',
            repair.delivered_by or '',
            repair.delivered_to_name or '',
            repair.invoice.invoice_number if repair.invoice else '',
        ]

        for col, val in enumerate(row_data, 1):
            c = ws.cell(row=row_num, column=col, value=val)
            c.border = border
            c.alignment = left

        for col in [13, 14, 15, 16]:
            ws.cell(row=row_num, column=col).alignment = right
            ws.cell(row=row_num, column=col).number_format = money
        for col in [1, 2, 3, 4, 5, 11, 12, 20]:
            ws.cell(row=row_num, column=col).alignment = center

        # Alternate fill
        if row_num % 2 == 0:
            fill = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
            for c in range(1, total_cols + 1):
                ws.cell(row=row_num, column=c).fill = fill

        totals['est'] += Decimal(str(repair.estimated_cost or 0))
        totals['lab'] += Decimal(str(repair.labour_charge or 0))
        totals['parts'] += parts_total
        totals['fin'] += Decimal(str(repair.final_amount or 0))
        row_num += 1

    # Totals row
    ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=12)
    c = ws.cell(row=row_num, column=1, value="GRAND TOTAL")
    c.font = total_font
    c.fill = total_fill
    c.alignment = right
    for col in range(2, 13):
        ws.cell(row=row_num, column=col).fill = total_fill
        ws.cell(row=row_num, column=col).border = border
    for col, key in [(13, 'est'), (14, 'lab'), (15, 'parts'), (16, 'fin')]:
        c = ws.cell(row=row_num, column=col, value=float(totals[key]))
        c.font = total_font
        c.fill = total_fill
        c.alignment = right
        c.number_format = money
        c.border = border
    for col in range(17, 21):
        ws.cell(row=row_num, column=col).fill = total_fill
        ws.cell(row=row_num, column=col).border = border

    ws.freeze_panes = 'A6'
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f'attachment; filename="my_repairs_{timezone.now().strftime("%Y%m%d_%H%M%S")}.xlsx"'
    )
    wb.save(response)
    return response


@login_required
@handle_errors(default_redirect='customer:customer_repairs')
def customer_repairs_excel(request):
    """Wrapper for repairs excel export with filters."""
    customer = _get_customer(request)

    qs = RepairJob.objects.filter(customer=customer).order_by('-date_in')

    status_filter = request.GET.get('status', '')
    search = request.GET.get('search', '').strip()
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    if status_filter:
        qs = qs.filter(status=status_filter)
    if search:
        qs = qs.filter(
            Q(job_number__icontains=search) |
            Q(device_model__icontains=search) |
            Q(issue_description__icontains=search)
        )
    if date_from:
        qs = qs.filter(date_in__gte=date_from)
    if date_to:
        qs = qs.filter(date_in__lte=date_to)

    # Prefetch parts to avoid N+1
    qs = qs.prefetch_related('parts')

    return _customer_repairs_excel(request, customer, qs)


def _customer_invoices_excel(customer, qs, request):
    """Invoices → Excel."""
    err = _require_openpyxl()
    if err:
        return err

    company = CompanyProfile.get_instance()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "My Invoices"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    money = '#,##0.00'

    ws.merge_cells('A1:H1')
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A2:H2')
    ws['A2'] = f"My Invoices — {customer.name}"
    ws['A2'].font = Font(bold=True, size=12)

    headers = ['Invoice #', 'Date', 'Due Date', 'GST Type', 'Subtotal', 'Tax', 'Grand Total', 'Status']
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=4, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.border = border
        c.alignment = Alignment(horizontal='center')

    row = 5
    total = Decimal('0')
    for inv in qs:
        total += inv.grand_total
        ws.cell(row=row, column=1, value=inv.invoice_number)
        ws.cell(row=row, column=2, value=inv.date.strftime("%d-%m-%Y"))
        ws.cell(row=row, column=3, value=inv.due_date.strftime("%d-%m-%Y") if inv.due_date else '')
        ws.cell(row=row, column=4, value=inv.get_gst_type_display())
        ws.cell(row=row, column=5, value=float(inv.subtotal)).number_format = money
        ws.cell(row=row, column=6, value=float(inv.tax_amount)).number_format = money
        ws.cell(row=row, column=7, value=float(inv.grand_total)).number_format = money
        ws.cell(row=row, column=8, value=inv.get_payment_status_display())
        for c in range(1, 9):
            ws.cell(row=row, column=c).border = border
        row += 1

    ws.cell(row=row, column=1, value="Total").font = Font(bold=True)
    ws.cell(row=row, column=7, value=float(total)).number_format = money
    for c in range(1, 9):
        ws.cell(row=row, column=c).border = border

    for i, w in enumerate([18, 14, 14, 18, 14, 14, 16, 14], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = 'A5'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f'attachment; filename="invoices_{customer.name}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
    )
    wb.save(response)
    return response


def _customer_payments_excel(customer, qs):
    """Payments → Excel."""
    err = _require_openpyxl()
    if err:
        return err

    company = CompanyProfile.get_instance()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Payments"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin = Side(style='thin')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    money = '#,##0.00'

    ws.merge_cells('A1:G1')
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A2:G2')
    ws['A2'] = f"Payment History — {customer.name}"
    ws['A2'].font = Font(bold=True, size=12)

    headers = ['Date', 'Invoice / Reference', 'Amount (₹)', 'Method', 'UPI Ref', 'Reference', 'Description']
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=4, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.border = border

    row = 5
    total = Decimal('0')
    for pay in qs:
        allocations = list(pay.allocations.all())
        if allocations:
            inv_nos = [a.invoice.invoice_number for a in allocations if a.invoice]
            ref = ', '.join(inv_nos) if inv_nos else '—'
        elif pay.is_advance:
            ref = 'Advance Payment'
        else:
            ref = 'Direct Payment'
        if pay.discount_amount and pay.discount_amount > 0:
            ref += f' (Disc: ₹{pay.discount_amount})'

        total += pay.amount
        ws.cell(row=row, column=1, value=pay.date.strftime("%d-%m-%Y"))
        ws.cell(row=row, column=2, value=ref)
        ws.cell(row=row, column=3, value=float(pay.amount)).number_format = money
        ws.cell(row=row, column=4, value=pay.get_method_display())
        ws.cell(row=row, column=5, value=pay.upi_ref or '')
        ws.cell(row=row, column=6, value=pay.reference or '')
        ws.cell(row=row, column=7, value=pay.description or '')
        for c in range(1, 8):
            ws.cell(row=row, column=c).border = border
        row += 1

    ws.cell(row=row, column=2, value="Total").font = Font(bold=True)
    ws.cell(row=row, column=3, value=float(total)).number_format = money
    for c in range(1, 8):
        ws.cell(row=row, column=c).border = border
        ws.cell(row=row, column=c).font = Font(bold=True)

    for i, w in enumerate([15, 25, 15, 15, 20, 18, 35], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = 'A5'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f'attachment; filename="payments_{customer.name}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
    )
    wb.save(response)
    return response


def _customer_statement_excel(customer, request):
    """Statement → Excel (dual or single)."""
    err = _require_openpyxl()
    if err:
        return err

    date_from = request.GET.get('date_from', '').strip()
    date_to = request.GET.get('date_to', '').strip()
    txn_type = request.GET.get('txn_type', '')
    search = request.GET.get('search', '').strip()

    data = _build_combined_rows(
        customer, date_from or None, date_to or None,
        txn_type or None, search or None,
    )
    rows = data.pop('rows')

    show_dual = (customer.contact_type == 'both')

    # Zero-out hidden side
    if not show_dual:
        if customer.contact_type == 'vendor':
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
    thin = Side(style='thin', color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center')
    right = Alignment(horizontal='right', vertical='center')
    money = '#,##0.00'

    company = CompanyProfile.get_instance()
    total_cols = 9 if show_dual else 6
    last_col = get_column_letter(total_cols)

    ws.merge_cells(f'A1:{last_col}1')
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = Font(bold=True, size=14)
    ws['A1'].alignment = center

    ws.merge_cells(f'A2:{last_col}2')
    ws['A2'] = f"Statement — {customer.name}"
    ws['A2'].font = Font(bold=True, size=12)
    ws['A2'].alignment = center

    # ─── Simple (non-dual) headers ───
    headers = ['Date', 'Particulars', 'Debit (₹)', 'Credit (₹)', 'Balance (₹)']
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=4, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center
        c.border = border

    row = 5
    ws.cell(row=row, column=2, value='Opening Balance').font = Font(bold=True)
    ws.cell(row=row, column=5, value=float(data['opening_recv'])).number_format = money
    for c in range(1, 6):
        ws.cell(row=row, column=c).border = border
    row += 1

    for r in rows:
        ws.cell(row=row, column=1, value=r['date'].strftime('%d-%m-%Y'))
        desc = r['description']
        if r.get('reference'):
            desc += f"  [{r['reference']}]"
        ws.cell(row=row, column=2, value=desc)
        ws.cell(row=row, column=3, value=float(r['recv_dr']) if r['recv_dr'] else '')
        ws.cell(row=row, column=4, value=float(r['recv_cr']) if r['recv_cr'] else '')
        ws.cell(row=row, column=5, value=float(r['running_recv']))
        for c in range(1, 6):
            ws.cell(row=row, column=c).border = border
            if c in (3, 4, 5):
                ws.cell(row=row, column=c).number_format = money
                ws.cell(row=row, column=c).alignment = right
        row += 1

    ws.cell(row=row, column=2, value='Closing Balance').font = Font(bold=True)
    ws.cell(row=row, column=5, value=float(data['closing_recv'])).number_format = money
    for c in range(1, 6):
        ws.cell(row=row, column=c).border = border
        ws.cell(row=row, column=c).font = Font(bold=True)

    for i, w in enumerate([15, 50, 15, 15, 15], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    safe = customer.name.replace(' ', '_').replace('/', '_')
    response['Content-Disposition'] = (
        f'attachment; filename="statement_{safe}_{datetime.now().strftime("%Y%m%d")}.xlsx"'
    )
    wb.save(response)
    return response