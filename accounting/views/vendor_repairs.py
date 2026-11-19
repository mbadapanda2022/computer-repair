# accounting/views/vendor_repairs.py
"""
Staff Portal — Vendor (third-party) Repair Jobs.

A customer device that cannot be repaired in-house is sent to an external
vendor. The device's internal parts are recorded before dispatch and checked
again on return, so missing/swapped/damaged parts are caught immediately.
"""

import json
import logging
from datetime import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from ..decorators import handle_errors
from ..forms import (
    VendorRepairCancelForm,
    VendorRepairCompleteForm,
    VendorRepairJobForm,
    VendorRepairPartForm,
    VendorRepairPartVerifyForm,
    VendorRepairReturnForm,
)
from ..models import (
    CompanyProfile, Contact, VendorRepairJob, VendorRepairPart,
)
from .utils import (
    htmx_response, is_htmx, redirect_to_staff, toast_only_response,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 20
PART_EDITABLE_STATUSES = ('sent', 'in_repair', 'returned')
PART_VERIFIABLE_STATUSES = ('returned',)


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════
def _parse_date(raw):
    """Return a date for an ISO string, or None (never raises)."""
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return None


def _base_queryset():
    return (
        VendorRepairJob.objects
        .select_related('customer', 'vendor', 'created_by', 'verified_by')
        .annotate(
            num_parts=Count(
                'parts',
                filter=Q(parts__is_deleted=False),
                distinct=True,
            ),
            num_pending=Count(
                'parts',
                filter=Q(
                    parts__is_deleted=False,
                    parts__verification_status='pending',
                ),
                distinct=True,
            ),
            num_discrepancies=Count(
                'parts',
                filter=Q(
                    parts__is_deleted=False,
                    parts__verification_status__in=(
                        VendorRepairJob.PARTS_DISCREPANCY_STATUSES
                    ),
                ),
                distinct=True,
            ),
        )
    )


def _filtered_queryset(request):
    qs = _base_queryset()

    search = request.GET.get('search', '').strip()
    status = request.GET.get('status', '')
    purpose = request.GET.get('purpose', '')
    vendor_id = request.GET.get('vendor', '')
    date_from = _parse_date(request.GET.get('date_from'))
    date_to = _parse_date(request.GET.get('date_to'))

    if search:
        qs = qs.filter(
            Q(job_number__icontains=search)
            | Q(vendor_reference__icontains=search)
            | Q(customer__name__icontains=search)
            | Q(vendor__name__icontains=search)
            | Q(device_model__icontains=search)
            | Q(serial_number__icontains=search)
        )
    if status == 'open':
        qs = qs.filter(status__in=VendorRepairJob.OPEN_STATUSES)
    elif status:
        qs = qs.filter(status=status)
    if purpose:
        qs = qs.filter(purpose=purpose)
    if vendor_id:
        qs = qs.filter(vendor_id=vendor_id)
    if date_from:
        qs = qs.filter(outbound_date__gte=date_from)
    if date_to:
        qs = qs.filter(outbound_date__lte=date_to)
    return qs


def _list_context(request):
    qs = _filtered_queryset(request)

    stats_agg = VendorRepairJob.objects.aggregate(
        total_count=Count('id'),
        open_count=Count(
            'id', filter=Q(status__in=VendorRepairJob.OPEN_STATUSES),
        ),
        with_vendor_count=Count(
            'id', filter=Q(status__in=('sent', 'in_repair')),
        ),
        returned_count=Count('id', filter=Q(status='returned')),
        completed_count=Count('id', filter=Q(status='completed')),
        warranty_count=Count('id', filter=Q(purpose='warranty')),
        total_final=Sum('final_amount', filter=~Q(status='cancelled')),
    )
    stats_agg['discrepancy_count'] = (
        VendorRepairJob.objects
        .filter(
            parts__is_deleted=False,
            parts__verification_status__in=(
                VendorRepairJob.PARTS_DISCREPANCY_STATUSES
            ),
        )
        .distinct()
        .count()
    )
    stats = {k: (v or 0) for k, v in stats_agg.items()}

    page_number = request.GET.get('page', 1)
    paginator = Paginator(qs.order_by('-outbound_date', '-id'), PAGE_SIZE)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages or 1)

    # Filtered total computed on a clean queryset to avoid GROUP BY from
    # the count annotations multiplying the money sum.
    filtered_total = (
        VendorRepairJob.objects
        .filter(pk__in=qs.values('pk'))
        .exclude(status='cancelled')
        .aggregate(total=Sum('final_amount'))['total']
        or Decimal('0')
    )

    vendors = (
        Contact.objects
        .filter(
            contact_type__in=['vendor', 'both'],
            vendor_repair_jobs_sent__isnull=False,
        )
        .distinct()
        .order_by('name')
    )

    return {
        'jobs': page_obj,
        'page_obj': page_obj,
        'stats': stats,
        'filtered_total': filtered_total,
        'vendors': vendors,
        'status_choices': VendorRepairJob.STATUS_CHOICES,
        'purpose_choices': VendorRepairJob.PURPOSE_CHOICES,
        'search': request.GET.get('search', '').strip(),
        'status_filter': request.GET.get('status', ''),
        'purpose_filter': request.GET.get('purpose', ''),
        'vendor_id': request.GET.get('vendor', ''),
        'date_from': request.GET.get('date_from', ''),
        'date_to': request.GET.get('date_to', ''),
    }


