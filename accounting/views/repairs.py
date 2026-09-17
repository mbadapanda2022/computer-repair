# accounting/views/repairs.py
"""
Staff Portal — Repair Jobs views.

Enhancements:
─────────────
- Removed no-op send_notification_sse loops (SSE removed earlier).
- Added select_related / prefetch_related for N+1 prevention.
- Added aging calculation + activity timeline for detail view.
- Enhanced list stats: overdue, this_week, in_progress.
"""

import json
import logging
import urllib.parse
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import F, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from ..decorators import handle_errors
from ..forms import RepairJobForm, RepairPartForm
from ..models import *
from ..models import sync_invoice_ledger
from ..utils.notification_helpers import send_notification_to_contact
from .utils import htmx_response, is_htmx, redirect_to_staff, toast_only_response

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════
STATUS_ORDER = ['pending', 'received', 'diagnosis', 'repairing', 'ready', 'delivered']
AGING_WARNING_DAYS = 4      # yellow after this many days
AGING_URGENT_DAYS = 8       # red after this many days
WARRANTY_DAYS = 30          # default warranty period


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════
def _compute_aging(job):
    """
    Return (days, level) for jobs still active.
    level: 'ok' | 'warning' | 'danger' | None (if done)
    """
    if job.status in ('delivered', 'cancelled'):
        return None, None

    today = timezone.now().date()
    if job.received_at:
        days = (today - job.received_at).days
    elif job.submitted_at:
        days = (today - job.submitted_at.date()).days
    else:
        days = (today - job.date_in).days

    if days <= AGING_WARNING_DAYS - 1:
        level = 'ok'
    elif days <= AGING_URGENT_DAYS - 1:
        level = 'warning'
    else:
        level = 'danger'

    return days, level


def _build_status_pipeline(job):
    """
    Build visual pipeline steps from STATUS_ORDER.
    Returns list of {key, label, state} where state ∈ {'done','current','pending'}.
    """
    if job.status == 'cancelled':
        return [{'key': 'cancelled', 'label': 'Cancelled', 'state': 'current'}]

    try:
        current_idx = STATUS_ORDER.index(job.status)
    except ValueError:
        current_idx = 0

    label_map = dict(RepairJob.STATUS_CHOICES)
    pipeline = []
    for i, key in enumerate(STATUS_ORDER):
        if i < current_idx:
            state = 'done'
        elif i == current_idx:
            state = 'current'
        else:
            state = 'pending'
        pipeline.append({
            'key': key,
            'label': label_map.get(key, key),
            'state': state,
        })
    return pipeline


def _build_activity_timeline(job):
    """Chronological activity from existing timestamps."""
    events = []

    if job.submitted_at:
        events.append({
            'label': 'Submitted Online',
            'at': job.submitted_at,
            'icon': 'cloud-upload',
            'color': 'info',
            'desc': 'Customer submitted repair request via portal',
        })

    if job.received_at:
        desc = 'Device physically received at shop'
        if job.received_by:
            desc += f' — by {job.received_by}'
        events.append({
            'label': 'Received at Shop',
            'at': job.received_at,
            'icon': 'box-arrow-in-down',
            'color': 'primary',
            'desc': desc,
        })

    if job.estimate_approved_at:
        if job.estimate_status == 'approved':
            icon, color = 'check-circle', 'success'
        elif job.estimate_status == 'on_hold':
            icon, color = 'pause-circle', 'warning'
        else:
            icon, color = 'x-circle', 'danger'

        parts = []
        if job.approval_source:
            parts.append(f"via {job.get_approval_source_display()}")
        if job.estimate_approved_by:
            name = job.estimate_approved_by.get_full_name() or job.estimate_approved_by.username
            parts.append(f"by {name}")

        events.append({
            'label': f'Estimate {job.get_estimate_status_display()}',
            'at': job.estimate_approved_at,
            'icon': icon,
            'color': color,
            'desc': ' '.join(parts),
        })

    if job.ready_at:
        events.append({
            'label': 'Ready for Delivery',
            'at': job.ready_at,
            'icon': 'check2-circle',
            'color': 'primary',
            'desc': 'Repair completed, awaiting customer pickup',
        })

    if job.delivered_at:
        if job.delivered_to_name:
            desc = f'Handed over to {job.delivered_to_name}'
            if job.delivered_to_designation:
                desc += f' ({job.delivered_to_designation})'
        elif job.delivered_by:
            desc = f'Delivered by {job.delivered_by}'
        else:
            desc = 'Device handed over to customer'

        events.append({
            'label': 'Delivered',
            'at': job.delivered_at,
            'icon': 'truck',
            'color': 'success',
            'desc': desc,
        })

    return events


def _compute_warranty(job):
    """Return (days_remaining, expires_on) if delivered, else (None, None)."""
    if job.status != 'delivered' or not job.delivery_date:
        return None, None
    expires_on = job.delivery_date + timedelta(days=WARRANTY_DAYS)
    days_remaining = (expires_on - timezone.now().date()).days
    return max(days_remaining, 0), expires_on


# ════════════════════════════════════════════════════════════
# HELPER: PAGINATED REPAIRS CONTEXT
# ════════════════════════════════════════════════════════════
def get_paginated_repairs_context(request, queryset=None):
    if queryset is None:
        queryset = (
            RepairJob.objects
            .select_related('customer', 'invoice')
            .order_by('-created_at')
        )

    today = timezone.now().date()
    week_start = today - timedelta(days=7)

    all_jobs = RepairJob.objects.all()
    stats = {
        'total_count': all_jobs.count(),
        'pending_count': all_jobs.filter(
            status__in=['pending', 'received', 'diagnosis', 'repairing']
        ).count(),
        'completed_count': all_jobs.filter(status='delivered').count(),
        'urgent_count': all_jobs.filter(
            estimate_status='pending', estimated_cost__isnull=False
        ).count(),
        'overdue_count': all_jobs.filter(
            status__in=['received', 'diagnosis', 'repairing'],
            received_at__lt=today - timedelta(days=AGING_URGENT_DAYS),
        ).count(),
        'this_week_count': all_jobs.filter(date_in__gte=week_start).count(),
    }

    search = request.GET.get('search', '').strip()
    status = request.GET.get('status', '')
    customer_id = request.GET.get('customer', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    page_number = request.GET.get('page', 1)

    if search:
        queryset = queryset.filter(
            Q(job_number__icontains=search) |
            Q(customer__name__icontains=search) |
            Q(device_model__icontains=search) |
            Q(serial_number__icontains=search)
        )
    if status:
        queryset = queryset.filter(status=status)
    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)
    if date_from:
        queryset = queryset.filter(date_in__gte=date_from)
    if date_to:
        queryset = queryset.filter(date_in__lte=date_to)

    filtered_total = queryset.aggregate(total=Sum('final_amount'))['total'] or Decimal('0')

    paginator = Paginator(queryset, 20)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Attach aging to each object (no extra DB hits)
    jobs = list(page_obj.object_list)
    for job in jobs:
        days, level = _compute_aging(job)
        job.aging_days = days
        job.aging_level = level

    customers = Contact.objects.filter(
        contact_type__in=['customer', 'both']
    ).order_by('name')

    return {
        'jobs': jobs,
        'page_obj': page_obj,
        'customers': customers,
        'search': search,
        'status_filter': status,
        'customer_id': customer_id,
        'date_from': date_from,
        'date_to': date_to,
        'status_choices': RepairJob.STATUS_CHOICES,
        'stats': stats,
        'filtered_total': filtered_total,
        'today': today,
    }


# ════════════════════════════════════════════════════════════
# 1. FIELD VALIDATION (HTMX)
# ════════════════════════════════════════════════════════════
@login_required
def validate_repair_field(request):
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    value = request.GET.get(field_name, '')
    repair_id = request.GET.get('repair_id')

    try:
        if repair_id:
            instance = RepairJob.objects.get(pk=repair_id)
            form = RepairJobForm(data={field_name: value}, instance=instance)
        else:
            form = RepairJobForm(data={field_name: value})
        form.full_clean()
        errors = form.errors.get(field_name, [])
        html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
        html += ''.join(f'<div>{err}</div>' for err in errors)
        html += '</div>'
        return HttpResponse(html)
    except Exception as e:
        logger.error(f"Validation error on {field_name}: {e}")
        return HttpResponse(
            f'<div id="field-{field_name}" class="invalid-feedback d-block">'
            f'Server validation error</div>'
        )


# ════════════════════════════════════════════════════════════
# 2. REPAIR LIST
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='accounting:repair_list')
def repair_list(request):
    if request.GET.get('reset'):
        return redirect('accounting:repair_list')

    context = get_paginated_repairs_context(request)

    if is_htmx(request):
        return render(request, 'repairs/partials/repair_table.html', context)
    return render(request, 'repairs/repair_list.html', context)


# ════════════════════════════════════════════════════════════
# 3. PRINT LIST
# ════════════════════════════════════════════════════════════
@login_required
def repair_list_print(request):
    search = request.GET.get('search', '')
    status = request.GET.get('status', '')
    customer_id = request.GET.get('customer', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    jobs = (
        RepairJob.objects
        .select_related('customer', 'invoice')
        .order_by('-date_in')
    )
    if search:
        jobs = jobs.filter(
            Q(job_number__icontains=search) |
            Q(customer__name__icontains=search) |
            Q(device_model__icontains=search) |
            Q(serial_number__icontains=search)
        )
    if status:
        jobs = jobs.filter(status=status)
    if customer_id:
        jobs = jobs.filter(customer_id=customer_id)
    if date_from:
        jobs = jobs.filter(date_in__gte=date_from)
    if date_to:
        jobs = jobs.filter(date_in__lte=date_to)

    total_amount = jobs.aggregate(total=Sum('final_amount'))['total'] or Decimal('0')
    company = CompanyProfile.get_instance()
    logo_exists = bool(company.logo and company.logo.name)

    customer_name = None
    if customer_id:
        try:
            customer_name = Contact.objects.get(pk=customer_id).name
        except Contact.DoesNotExist:
            pass

    return render(request, 'repairs/repair_list_print.html', {
        'jobs': jobs,
        'company': company,
        'logo_exists': logo_exists,
        'total_amount': total_amount,
        'search': search,
        'status_filter': status,
        'customer_name': customer_name,
        'customer_id': customer_id,
        'date_from': date_from,
        'date_to': date_to,
    })


# ════════════════════════════════════════════════════════════
# 4. REPAIR CREATE
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list',
               htmx_template='repairs/partials/repair_form_modal.html')
def repair_create(request):
    template_name = (
        'repairs/partials/repair_form_modal.html'
        if is_htmx(request) else 'repairs/repair_form.html'
    )

    if request.method == 'POST':
        form = RepairJobForm(request.POST)
        if form.is_valid():
             with transaction.atomic():
                job = form.save(commit=False)
                now = timezone.now()
                job.date_in = now.date()

                if job.status == 'pending':
                    job.status = 'received'
                if not job.received_at:
                    job.received_at = now.date()
                job.save()

                send_notification_to_contact(
                    job.customer,
                    title=f"Repair Job Created: {job.job_number}",
                    message=f"Your repair for {job.device_model} has been received at the shop.",
                    link=reverse('customer:customer_repair_detail', args=[job.pk]),
                    notif_type='success',
                    category='repairs',
                    send_email=False,
                )

                if is_htmx(request):
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:repair_detail', args=[job.pk])
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'success',
                            'message': f'Repair job {job.job_number} created.',
                        },
                    })
                    return response

                messages.success(request, f"Repair job {job.job_number} created.")
                return redirect_to_staff('repair_detail', pk=job.pk)
        else:
            if is_htmx(request):
                return render(request, 'repairs/partials/repair_form_modal.html',
                              {'form': form})
    else:
        form = RepairJobForm()

    return render(request, template_name, {'form': form})


# ════════════════════════════════════════════════════════════
# 5. REPAIR UPDATE
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list',
               htmx_template='repairs/partials/repair_form_modal.html')