def _parts_context(job):
    parts = list(job.parts.all())
    return {
        'job': job,
        'parts': parts,
        'can_edit_parts': job.status in PART_EDITABLE_STATUSES,
        'can_verify_parts': job.status in PART_VERIFIABLE_STATUSES,
    }


def _parts_section_response(request, job, toast):
    """Swap the parts table in place, close the modal and show a toast."""
    # Re-fetch with the parts prefetched so the template's count helpers
    # (pending / discrepancy / line count) do not fire one query each.
    job = VendorRepairJob.objects.prefetch_related('parts').get(pk=job.pk)
    return htmx_response(
        request,
        'vendor_repairs/partials/parts_section.html',
        _parts_context(job),
        toast=toast,
        close_modal=True,
        extra_headers={
            'HX-Retarget': '#vendor-parts-section',
            'HX-Reswap': 'outerHTML',
        },
    )


def _redirect_to_detail(job, toast_message):
    response = HttpResponse()
    response['HX-Redirect'] = reverse(
        'accounting:vendor_repair_detail', args=[job.pk],
    )
    response['HX-Trigger'] = json.dumps({
        'showToast': {'level': 'success', 'message': toast_message},
    })
    return response


# ============================================================
# INLINE PARTS GRID ON THE JOB FORM
# ============================================================
PART_ROW_BLANKS = 3
PART_PRESETS = (
    'HDD', 'SSD', 'RAM', 'WiFi Card', 'Battery', 'Keyboard',
    'Touchpad', 'Webcam', 'CMOS Battery', 'DVD Drive',
)
_PART_POST_FIELDS = (
    'part_id', 'part_name', 'part_identifier',
    'part_quantity', 'part_presence', 'part_condition',
)


def _blank_part_row():
    return {
        'id': '', 'name': '', 'identifier': '', 'quantity': '',
        'presence': 'installed', 'condition': '', 'remove': False,
    }


def _part_rows_from_post(request):
    """Parse the inline parts grid. Returns (rows, errors).

    Checkboxes are parallel hidden inputs (always submitted), so every
    column list keeps the same indexes and rows never shift.
    """
    cols = {name: request.POST.getlist(name) for name in _PART_POST_FIELDS}
    remove_flags = request.POST.getlist('part_remove')
    count = max((len(values) for values in cols.values()), default=0)

    rows, errors = [], []
    for i in range(count):
        raw = {
            key: (values[i] if i < len(values) else '').strip()
            for key, values in cols.items()
        }
        remove = (remove_flags[i] if i < len(remove_flags) else '0').strip() == '1'
        # Quantity is prefilled (1) on blank lines, so only typed text
        # counts as content — otherwise every untouched row would error.
        has_content = any(
            raw[key] for key in
            ('part_name', 'part_identifier', 'part_condition')
        )
        if not has_content and not remove:
            continue

        presence = (
            raw['part_presence']
            if raw['part_presence'] in ('installed', 'not_installed')
            else 'installed'
        )
        quantity = None
        if raw['part_quantity']:
            try:
                quantity = int(raw['part_quantity'])
            except ValueError:
                errors.append(
                    f'Parts row {i + 1}: quantity must be a whole number.')
        elif presence == 'installed':
            quantity = 1

        if not remove:
            if not raw['part_name']:
                errors.append(
                    f'Parts row {i + 1}: enter a part name or clear the row.')
            elif presence == 'installed' and quantity is not None and quantity < 1:
                quantity = 1

        rows.append({
            'id': raw['part_id'],
            'name': raw['part_name'],
            'identifier': raw['part_identifier'],
            'quantity': (
                0 if presence == 'not_installed'
                else (quantity if quantity is not None else '')
            ),
            'presence': presence,
            'condition': raw['part_condition'],
            'remove': remove,
        })
    return rows, errors