def repair_update(request, pk):
    job = get_object_or_404(
        RepairJob.objects.select_related('customer'), pk=pk
    )
    template_name = (
        'repairs/partials/repair_form_modal.html'
        if is_htmx(request) else 'repairs/repair_form.html'
    )

    if request.method == 'POST':
        # Capture pre-form status so we can detect a status change.
        old_status = job.status
        form = RepairJobForm(request.POST, instance=job)

        if form.is_valid():
            with transaction.atomic():
                job = form.save(commit=False)

                if job.status == 'delivered' and not job.delivery_date:
                    job.delivery_date = timezone.now().date()

                job.save()

                parts_total = job.parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
                job.final_amount = parts_total + job.labour_charge
                job.save(update_fields=['final_amount'])

                if old_status == job.status:
                    send_notification_to_contact(
                        job.customer,
                        title=f"Repair Job Updated: {job.job_number}",
                        message=f"Your repair for {job.device_model} has been updated.",
                        link=reverse('customer:customer_repair_detail', args=[job.pk]),
                        notif_type='info',
                        category='repairs',
                        send_email=False,
                    )

                if is_htmx(request):
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:repair_detail', args=[job.pk])
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'success',
                            'message': f'Repair job {job.job_number} updated.',
                        },
                    })
                    return response

                messages.success(request, f"Repair job {job.job_number} updated.")
                return redirect_to_staff('repair_detail', pk=job.pk)

        else:
            if is_htmx(request):
                return render(request, 'repairs/partials/repair_form_modal.html',
                              {'form': form, 'job': job})
            return render(request, template_name, {'form': form, 'job': job})

    else:
        form = RepairJobForm(instance=job)
        return render(request, template_name, {'form': form, 'job': job})


# ════════════════════════════════════════════════════════════
# 6. REPAIR DETAIL
# ════════════════════════════════════════════════════════════
@login_required
def repair_detail(request, pk):
    job = get_object_or_404(
        RepairJob.objects
        .select_related('customer', 'invoice', 'estimate_approved_by'),
        pk=pk,
    )
    parts = job.parts.select_related('product').all()
    parts_total = parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')

    # Enhanced context
    days_in_shop, aging_level = _compute_aging(job)
    warranty_days, warranty_expires = _compute_warranty(job)
    status_pipeline = _build_status_pipeline(job)
    activity_timeline = _build_activity_timeline(job)

    # Estimate vs actual comparison
    estimate_diff = None
    if job.estimated_cost and job.final_amount:
        estimate_diff = job.final_amount - job.estimated_cost

    from ..utils.tracking import generate_tracking_token
    track_token = generate_tracking_token(job)

    context = {
        'job': job,
        'parts': parts,
        'parts_total': parts_total,
        'part_form': RepairPartForm(),
        'status_choices': RepairJob.STATUS_CHOICES,

        # Enhanced
        'days_in_shop': days_in_shop,
        'aging_level': aging_level,
        'warranty_days': warranty_days,
        'warranty_expires': warranty_expires,
        'status_pipeline': status_pipeline,
        'activity_timeline': activity_timeline,
        'estimate_diff': estimate_diff,

        'track_token': track_token,
    }
    return render(request, 'repairs/repair_detail.html', context)


# ════════════════════════════════════════════════════════════
# STATUS CHANGE — Context-aware modal
# ════════════════════════════════════════════════════════════

# Configuration for each new-status modal
STATUS_MODAL_CONFIG = {
    'received': {
        'title': 'Mark as Received',
        'icon': 'box-arrow-in-down',
        'color': 'primary',
        'description': 'Device has physically arrived at the shop. Fill in reception details.',
    },
    'diagnosis': {
        'title': 'Start Diagnosis',
        'icon': 'search',
        'color': 'info',
        'description': 'What did you find after inspecting the device?',
    },
    'repairing': {
        'title': 'Start Repairing',
        'icon': 'arrow-repeat',
        'color': 'warning',
        'description': 'Confirm repair work is starting.',
    },
    'ready': {
        'title': 'Mark Ready for Delivery',
        'icon': 'check2-circle',
        'color': 'primary',
        'description': 'Repair complete. Device ready for customer pickup.',
    },
    'delivered': {
        'title': 'Mark as Delivered',
        'icon': 'truck',
        'color': 'success',
        'description': 'Device is being handed over to the customer. Fill in delivery details.',
    },
    'cancelled': {
        'title': 'Cancel Repair',
        'icon': 'x-circle',
        'color': 'danger',
        'description': 'This will remove all parts and reverse stock. Cannot be undone.',
    },
}

def _modal_error_response(level, message, status=200):
    """
    Return a toast WITHOUT triggering any HTMX swap.
    Used when a modal-target form returns a validation error
    (otherwise the empty response would open an empty modal).
    """
    response = HttpResponse(status=status)
    response['HX-Reswap'] = 'none'   # ← key: don't swap anything
    response['HX-Trigger'] = json.dumps({
        'showToast': {'level': level, 'message': message}
    })
    return response


@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list')
def update_repair_status(request, pk):
    """
    Two modes (single URL):

    Mode A — Dropdown submit (POST has `status`):
        → Return context-aware modal for that status.

    Mode B — Modal submit (POST has `new_status`):
        → Apply status + extra fields atomically.
    """
    job = get_object_or_404(RepairJob, pk=pk)

    if request.method != 'POST':
        return redirect_to_staff('repair_detail', pk=pk)

    # ════════════════════════════════════════════════════
    # MODE A — Dropdown → Show modal
    # ════════════════════════════════════════════════════
    if 'new_status' not in request.POST:
        new_status = (request.POST.get('status') or '').strip()

        # Validation
        if not new_status or new_status not in dict(RepairJob.STATUS_CHOICES):
            return _modal_error_response('danger', 'Invalid status.', status=400)

        if new_status == job.status:
            return _modal_error_response(
                'warning',
                f'Status is already "{job.get_status_display()}".',
            )

        if job.status in ('delivered', 'cancelled'):
            return _modal_error_response(
                'error',
                f'Cannot change from {job.get_status_display()}.',
                status=400,
            )

        config = STATUS_MODAL_CONFIG.get(new_status, {
            'title': 'Update Status',
            'icon': 'arrow-repeat',
            'color': 'primary',
            'description': '',
        })

        return render(request, 'repairs/partials/status_change_modal.html', {
            'job': job,
            'new_status': new_status,
            'config': config,
        })

    # ════════════════════════════════════════════════════
    # MODE B — Modal submit → Apply
    # ════════════════════════════════════════════════════
    new_status = (request.POST.get('new_status') or '').strip()
    old_status = job.status

    if not new_status or new_status not in dict(RepairJob.STATUS_CHOICES):
        return toast_only_response(
            {'level': 'danger', 'message': 'Invalid status.'}, status=400,
        )

    if old_status in ('delivered', 'cancelled'):
        return toast_only_response(
            {'level': 'error',
             'message': f'Cannot change from {job.get_status_display()}.'},
            status=400,
        )

    # ── Collect optional fields from POST ─────────────
    DATE_FIELDS = {'received_at', 'ready_at', 'delivery_date'}
    TEXT_FIELDS = {
        'received_by', 'received_remarks', 'diagnosis_report',
        'delivered_by', 'delivered_to_name', 'delivered_to_phone',
        'delivered_to_designation', 'delivery_remarks',
    }

    extra = {}
    from datetime import datetime as dt

    for field in DATE_FIELDS | TEXT_FIELDS:
        if field not in request.POST:
            continue
        raw = (request.POST.get(field) or '').strip()
        if not raw:
            continue
        if field in DATE_FIELDS:
            try:
                extra[field] = dt.strptime(raw, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                pass
        else:
            extra[field] = raw

    # ── Apply ─────────────────────────────────────────
    with transaction.atomic():
        # On cancellation: delete parts (reverses stock via RepairPart.delete)
        if new_status == 'cancelled' and old_status != 'cancelled':
            for part in job.parts.all():
                part.delete()

        job.status = new_status
        for field, value in extra.items():
            setattr(job, field, value)
        update_fields = ['status'] + list(extra.keys())
        job.save(update_fields=update_fields)
        
    logger.info(
        "Status changed | job=%s | %s → %s | by=%s",
        job.job_number, old_status, new_status, request.user.username,
    )

    # ── Response ──────────────────────────────────────
    if is_htmx(request):
        response = HttpResponse()
        response['HX-Redirect'] = reverse(
            'accounting:repair_detail', args=[job.pk],
        )
        response['HX-Trigger'] = json.dumps({
            'closeModal': '',
            'showToast': {
                'level': 'success',
                'message': (
                    f'Status updated to {job.get_status_display()}.'
                ),
            },
        })
        return response

    messages.success(
        request,
        f"Status updated to {job.get_status_display()}.",
    )
    return redirect_to_staff('repair_detail', pk=job.pk)

# ════════════════════════════════════════════════════════════
# 8. ADD REPAIR PART
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list',
               htmx_template='repairs/partials/part_form_modal.html')
def add_repair_part(request, pk):
    job = get_object_or_404(RepairJob, pk=pk)

    if request.method == 'POST':
        form = RepairPartForm(request.POST)
        if form.is_valid():
            product = form.cleaned_data['product']
            quantity = form.cleaned_data['quantity']

            if not product.is_service and product.current_stock < quantity:
                error_msg = (
                    f"Insufficient stock for {product.name}. "
                    f"Available: {product.current_stock}"
                )
                if is_htmx(request):
                    form.add_error('quantity', error_msg)
                    return render(request, 'repairs/partials/part_form_modal.html',
                                  {'form': form, 'job': job})
                messages.error(request, error_msg)
                return redirect_to_staff('repair_detail', pk=job.pk)

            with transaction.atomic():
                part = form.save(commit=False)
                part.repair_job = job
                part.save()

                parts_total = job.parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
                job.final_amount = parts_total + job.labour_charge
                job.save(update_fields=['final_amount'])

                send_notification_to_contact(
                    job.customer,
                    title=f"Part Added to Repair: {job.job_number}",
                    message=f"A new part '{product.name}' has been added to your repair.",
                    link=reverse('customer:customer_repair_detail', args=[job.pk]),
                    notif_type='info',
                    category='repairs',
                    send_email=False,
                )

                if is_htmx(request):
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:repair_detail', args=[job.pk])
                    response['HX-Trigger'] = json.dumps({
                        'closeModal': '',
                        'showToast': {
                            'level': 'success',
                            'message': f'Part "{product.name}" added. Stock updated.',
                        },
                    })
                    return response

                messages.success(request, f"Part '{product.name}' added successfully.")
                return redirect_to_staff('repair_detail', pk=job.pk)
        else:
            if is_htmx(request):
                return render(request, 'repairs/partials/part_form_modal.html',
                              {'form': form, 'job': job})
    else:
        form = RepairPartForm()

    return render(request, 'repairs/partials/part_form_modal.html',
                  {'form': form, 'job': job})


# ════════════════════════════════════════════════════════════
# 9. REMOVE REPAIR PART
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list')
def remove_repair_part(request, part_pk):
    part = get_object_or_404(
        RepairPart.objects.select_related('repair_job', 'product'),
        pk=part_pk,
    )
    job = part.repair_job
    product = part.product

    with transaction.atomic():
        part.delete()

        parts = job.parts.all()
        parts_total = parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
        job.final_amount = parts_total + job.labour_charge
        job.save(update_fields=['final_amount'])

        send_notification_to_contact(
            job.customer,
            title=f"Part Removed from Repair: {job.job_number}",
            message=f"The part '{product.name}' has been removed from your repair.",
            link=reverse('customer:customer_repair_detail', args=[job.pk]),
            notif_type='info',
            category='repairs',
            send_email=False,
        )

        if is_htmx(request):
            response = render(request, 'repairs/partials/parts_with_totals.html', {
                'parts': parts,
                'job': job,
                'parts_total': parts_total,
            })
            response['HX-Trigger'] = json.dumps({
                'showToast': {
                    'level': 'success',
                    'message': f'Part "{product.name}" removed successfully.',
                },
            })
            return response

        messages.success(request, f'Part "{product.name}" removed successfully.')
        return redirect_to_staff('repair_detail', pk=job.pk)