def _sync_form_parts(job, rows):
    """Apply the inline grid to the job's parts.

    Only unverified parts are editable or removable; verified history stays
    locked so a past check can never be silently rewritten.
    """
    existing = {str(part.pk): part for part in job.parts.all()}
    for raw in rows:
        part = existing.get(raw['id']) if raw['id'] else None
        if part and part.verification_status != 'pending':
            continue
        if raw['remove']:
            if part:
                part.delete()
            continue
        if not raw['name']:
            continue

        data = {
            'name': raw['name'],
            'identifier': raw['identifier'],
            'quantity': raw['quantity'] if raw['quantity'] is not None else 1,
            'presence': raw['presence'],
            'condition_sent': raw['condition'],
        }
        if part:
            for field, value in data.items():
                setattr(part, field, value)
            part.save()
        else:
            VendorRepairPart.objects.create(job=job, **data)


def _form_parts_context(request, job):
    if request.method == 'POST':
        rows, _ = _part_rows_from_post(request)
        rows = [row for row in rows if not row['remove']]
    else:
        rows = []
        if job:
            for part in job.parts.filter(verification_status='pending'):
                rows.append({
                    'id': str(part.pk),
                    'name': part.name,
                    'identifier': part.identifier,
                    'quantity': part.quantity,
                    'presence': part.presence,
                    'condition': part.condition_sent,
                    'remove': False,
                })
    while len(rows) < PART_ROW_BLANKS:
        rows.append(_blank_part_row())

    verified_parts = (
        list(job.parts.exclude(verification_status='pending')) if job else []
    )
    return {
        'form_parts': rows,
        'verified_parts': verified_parts,
        'part_presets': PART_PRESETS,
    }


# ════════════════════════════════════════════════════════════
# LIST / PRINT / EXPORT
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_list(request):
    if request.GET.get('reset'):
        return redirect('accounting:vendor_repair_list')

    context = _list_context(request)
    if is_htmx(request):
        return render(
            request, 'vendor_repairs/partials/vendor_repair_table.html',
            context,
        )
    return render(request, 'vendor_repairs/vendor_repair_list.html', context)


@login_required
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_list_print(request):
    qs = _filtered_queryset(request).order_by('-outbound_date', '-id')
    jobs = list(qs)
    company = CompanyProfile.get_instance()
    return render(request, 'vendor_repairs/vendor_repair_list_print.html', {
        'jobs': jobs,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
        'filtered_total': sum(
            (j.final_amount or Decimal('0') for j in jobs
             if j.status != 'cancelled'),
            Decimal('0'),
        ),
        'search': request.GET.get('search', '').strip(),
        'status_filter': request.GET.get('status', ''),
        'purpose_filter': request.GET.get('purpose', ''),
        'date_from': request.GET.get('date_from', ''),
        'date_to': request.GET.get('date_to', ''),
    })