# ════════════════════════════════════════════════════════════
# 10. CREATE INVOICE FROM REPAIR
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list')
def create_invoice_from_repair(request, pk):
    job = get_object_or_404(RepairJob.objects.select_related('customer'), pk=pk)

    if request.method == 'GET':
        return render(request, 'repairs/partials/create_invoice_modal.html', {'job': job})

    invoice_date = timezone.now().date()
    if request.POST.get('invoice_date'):
        try:
            invoice_date = datetime.strptime(
                request.POST.get('invoice_date'), '%Y-%m-%d'
            ).date()
        except (ValueError, TypeError):
            pass

    if job.invoice:
        messages.info(request, "Invoice already exists for this repair.")
        return redirect_to_staff('invoice_detail', pk=job.invoice.pk)

    if job.status not in ('ready', 'delivered'):
        messages.error(request, "Repair must be 'Ready' or 'Delivered' to generate invoice.")
        return redirect_to_staff('repair_detail', pk=pk)

    if job.estimated_cost and job.estimated_cost > 0 and job.estimate_status != 'approved':
        messages.error(request, "Estimate must be approved before invoicing.")
        return redirect_to_staff('repair_detail', pk=pk)

    with transaction.atomic():
        company = CompanyProfile.get_instance()
        if not job.customer.gstin:
            gst_type = 'non_gst'
        elif company.state and job.customer.state and company.state != job.customer.state:
            gst_type = 'interstate'
        else:
            gst_type = 'intrastate'

        notes = (
            f"Repair job: {job.job_number}\n"
            f"Device: {job.device_model} (SN: {job.serial_number or 'N/A'})\n"
            f"Issue: {job.issue_description}\n"
            f"Action: {job.action_taken or 'Not specified'}\n"
            f"Notes: {job.notes or ''}"
        )

        invoice = Invoice(
            customer=job.customer,
            date=invoice_date,
            gst_type=gst_type,
            notes=notes,
        )
        invoice.save()

        for part in job.parts.select_related('product').all():
            InvoiceItem.objects.create(
                invoice=invoice,
                product=part.product,
                quantity=part.quantity,
                unit_price=part.unit_price,
                tax_rate=part.product.tax_rate,
                description=f"Repair part: {part.product.name}",
            )

        if job.labour_charge > 0:
            tax_rate = company.default_tax_rate or Decimal('18')
            labour_product, _ = Product.objects.get_or_create(
                name="Repair Labour",
                defaults={
                    'is_service': True,
                    'selling_price': job.labour_charge,
                    'tax_rate': tax_rate,
                    'hsn_code': '998446',
                },
            )
            InvoiceItem.objects.create(
                invoice=invoice,
                product=labour_product,
                quantity=Decimal('1'),
                unit_price=job.labour_charge,
                tax_rate=tax_rate,
                description="Labour Charge",
            )

        invoice.calculate_totals()
        invoice.save()
        sync_invoice_ledger(invoice)

        job.invoice = invoice
        if job.status == 'ready':
            job.status = 'delivered'
        job.save(update_fields=['invoice', 'status'])

        send_notification_to_contact(
            job.customer,
            title=f"Invoice Generated: {invoice.invoice_number}",
            message=f"Invoice for repair job {job.job_number} is ready.",
            link=reverse('customer:customer_invoice_detail', args=[invoice.pk]),
            notif_type='success',
            category='sales',
            send_email=False,
        )

        messages.success(
            request,
            f"Invoice {invoice.invoice_number} created for {invoice_date}.",
        )

        if is_htmx(request):
            response = HttpResponse()
            response['HX-Redirect'] = reverse('accounting:invoice_detail', args=[invoice.pk])
            response['HX-Trigger'] = json.dumps({
                'showToast': {
                    'level': 'success',
                    'message': f'Invoice {invoice.invoice_number} created.',
                },
                'closeModal': '',
            })
            return response

        return redirect_to_staff('invoice_detail', pk=invoice.pk)


# ════════════════════════════════════════════════════════════
# 11. DELETE REPAIR
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:repair_list')
def repair_delete(request, pk):
    job = get_object_or_404(RepairJob, pk=pk)

    if job.invoice:
        return toast_only_response(
            {'level': 'danger', 'message': 'Cannot delete job with linked invoice.'},
            status=400,
        )

    with transaction.atomic():
        for part in job.parts.all():
            part.delete()
        job.delete()

        if is_htmx(request):
            return htmx_response(
                request,
                'repairs/partials/repair_table.html',
                context=get_paginated_repairs_context(request),
                toast={'level': 'success', 'message': 'Repair job deleted successfully.'},
            )

        messages.success(request, "Repair job deleted successfully.")
        return redirect_to_staff('repair_list')


# ════════════════════════════════════════════════════════════
# 12. PRINT REPAIR
# ════════════════════════════════════════════════════════════
@login_required
def repair_print(request, pk):
    job = get_object_or_404(
        RepairJob.objects.select_related('customer', 'invoice'),
        pk=pk,
    )
    parts = job.parts.select_related('product').all()
    company = CompanyProfile.get_instance()
    logo_exists = bool(company.logo and company.logo.name)

    from ..utils.tracking import generate_tracking_token
    track_token = generate_tracking_token(job)

    return render(request, 'repairs/repair_print.html', {
        'job': job,
        'parts': parts,
        'company': company,
        'logo_exists': logo_exists,
        'invoice': job.invoice if job.invoice else None,
        'track_token': track_token,
    })


# ════════════════════════════════════════════════════════════
# 13. REPAIR CREATE FOR SPECIFIC CONTACT
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list',
               htmx_template='contacts/repair_form_from_contact.html')
def repair_create_for_contact(request, contact_id):
    contact = get_object_or_404(Contact, pk=contact_id)

    if request.method == 'POST':
        form = RepairJobForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                job = form.save(commit=False)
                job.customer = contact
                now = timezone.now()
                if not job.received_at:
                    job.received_at = now.date()
                if not job.date_in:
                    job.date_in = now.date()
                if job.status == 'pending':
                    job.status = 'received'
                job.save()
                form.save_m2m()

                send_notification_to_contact(
                    job.customer,
                    title=f"Repair Job Created: {job.job_number}",
                    message=f"Your repair for {job.device_model} has been received.",
                    link=reverse('customer:customer_repair_detail', args=[job.pk]),
                    notif_type='success',
                    category='repairs',
                    send_email=False,
                )

                if is_htmx(request):
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:repair_detail', args=[job.pk])
                    return response

                messages.success(request, f"Repair job {job.job_number} created.")
                return redirect_to_staff('repair_list')
        else:
            if is_htmx(request):
                return render(request, 'contacts/repair_form_from_contact.html',
                              {'form': form, 'contact': contact})
    else:
        form = RepairJobForm(initial={'customer': contact})

    return render(request, 'contacts/repair_form_from_contact.html',
                  {'form': form, 'contact': contact})


# ════════════════════════════════════════════════════════════
# 14. STAFF APPROVE ESTIMATE
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='accounting:repair_list')
def staff_approve_estimate(request, pk):
    repair = get_object_or_404(RepairJob, pk=pk)

    if repair.estimate_status != 'pending':
        messages.warning(request, f"This estimate is already {repair.get_estimate_status_display()}.")
        return redirect('accounting:repair_detail', pk=repair.pk)

    if request.method == 'POST':
        source = request.POST.get('source', 'staff_phone')
        remarks = request.POST.get('remarks', '').strip()

        with transaction.atomic():
            repair.estimate_status = 'approved'
            repair.estimate_approved_at = timezone.now()
            repair.estimate_approved_by = request.user
            repair.approval_source = source
            repair.approval_remarks = (
                remarks if remarks else
                f"Approved by {request.user.get_full_name() or request.user.username}"
            )

            if repair.status in ['pending', 'diagnosis']:
                repair.status = 'repairing'

            repair.save()

            send_notification_to_contact(
                repair.customer,
                title=f"Your repair {repair.job_number} has been approved",
                message=f"Your repair for {repair.device_model} has been approved and will start shortly.",
                link=reverse('customer:customer_repair_detail', args=[repair.pk]),
                notif_type='success',
                category='repairs',
                send_email=False,
            )

            messages.success(request, f"Estimate for {repair.job_number} approved!")

            if is_htmx(request):
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:repair_detail', args=[repair.pk])
                response['HX-Trigger'] = json.dumps({'closeModal': ''})
                return response

            return redirect('accounting:repair_detail', pk=repair.pk)

    return render(request, 'repairs/partials/staff_approve_modal.html', {'repair': repair})