@login_required
@handle_errors(default_redirect='accounting:vendor_repair_list')
def export_vendor_repairs_excel(request):
    """Excel export with one sheet of jobs and one sheet of every part."""
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        messages.error(request, 'Openpyxl library is not installed.')
        return redirect('accounting:vendor_repair_list')

    qs = (
        _filtered_queryset(request)
        .order_by('-outbound_date', '-id')
        .prefetch_related('parts')
    )
    jobs = list(qs)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Vendor Repairs'

    title_font = Font(bold=True, size=16, color='FFFFFF')
    title_fill = PatternFill(
        start_color='1F4E78', end_color='1F4E78', fill_type='solid',
    )
    header_font = Font(bold=True, size=10, color='FFFFFF')
    header_fill = PatternFill(
        start_color='305496', end_color='305496', fill_type='solid',
    )
    total_font = Font(bold=True, size=11, color='FFFFFF')
    total_fill = PatternFill(
        start_color='375623', end_color='375623', fill_type='solid',
    )
    thin = Side(style='thin', color='999999')
    border_all = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center', wrap_text=True)
    right = Alignment(horizontal='right', vertical='center')
    money_format = '#,##0.00'

    headers = [
        ('Job #', 14), ('Purpose', 16), ('Status', 20), ('Date Sent', 13),
        ('Expected Return', 15), ('Date Returned', 14),
        ('Vendor', 24), ('Vendor Job/Ref', 18),
        ('Vendor Contact', 20), ('Vendor Phone', 14),
        ('Customer', 22), ('Customer Phone', 14),
        ('Device', 22), ('Serial #', 16),
        ('Issue', 30), ('Accessories', 20), ('Condition Sent', 20),
        ('Warranty Claim #', 18), ('Claim Status', 16),
        ('Purchase Date', 14), ('Warranty Expiry', 15),
        ('Bill / Card Ref', 18),
        ('Dispatched By', 16), ('Courier', 14), ('Tracking #', 16),
        ('Return Condition', 20),
        ('Part Lines', 10), ('Part Qty', 10), ('Pending Checks', 12),
        ('Discrepancies', 12),
        ('Estimate (Rs.)', 14), ('Final (Rs.)', 14), ('Difference (Rs.)', 14),
        ('Vendor Invoice #', 16), ('Vendor Invoice Date', 14),
        ('Parts Verified', 12), ('Verified By', 16), ('Created By', 16),
        ('Created At', 16), ('Notes', 30), ('Verification Notes', 30),
    ]

    ws.merge_cells(
        start_row=1, start_column=1, end_row=1, end_column=len(headers),
    )
    cell = ws.cell(row=1, column=1, value='Vendor Repair Jobs')
    cell.font = title_font
    cell.fill = title_fill
    cell.alignment = center

    for col_idx, (label, width) in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=col_idx, value=label)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border_all
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.row_dimensions[1].height = 24
    ws.row_dimensions[2].height = 32

    for row_idx, job in enumerate(jobs, start=3):
        parts = [p for p in job.parts.all() if not p.is_deleted]
        values = [
            job.job_number,
            job.get_purpose_display(),
            job.get_status_display(),
            job.outbound_date,
            job.expected_return_date,
            job.received_back_at,
            job.vendor.name,
            job.vendor_reference,
            job.vendor_contact_person,
            job.vendor_phone or (job.vendor.phone or ''),
            job.customer.name,
            job.customer.phone or '',
            job.device_model,
            job.serial_number,
            job.issue_description,
            job.accessories or '',
            job.device_condition or '',
            job.warranty_claim_number if job.is_warranty_claim else '',
            job.get_warranty_claim_status_display() if job.is_warranty_claim else '',
            job.purchase_date if job.is_warranty_claim else None,
            job.warranty_expiry_date if job.is_warranty_claim else None,
            job.warranty_document_ref if job.is_warranty_claim else '',
            job.dispatched_by,
            job.courier_name,
            job.tracking_number,
            job.return_condition or '',
            len(parts),
            sum(p.quantity for p in parts),
            sum(1 for p in parts if p.verification_status == 'pending'),
            sum(
                1 for p in parts
                if p.verification_status in VendorRepairJob.PARTS_DISCREPANCY_STATUSES
            ),
            float(job.estimate_amount) if job.estimate_amount is not None else None,
            float(job.final_amount or 0),
            float(job.amount_difference),
            job.vendor_invoice_number,
            job.vendor_invoice_date,
            'Yes' if job.parts_verified else 'No',
            job.verified_by.get_full_name() or job.verified_by.username
            if job.verified_by else '',
            job.created_by.get_full_name() or job.created_by.username
            if job.created_by else '',
            job.created_at.strftime('%d %b %Y %H:%M') if job.created_at else '',
            job.notes or '',
            job.verification_notes or '',
        ]
        for col_idx, value in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border = border_all
            cell.alignment = left
            if col_idx in (2, 4, 5, 6, 19, 20, 21, 27, 28, 29, 30, 36, 39):
                cell.alignment = center
            if col_idx in (31, 32, 33):
                cell.number_format = money_format
                cell.alignment = right

    if jobs:
        total_row = len(jobs) + 3
        ws.cell(row=total_row, column=30, value='TOTAL').font = total_font
        ws.cell(row=total_row, column=30).fill = total_fill
        status_rng = f'$C$3:$C${total_row - 1}'
        for col in (31, 32, 33):
            cell = ws.cell(row=total_row, column=col)
            letter = get_column_letter(col)
            # Match the on-screen total: cancelled jobs are excluded.
            cell.value = (
                f'=SUMIF({status_rng},"<>Cancelled",'
                f'{letter}3:{letter}{total_row - 1})'
            )
            cell.font = total_font
            cell.fill = total_fill
            cell.number_format = money_format
            cell.alignment = right

    ws.freeze_panes = 'A3'
    ws.auto_filter.ref = f'A2:{get_column_letter(len(headers))}2'

    # ---------- Parts sheet ----------
    ws2 = wb.create_sheet('Parts')
    part_headers = [
        ('Job #', 14), ('Vendor', 24), ('Customer', 22), ('Device', 22),
        ('Part Name', 26), ('Serial / Marking', 20), ('Qty', 8),
        ('Condition Sent', 20), ('Condition Returned', 20),
        ('Verification', 18), ('Remarks', 30),
    ]
    for col_idx, (label, width) in enumerate(part_headers, start=1):
        cell = ws2.cell(row=1, column=col_idx, value=label)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border_all
        ws2.column_dimensions[get_column_letter(col_idx)].width = width
    ws2.row_dimensions[1].height = 28

    part_row = 2
    for job in jobs:
        for part in job.parts.all():
            if part.is_deleted:
                continue
            values = [
                job.job_number, job.vendor.name, job.customer.name,
                job.device_model, part.name, part.identifier,
                part.quantity, part.condition_sent,
                part.condition_returned, part.get_verification_status_display(),
                part.remarks or '',
            ]
            for col_idx, value in enumerate(values, start=1):
                cell = ws2.cell(row=part_row, column=col_idx, value=value)
                cell.border = border_all
                cell.alignment = center if col_idx == 7 else left
            part_row += 1

    ws2.freeze_panes = 'A2'
    ws2.auto_filter.ref = (
        f'A1:{get_column_letter(len(part_headers))}1'
    )

    response = HttpResponse(
        content_type=(
            'application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sheet'
        )
    )
    filename = (
        f'vendor_repairs_{timezone.localtime():%Y%m%d_%H%M}.xlsx'
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


# ════════════════════════════════════════════════════════════
# CREATE / UPDATE / DELETE
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_create(request):
    template = (
        'vendor_repairs/partials/vendor_repair_form_modal.html'
        if is_htmx(request) else 'vendor_repairs/vendor_repair_form.html'
    )

    if request.method == 'POST':
        form = VendorRepairJobForm(request.POST)
        part_rows, part_errors = _part_rows_from_post(request)
        if form.is_valid() and not part_errors:
            job = form.save(commit=False)
            job.created_by = request.user
            job.save()
            _sync_form_parts(job, part_rows)
            if is_htmx(request):
                return _redirect_to_detail(
                    job, f'Vendor repair {job.job_number} created.',
                )
            messages.success(
                request, f'Vendor repair {job.job_number} created.',
            )
            return redirect_to_staff('vendor_repair_detail', pk=job.pk)
        for error in part_errors:
            form.add_error(None, error)
    else:
        form = VendorRepairJobForm()

    if is_htmx(request) and form.errors:
        return htmx_response(
            request, template, {
                'form': form,
                'job': None,
                'is_htmx': True,
                **_form_parts_context(request, None),
            },
            toast={
                'level': 'danger',
                'message': 'Vendor repair not saved — please fix the highlighted fields.',
            },
        )
    return render(request, template, {
        'form': form,
        'job': None,
        'is_htmx': is_htmx(request),
        **_form_parts_context(request, None),
    })


@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_update(request, pk):
    job = get_object_or_404(VendorRepairJob, pk=pk)
    template = (
        'vendor_repairs/partials/vendor_repair_form_modal.html'
        if is_htmx(request) else 'vendor_repairs/vendor_repair_form.html'
    )

    if request.method == 'POST':
        form = VendorRepairJobForm(request.POST, instance=job)
        part_rows, part_errors = _part_rows_from_post(request)
        if form.is_valid() and not part_errors:
            form.save()
            _sync_form_parts(job, part_rows)
            if is_htmx(request):
                return _redirect_to_detail(
                    job, f'Vendor repair {job.job_number} updated.',
                )
            messages.success(
                request, f'Vendor repair {job.job_number} updated.',
            )
            return redirect_to_staff('vendor_repair_detail', pk=job.pk)
        for error in part_errors:
            form.add_error(None, error)
    else:
        form = VendorRepairJobForm(instance=job)

    if is_htmx(request) and form.errors:
        return htmx_response(
            request, template, {
                'form': form,
                'job': job,
                'is_htmx': True,
                **_form_parts_context(request, job),
            },
            toast={
                'level': 'danger',
                'message': 'Vendor repair not saved — please fix the highlighted fields.',
            },
        )
    return render(request, template, {
        'form': form,
        'job': job,
        'is_htmx': is_htmx(request),
        **_form_parts_context(request, job),
    })


@login_required
@csrf_protect
@require_POST
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_delete(request, pk):
    job = get_object_or_404(VendorRepairJob, pk=pk)
    number = job.job_number
    with transaction.atomic():
        job.delete()  # soft-deletes the job and every part

    if is_htmx(request):
        return htmx_response(
            request,
            'vendor_repairs/partials/vendor_repair_table.html',
            _list_context(request),
            toast={
                'level': 'success',
                'message': f'Vendor repair {number} deleted.',
            },
            extra_headers={
                'HX-Retarget': '#vendor-repair-table-container',
                'HX-Reswap': 'innerHTML',
            },
        )

    messages.success(request, f'Vendor repair {number} deleted.')
    return redirect_to_staff('vendor_repair_list')


# ════════════════════════════════════════════════════════════
# DETAIL / PRINT
# ════════════════════════════════════════════════════════════
@login_required
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_detail(request, pk):
    job = get_object_or_404(
        _base_queryset().prefetch_related('parts'),
        pk=pk,
    )
    context = _parts_context(job)
    context.update({
        'part_form': VendorRepairPartForm(),
        'verify_form': VendorRepairPartVerifyForm(),
        'return_form': VendorRepairReturnForm(job=job),
        'complete_form': VendorRepairCompleteForm(),
        'cancel_form': VendorRepairCancelForm(),
    })
    return render(request, 'vendor_repairs/vendor_repair_detail.html', context)


@login_required
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_print(request, pk):
    job = get_object_or_404(
        VendorRepairJob.objects.select_related('customer', 'vendor'),
        pk=pk,
    )
    parts = list(job.parts.all())
    company = CompanyProfile.get_instance()
    return render(request, 'vendor_repairs/vendor_repair_print.html', {
        'job': job,
        'parts': parts,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
    })


# ════════════════════════════════════════════════════════════
# PART OPERATIONS (HTMX)
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_add_part(request, pk):
    job = get_object_or_404(VendorRepairJob, pk=pk)

    if job.status not in PART_EDITABLE_STATUSES:
        return toast_only_response(
            {'level': 'danger', 'message': 'Parts cannot be changed on a closed job.'},
            status=400,
        )

    if request.method == 'POST':
        form = VendorRepairPartForm(request.POST)
        if form.is_valid():
            part = form.save(commit=False)
            part.job = job
            with transaction.atomic():
                part.save()
                if job.parts_verified:
                    job.parts_verified = False
                    job.save(update_fields=['parts_verified'])
            return _parts_section_response(
                request, job,
                {
                    'level': 'success',
                    'message': f'Part "{part.name}" added.',
                },
            )
        return render(
            request, 'vendor_repairs/partials/part_form_modal.html',
            {'form': form, 'job': job, 'part': None},
        )

    return render(
        request, 'vendor_repairs/partials/part_form_modal.html',
        {'form': VendorRepairPartForm(), 'job': job, 'part': None},
    )


@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_edit_part(request, part_pk):
    part = get_object_or_404(VendorRepairPart, pk=part_pk)
    job = part.job

    if job.status not in PART_EDITABLE_STATUSES:
        return toast_only_response(
            {'level': 'danger', 'message': 'Parts cannot be changed on a closed job.'},
            status=400,
        )

    if request.method == 'POST':
        form = VendorRepairPartForm(request.POST, instance=part)
        if form.is_valid():
            form.save()
            return _parts_section_response(
                request, job,
                {
                    'level': 'success',
                    'message': f'Part "{part.name}" updated.',
                },
            )
        return render(
            request, 'vendor_repairs/partials/part_form_modal.html',
            {'form': form, 'job': job, 'part': part},
        )

    return render(
        request, 'vendor_repairs/partials/part_form_modal.html',
        {'form': VendorRepairPartForm(instance=part), 'job': job, 'part': part},
    )


@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_verify_part(request, part_pk):
    part = get_object_or_404(VendorRepairPart, pk=part_pk)
    job = part.job

    if job.status not in PART_VERIFIABLE_STATUSES:
        return toast_only_response(
            {
                'level': 'warning',
                'message': 'Mark the device as returned before verifying parts.',
            },
            status=400,
        )

    if request.method == 'POST':
        form = VendorRepairPartVerifyForm(request.POST)
        if form.is_valid():
            part.verification_status = form.cleaned_data['verification_status']
            part.condition_returned = form.cleaned_data['condition_returned']
            part.remarks = form.cleaned_data['remarks']
            with transaction.atomic():
                part.save(update_fields=[
                    'verification_status', 'condition_returned',
                    'remarks', 'updated_at',
                ])
                # A verified job becomes unverified if a part is re-opened.
                if job.parts_verified and part.verification_status == 'pending':
                    job.parts_verified = False
                    job.save(update_fields=['parts_verified'])
            level = 'warning' if part.is_discrepancy else 'success'
            return _parts_section_response(
                request, job,
                {
                    'level': level,
                    'message': (
                        f'"{part.name}" marked {part.get_verification_status_display()}.'
                    ),
                },
            )
        return render(
            request, 'vendor_repairs/partials/part_verify_modal.html',
            {'form': form, 'job': job, 'part': part},
        )

    form = VendorRepairPartVerifyForm(initial={
        'verification_status': part.verification_status,
        'condition_returned': part.condition_returned,
        'remarks': part.remarks,
    })
    return render(
        request, 'vendor_repairs/partials/part_verify_modal.html',
        {'form': form, 'job': job, 'part': part},
    )


@login_required
@csrf_protect
@require_POST
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_delete_part(request, part_pk):
    part = get_object_or_404(VendorRepairPart, pk=part_pk)
    job = part.job

    if job.status not in PART_EDITABLE_STATUSES:
        return toast_only_response(
            {'level': 'danger', 'message': 'Parts cannot be changed on a closed job.'},
            status=400,
        )

    name = part.name
    with transaction.atomic():
        part.delete()
        if job.parts_verified:
            job.parts_verified = False
            job.save(update_fields=['parts_verified'])
    return _parts_section_response(
        request, job,
        {'level': 'success', 'message': f'Part "{name}" removed.'},
    )


@login_required
@csrf_protect
@require_POST
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_verify_all_parts(request, pk):
    """Mark every still-pending part as OK (bulk physical check)."""
    job = get_object_or_404(VendorRepairJob, pk=pk)

    if job.status not in PART_VERIFIABLE_STATUSES:
        return toast_only_response(
            {
                'level': 'warning',
                'message': 'Mark the device as returned before verifying parts.',
            },
            status=400,
        )

    pending = job.parts.filter(verification_status='pending')
    count = pending.count()
    if count:
        pending.update(verification_status='ok', updated_at=timezone.now())
    return _parts_section_response(
        request, job,
        {
            'level': 'success',
            'message': (
                f'{count} pending part(s) marked OK.' if count
                else 'No pending parts to verify.'
            ),
        },
    )


# ════════════════════════════════════════════════════════════
# STATUS ACTIONS
# ════════════════════════════════════════════════════════════
@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_mark_returned(request, pk):
    job = get_object_or_404(VendorRepairJob, pk=pk)

    if job.status not in ('sent', 'in_repair'):
        return toast_only_response(
            {'level': 'warning', 'message': 'This job is not with the vendor.'},
            status=400,
        )

    if request.method == 'POST':
        form = VendorRepairReturnForm(request.POST, job=job)
        if form.is_valid():
            job.received_back_at = form.cleaned_data['received_back_at']
            job.received_by = form.cleaned_data['received_by']
            job.return_condition = form.cleaned_data['return_condition']
            job.status = 'returned'
            job.save(update_fields=[
                'received_back_at', 'received_by', 'return_condition',
                'status', 'updated_at',
            ])
            if is_htmx(request):
                return _redirect_to_detail(
                    job,
                    f'{job.job_number} marked returned — verify the parts now.',
                )
            messages.success(
                request,
                f'{job.job_number} marked returned — verify the parts now.',
            )
            return redirect_to_staff('vendor_repair_detail', pk=job.pk)
    else:
        form = VendorRepairReturnForm(job=job)

    return render(
        request, 'vendor_repairs/partials/return_modal.html',
        {'form': form, 'job': job},
    )


@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_mark_completed(request, pk):
    job = get_object_or_404(
        VendorRepairJob.objects.prefetch_related('parts'), pk=pk,
    )

    if job.status == 'completed':
        return toast_only_response(
            {'level': 'info', 'message': 'This job is already completed.'},
            status=400,
        )
    if job.status != 'returned':
        return toast_only_response(
            {
                'level': 'warning',
                'message': 'Mark the device as returned before completing the job.',
            },
            status=400,
        )

    pending = job.parts_pending_count
    if request.method == 'POST':
        form = VendorRepairCompleteForm(request.POST)
        if pending:
            form.add_error(
                None,
                f'{pending} part(s) are still Pending Check. Verify every part '
                f'before completing the job.',
            )
        if not form.errors and form.is_valid():
            job.status = 'completed'
            job.parts_verified = True
            job.verified_at = timezone.now()
            job.verified_by = request.user
            job.verification_notes = form.cleaned_data['verification_notes']
            job.save(update_fields=[
                'status', 'parts_verified', 'verified_at', 'verified_by',
                'verification_notes', 'updated_at',
            ])
            if is_htmx(request):
                return _redirect_to_detail(
                    job, f'{job.job_number} completed and verified.',
                )
            messages.success(request, f'{job.job_number} completed and verified.')
            return redirect_to_staff('vendor_repair_detail', pk=job.pk)
    else:
        form = VendorRepairCompleteForm(initial={
            'verification_notes': job.verification_notes or '',
        })

    return render(
        request, 'vendor_repairs/partials/complete_modal.html',
        {'form': form, 'job': job, 'pending_parts': pending},
    )


@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:vendor_repair_list')
def vendor_repair_cancel(request, pk):
    job = get_object_or_404(VendorRepairJob, pk=pk)

    if job.status in ('completed', 'cancelled'):
        return toast_only_response(
            {'level': 'danger', 'message': 'This job cannot be cancelled.'},
            status=400,
        )

    if request.method == 'POST':
        form = VendorRepairCancelForm(request.POST)
        if form.is_valid():
            job.status = 'cancelled'
            job.cancel_reason = form.cleaned_data['reason']
            job.save()
            if is_htmx(request):
                return _redirect_to_detail(
                    job, f'{job.job_number} cancelled.',
                )
            messages.success(request, f'{job.job_number} cancelled.')
            return redirect_to_staff('vendor_repair_detail', pk=job.pk)
    else:
        form = VendorRepairCancelForm()

    return render(
        request, 'vendor_repairs/partials/cancel_modal.html',
        {'form': form, 'job': job},
    )