# ════════════════════════════════════════════════════════════
# 15. EXPORT REPAIRS TO EXCEL
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='accounting:repair_list')
def export_repairs_excel(request):
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        messages.error(request, "Openpyxl library is not installed.")
        return redirect('accounting:repair_list')

    queryset = (
        RepairJob.objects
        .select_related('customer', 'invoice')
        .order_by('-created_at')
        .prefetch_related('parts')
    )

    search = request.GET.get('search', '').strip()
    status_filter = request.GET.get('status', '')
    customer_id = request.GET.get('customer', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    if search:
        queryset = queryset.filter(
            Q(job_number__icontains=search) |
            Q(customer__name__icontains=search) |
            Q(device_model__icontains=search) |
            Q(serial_number__icontains=search)
        )
    if status_filter:
        queryset = queryset.filter(status=status_filter)
    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)
    if date_from:
        queryset = queryset.filter(date_in__gte=date_from)
    if date_to:
        queryset = queryset.filter(date_in__lte=date_to)

    company = CompanyProfile.get_instance()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Repair Jobs"

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
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center', wrap_text=True)
    right = Alignment(horizontal='right', vertical='center')

    TOTAL_COLS = 21
    last_col = get_column_letter(TOTAL_COLS)

    ws.merge_cells(f'A1:{last_col}1')
    ws['A1'] = company.name or "A1 Computer Solutions"
    ws['A1'].font = title_font
    ws['A1'].fill = title_fill
    ws['A1'].alignment = center
    ws.row_dimensions[1].height = 30

    ws.merge_cells(f'A2:{last_col}2')
    parts_addr = []
    if company.address: parts_addr.append(company.address)
    if company.phone: parts_addr.append(f"Phone: {company.phone}")
    if company.email: parts_addr.append(f"Email: {company.email}")
    if company.gstin: parts_addr.append(f"GSTIN: {company.gstin}")
    ws['A2'] = " | ".join(parts_addr)
    ws['A2'].font = Font(size=10, italic=True)
    ws['A2'].alignment = center

    ws.merge_cells(f'A3:{last_col}3')
    ws['A3'] = "REPAIR JOBS REPORT"
    ws['A3'].font = subtitle_font
    ws['A3'].fill = subtitle_fill
    ws['A3'].alignment = center
    ws.row_dimensions[3].height = 25

    ws.merge_cells(f'A4:{last_col}4')
    filter_parts = [f"Generated: {timezone.now().strftime('%d-%m-%Y %H:%M')}"]
    if search: filter_parts.append(f"Search: {search}")
    if status_filter: filter_parts.append(f"Status: {status_filter}")
    if date_from: filter_parts.append(f"From: {date_from}")
    if date_to: filter_parts.append(f"To: {date_to}")
    filter_parts.append(f"Total: {queryset.count()}")
    ws['A4'] = " | ".join(filter_parts)
    ws['A4'].font = Font(size=10, italic=True, color="555555")
    ws['A4'].alignment = center

    ws.row_dimensions[5].height = 5

    headers = [
        ('Job #', 14), ('Date In', 12), ('Customer', 22), ('Phone', 14),
        ('Device Model', 20), ('Serial #', 16), ('Issue', 28),
        ('Diagnosis', 28), ('Action Taken', 28), ('Status', 12),
        ('Estimate Status', 14), ('Estimated Cost', 14),
        ('Received By', 14), ('Delivered By', 14), ('Delivered To', 16),
        ('Recipient Phone', 14), ('Delivery Date', 12),
        ('Labour (Rs.)', 12), ('Parts (Rs.)', 12),
        ('Final Amount (Rs.)', 15), ('Invoice #', 14),
    ]
    for col, (h, w) in enumerate(headers, 1):
        c = ws.cell(row=6, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center
        c.border = border_header
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.row_dimensions[6].height = 30

    row_num = 7
    total_labour = Decimal('0')
    total_parts = Decimal('0')
    total_final = Decimal('0')

    for job in queryset:
        parts_total = sum(p.line_total for p in job.parts.all())

        row_data = [
            job.job_number,
            job.date_in.strftime('%d-%m-%Y') if job.date_in else '',
            job.customer.name if job.customer else '',
            job.customer.phone if job.customer and job.customer.phone else '',
            job.device_model or '',
            job.serial_number or '',
            job.issue_description or '',
            job.diagnosis_report or '',
            job.action_taken or '',
            job.get_status_display(),
            job.get_estimate_status_display() if job.estimate_status else 'No Estimate',
            float(job.estimated_cost) if job.estimated_cost else 0,
            job.received_by or '',
            job.delivered_by or '',
            job.delivered_to_name or '',
            job.delivered_to_phone or '',
            job.delivery_date.strftime('%d-%m-%Y') if job.delivery_date else '',
            float(job.labour_charge or 0),
            float(parts_total),
            float(job.final_amount or 0),
            job.invoice.invoice_number if job.invoice else '',
        ]

        for col, val in enumerate(row_data, 1):
            c = ws.cell(row=row_num, column=col, value=val)
            c.border = border_all
            c.alignment = left

        for col in [12, 18, 19, 20]:
            ws.cell(row=row_num, column=col).alignment = right
            ws.cell(row=row_num, column=col).number_format = money_format
        for col in [1, 2, 10, 11, 17, 21]:
            ws.cell(row=row_num, column=col).alignment = center

        total_labour += Decimal(str(job.labour_charge or 0))
        total_parts += parts_total
        total_final += Decimal(str(job.final_amount or 0))

        if row_num % 2 == 0:
            alt = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
            for c in range(1, TOTAL_COLS + 1):
                ws.cell(row=row_num, column=c).fill = alt
        row_num += 1

    ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=17)
    tl = ws.cell(row=row_num, column=1, value="GRAND TOTAL")
    tl.font = total_font
    tl.fill = total_fill
    tl.alignment = right
    for col in range(2, 18):
        ws.cell(row=row_num, column=col).fill = total_fill
        ws.cell(row=row_num, column=col).border = border_header
    for col, val in [(18, total_labour), (19, total_parts), (20, total_final)]:
        c = ws.cell(row=row_num, column=col, value=float(val))
        c.font = total_font
        c.fill = total_fill
        c.alignment = right
        c.number_format = money_format
        c.border = border_header
    et = ws.cell(row=row_num, column=21, value="")
    et.fill = total_fill
    et.border = border_header

    ws.freeze_panes = 'A7'
    ws.auto_filter.ref = f"A6:{last_col}6"
    ws.page_setup.orientation = 'landscape'
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = (
        f'attachment; filename="repairs_{timezone.now().strftime("%Y%m%d_%H%M%S")}.xlsx"'
    )
    wb.save(response)
    return response


# ════════════════════════════════════════════════════════════
# 16. SEND ESTIMATE TO CUSTOMER
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list')
def send_estimate_to_customer(request, pk):
    """
    Send repair estimate to the customer via:
      - In-app notification (always)
      - Email (optional)
      - WhatsApp link (handled client-side)

    GET  -> render the send modal
    POST -> dispatch notification(s), return toast + close modal
    """
    job = get_object_or_404(
        RepairJob.objects.select_related('customer'),
        pk=pk,
    )

    if job.status == 'cancelled':
        return toast_only_response(
            {'level': 'danger', 'message': 'Cannot send estimate for a cancelled repair.'},
            status=400,
        )

    if not job.estimated_cost or job.estimated_cost <= 0:
        return toast_only_response(
            {'level': 'danger',
             'message': 'Set an estimated cost first, then send the estimate.'},
            status=400,
        )

    # ── GET: show modal ────────────────────────────────
    if request.method == 'GET':
        return render(request, 'repairs/partials/send_estimate_modal.html', {
            'job': job,
        })

    # ── POST: dispatch ─────────────────────────────────
    method = (request.POST.get('method') or 'email').strip()
    custom_message = (request.POST.get('message') or '').strip()

    # Build the base message
    if not custom_message:
        custom_message = (
            f"Repair estimate for your {job.device_model} (Job: {job.job_number}) "
            f"is ₹{job.estimated_cost:.2f}. Please review and approve/reject "
            f"from your customer portal."
        )

    sent_channels = []

    # 1. In-app notification (always)
    try:
        send_notification_to_contact(
            job.customer,
            title=f"Repair Estimate Ready: {job.job_number}",
            message=custom_message,
            link=reverse('customer:customer_repair_detail', args=[job.pk]),
            notif_type='warning',
            category='repairs',
            # send_email=(method in ('email', 'both')),
            send_email=False,
        )
        sent_channels.append('Notification')
        if method in ('email', 'both'):
            sent_channels.append('Email')
    except Exception:
        logger.exception("Failed to send estimate notification for %s", job.job_number)
        return toast_only_response(
            {'level': 'danger', 'message': 'Failed to send estimate. Please try again.'},
            status=500,
        )

    # 2. If WhatsApp-only, we still want the client to know
    whatsapp_url = None
    if method in ('whatsapp', 'both') and job.customer.phone:
        whatsapp_url = (
            f"https://wa.me/91{job.customer.phone}"
            f"?text={urllib.parse.quote(custom_message + ' — View: ') }"
            f"{request.build_absolute_uri(reverse('customer:customer_repair_detail', args=[job.pk]))}"
        )
        sent_channels.append('WhatsApp')

    logger.info(
        "Estimate sent | job=%s | customer=%s | via=%s",
        job.job_number, job.customer.name, ', '.join(sent_channels),
    )

    response = HttpResponse()
    response['HX-Trigger'] = json.dumps({
        'closeModal': '',
        'showToast': {
            'level': 'success',
            'message': f'Estimate sent via {", ".join(sent_channels)}.',
            'title': 'Estimate Sent',
        },
        # If WhatsApp, trigger client-side open
        **({'openWhatsApp': whatsapp_url} if whatsapp_url else {}),
    })
    return response


# ════════════════════════════════════════════════════════════
# 17. PRINT ESTIMATE (hand-over to customer)
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='accounting:repair_list')
def estimate_print(request, pk):
    """
    Printable estimate slip for the customer.
    Shows: device, issue, estimated cost breakdown, terms.
    """
    job = get_object_or_404(
        RepairJob.objects.select_related('customer'),
        pk=pk,
    )
    parts = job.parts.select_related('product').all()
    parts_total = parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')

    company = CompanyProfile.get_instance()
    logo_exists = bool(company.logo and company.logo.name)

    # Estimate = parts estimate + labour estimate
    # If parts exist, calculate; else just show estimated_cost
    estimated_parts = parts_total
    estimated_labour = job.labour_charge or Decimal('0')
    if estimated_parts == 0 and estimated_labour == 0:
        estimated_labour = job.estimated_cost or Decimal('0')

    total_estimate = (estimated_parts + estimated_labour).quantize(Decimal('0.01'))
    if total_estimate == 0:
        total_estimate = job.estimated_cost or Decimal('0')

    return render(request, 'repairs/estimate_print.html', {
        'job': job,
        'parts': parts,
        'parts_total': parts_total,
        'estimated_parts': estimated_parts,
        'estimated_labour': estimated_labour,
        'total_estimate': total_estimate,
        'company': company,
        'logo_exists': logo_exists,
    })


# ════════════════════════════════════════════════════════════
# 18. WARRANTY CARD PRINT (after delivery)
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='accounting:repair_list')
def warranty_card_print(request, pk):
    """
    Printable warranty card — handed over after delivery.
    Only available when repair is delivered.
    """
    job = get_object_or_404(
        RepairJob.objects.select_related('customer', 'invoice'),
        pk=pk,
    )

    if job.status != 'delivered':
        messages.warning(request, "Warranty card is only available after delivery.")
        return redirect_to_staff('repair_detail', pk=job.pk)

    company = CompanyProfile.get_instance()
    logo_exists = bool(company.logo and company.logo.name)

    # Warranty expiry = delivery_date + 30 days (default)
    from datetime import timedelta
    warranty_days = 30
    expires_on = None
    days_left = None
    if job.delivery_date:
        expires_on = job.delivery_date + timedelta(days=warranty_days)
        days_left = (expires_on - timezone.now().date()).days

    return render(request, 'repairs/warranty_card_print.html', {
        'job': job,
        'company': company,
        'logo_exists': logo_exists,
        'warranty_days': warranty_days,
        'expires_on': expires_on,
        'days_left': days_left,
    })
    
# ════════════════════════════════════════════════════════════
# 19. QUICK UPDATE — inline edit from detail page
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list')
def quick_update_repair(request, pk):
    """
    Quick inline update of repair fields directly from detail page.
    Only updates the fields submitted — does NOT touch anything else.

    GET  → render modal with pre-filled form
    POST → save partial fields, redirect back to detail
    """
    job = get_object_or_404(RepairJob, pk=pk)

    if job.status in ('delivered', 'cancelled'):
        return toast_only_response(
            {'level': 'danger',
             'message': f'Cannot edit a {job.get_status_display()} repair.'},
            status=400,
        )

    # ── GET: show modal ────────────────────────────────
    if request.method == 'GET':
        return render(request, 'repairs/partials/quick_update_modal.html', {
            'job': job,
        })

    # ── POST: update only provided fields ──────────────
    update_fields = []

    # Estimated cost
    est = request.POST.get('estimated_cost', '').strip()
    if est:
        try:
            job.estimated_cost = Decimal(est)
            update_fields.append('estimated_cost')
        except (ValueError, TypeError):
            pass
    elif est == '' and 'estimated_cost' in request.POST:
        # Explicitly cleared
        job.estimated_cost = None
        update_fields.append('estimated_cost')

    # Labour charge
    labour = request.POST.get('labour_charge', '').strip()
    if labour:
        try:
            job.labour_charge = Decimal(labour)
            update_fields.append('labour_charge')
        except (ValueError, TypeError):
            pass

    # Text fields
    for field in ['diagnosis_report', 'action_taken', 'received_by', 'received_remarks']:
        if field in request.POST:
            setattr(job, field, request.POST.get(field, '').strip())
            update_fields.append(field)

    # Ready date (only allow if status is repairing/diagnosis)
    ready_date = request.POST.get('ready_at', '').strip()
    if ready_date and job.status in ('diagnosis', 'repairing', 'received'):
        try:
            from datetime import datetime as dt
            job.ready_at = dt.strptime(ready_date, '%Y-%m-%d').date()
            update_fields.append('ready_at')
        except (ValueError, TypeError):
            pass

    if update_fields:
        # Recalc final amount if labour changed
        if 'labour_charge' in update_fields:
            parts_total = job.parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
            job.final_amount = parts_total + job.labour_charge
            update_fields.append('final_amount')

        job.save(update_fields=update_fields)
        logger.info(
            "Quick update | job=%s | fields=%s",
            job.job_number, ','.join(update_fields),
        )

    # HTMX
    if is_htmx(request):
        response = HttpResponse()
        response['HX-Redirect'] = reverse('accounting:repair_detail', args=[job.pk])
        response['HX-Trigger'] = json.dumps({
            'closeModal': '',
            'showToast': {
                'level': 'success',
                'message': 'Repair updated successfully.',
            },
        })
        return response

    messages.success(request, "Repair updated successfully.")
    return redirect_to_staff('repair_detail', pk=job.pk)