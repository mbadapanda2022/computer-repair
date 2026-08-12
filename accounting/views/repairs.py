# accounting/views/repairs.py
import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.contrib import messages
from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from django.db.models import Q, Sum, F
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required

from ..models import *
from ..forms import RepairJobForm, RepairPartForm
from accounting.utils.notification_helpers import (
    
    send_notification_to_customer,
    send_notification_sse
)
from .utils import is_htmx, htmx_response, redirect_to_staff, toast_only_response
from ..decorators import handle_errors
from .sales import create_or_update_invoice_ledger

logger = logging.getLogger(__name__)


# ============================================================
# HELPER: PAGINATED REPAIRS CONTEXT
# ============================================================
def get_paginated_repairs_context(request, queryset=None):
    if queryset is None:
        queryset = RepairJob.objects.select_related('customer').all().order_by('-created_at')

    search = request.GET.get('search', '').strip()
    status = request.GET.get('status', '')
    customer_id = request.GET.get('customer', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    reset = request.GET.get('reset', '')
    page_number = request.GET.get('page', 1)

    if reset:
        return redirect_to_staff('repair_list')

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

    paginator = Paginator(queryset, 20)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    customers = Contact.objects.filter(contact_type__in=['customer', 'both']).order_by('name')

    context = {
        'jobs': page_obj.object_list,
        'page_obj': page_obj,
        'customers': customers,
        'search': search,
        'status_filter': status,
        'customer_id': customer_id,
        'date_from': date_from,
        'date_to': date_to,
        'status_choices': RepairJob.STATUS_CHOICES,
    }
    return context


# ============================================================
# 1. FIELD VALIDATION (HTMX)
# ============================================================
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
        error_html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
        for err in errors:
            error_html += f'<div>{err}</div>'
        error_html += '</div>'
        return HttpResponse(error_html)
    except Exception as e:
        logger.error(f"Validation error on {field_name}: {e}")
        return HttpResponse(f'<div id="field-{field_name}" class="invalid-feedback d-block">Server validation error</div>')


# ============================================================
# 2. REPAIR LIST (with pagination & filters)
# ============================================================
@handle_errors(default_redirect='accounting:repair_list')
def repair_list(request):
    context = get_paginated_repairs_context(request)

    if is_htmx(request):
        return render(request, 'repairs/partials/repair_table.html', context)
    return render(request, 'repairs/repair_list.html', context)


# ============================================================
# 3. PRINT LIST
# ============================================================
def repair_list_print(request):
    search = request.GET.get('search', '')
    status = request.GET.get('status', '')
    customer_id = request.GET.get('customer', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    jobs = RepairJob.objects.select_related('customer').all().order_by('-date_in')
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
    logo_exists = bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name))

    customer_name = None
    if customer_id:
        try:
            customer_name = Contact.objects.get(pk=customer_id).name
        except Contact.DoesNotExist:
            pass

    context = {
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
    }
    return render(request, 'repairs/repair_list_print.html', context)


# ============================================================
# 4. REPAIR CREATE (general)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list', htmx_template='repairs/partials/repair_form_modal.html')
def repair_create(request):
    template_name = 'repairs/partials/repair_form_modal.html' if is_htmx(request) else 'repairs/repair_form.html'

    if request.method == 'POST':
        form = RepairJobForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                job = form.save()
                send_notification_to_customer(
                    job.customer,
                    title=f"Repair Job Created: {job.job_number}",
                    message=f"Your repair for {job.device_model} has been received.",
                    link=reverse('customer:customer_repair_detail', args=[job.pk]),
                    notif_type='success',
                    category='repairs',
                    send_email=True
                )
                for staff in User.objects.filter(is_staff=True):
                    send_notification_sse(staff)

                if is_htmx(request):
                    messages.success(request, f"Repair job {job.job_number} created.")
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:repair_detail', args=[job.pk])
                    return response

                messages.success(request, f"Repair job {job.job_number} created.")
                return redirect_to_staff('repair_detail', pk=job.pk)
        else:
            if is_htmx(request):
                return render(request, 'repairs/partials/repair_form_modal.html', {'form': form})
    else:
        form = RepairJobForm()

    return render(request, template_name, {'form': form})


# ============================================================
# 5. REPAIR UPDATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list', htmx_template='repairs/partials/repair_form_modal.html')
def repair_update(request, pk):
    job = get_object_or_404(RepairJob.objects.select_related('customer'), pk=pk)
    template_name = 'repairs/partials/repair_form_modal.html' if is_htmx(request) else 'repairs/repair_form.html'

    if request.method == 'POST':
        form = RepairJobForm(request.POST, instance=job)
        
        if form.is_valid():
            with transaction.atomic():
                # Save without committing to DB yet
                job = form.save(commit=False)
                
                # Auto-set delivery_date if status is 'delivered' and date is empty
                if job.status == 'delivered' and not job.delivery_date:
                    job.delivery_date = timezone.now().date()
                
                # Save the job with updated fields
                job.save()
                
                # Recalculate final amount
                parts_total = job.parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
                job.final_amount = parts_total + job.labour_charge
                job.save(update_fields=['final_amount'])

                # Send notifications
                send_notification_to_customer(
                    job.customer,
                    title=f"Repair Job Updated: {job.job_number}",
                    message=f"Your repair for {job.device_model} has been updated.",
                    link=reverse('customer:customer_repair_detail', args=[job.pk]),
                    notif_type='info',
                    category='repairs',
                    send_email=False
                )
                for staff in User.objects.filter(is_staff=True):
                    send_notification_sse(staff)

                # HTMX Response
                if is_htmx(request):
                    messages.success(request, f"Repair job {job.job_number} updated.")
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:repair_detail', args=[job.pk])
                    return response

                # Non-HTMX Response
                messages.success(request, f"Repair job {job.job_number} updated.")
                return redirect_to_staff('repair_detail', pk=job.pk)

        else:
            # ❌ Form Invalid – keep in modal
            if is_htmx(request):
                return render(request, 'repairs/partials/repair_form_modal.html', {
                    'form': form,
                    'job': job
                })
            # Full page mode – render form with errors
            return render(request, template_name, {'form': form, 'job': job})

    else:
        # GET Request – show edit form
        form = RepairJobForm(instance=job)
        return render(request, template_name, {'form': form, 'job': job})


# ============================================================
# 6. REPAIR DETAIL
# ============================================================
def repair_detail(request, pk):
    job = get_object_or_404(RepairJob.objects.select_related('customer', 'invoice'), pk=pk)
    parts = job.parts.select_related('product').all()
    part_form = RepairPartForm()
    parts_total = parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')

    context = {
        'job': job,
        'parts': parts,
        'parts_total': parts_total,
        'part_form': part_form,
    }
    return render(request, 'repairs/repair_detail.html', context)


# ============================================================
# 7. UPDATE STATUS
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list')
def update_repair_status(request, pk):
    job = get_object_or_404(RepairJob, pk=pk)

    if request.method != 'POST':
        return redirect_to_staff('repair_detail', pk=pk)

    new_status = request.POST.get('status')
    if not new_status or new_status not in dict(RepairJob.STATUS_CHOICES):
        return toast_only_response(
            {'level': 'danger', 'message': 'Invalid status selected.'},
            status=400
        )

    old_status = job.status
    if old_status == new_status:
        return toast_only_response(
            {'level': 'warning', 'message': 'Status is already set to that value.'},
            status=200
        )

    if old_status in ('delivered', 'cancelled'):
        return toast_only_response(
            {'level': 'error', 'message': f'Cannot change status from {job.get_status_display()}.'},
            status=400
        )

    with transaction.atomic():
        if new_status == 'cancelled' and old_status != 'cancelled':
            for part in job.parts.all():
                StockMovement.objects.create(
                    product=part.product,
                    movement_type='return_in',
                    quantity=part.quantity,
                    reference=f"CANCEL-{job.job_number}",
                    date=timezone.now().date(),
                    notes=f"Stock returned due to cancellation of repair job {job.job_number}"
                )
                Product.objects.filter(pk=part.product_id).update(current_stock=F('current_stock') + part.quantity)

        job.status = new_status
        job.save(update_fields=['status'])

        send_notification_to_customer(
            job.customer,
            title=f"Repair Status Updated: {job.job_number}",
            message=f"Your repair for {job.device_model} is now {job.get_status_display()}.",
            link=reverse('customer:customer_repair_detail', args=[job.pk]),
            notif_type='info',
            category='repairs',
            send_email=False
        )
        for staff in User.objects.filter(is_staff=True):
            send_notification_sse(staff)

        if is_htmx(request):
            return render(request, 'repairs/partials/status_row.html', {'job': job})

        messages.success(request, f"Status updated to {job.get_status_display()}.")
        return redirect_to_staff('repair_detail', pk=pk)


# ============================================================
# 8. ADD REPAIR PART (WITH STOCK VALIDATION)
# ============================================================

@csrf_protect
@handle_errors(default_redirect='accounting:repair_list', htmx_template='repairs/partials/part_form_modal.html')
def add_repair_part(request, pk):
    job = get_object_or_404(RepairJob, pk=pk)

    if request.method == 'POST':
        form = RepairPartForm(request.POST)
        if form.is_valid():
            product = form.cleaned_data['product']
            quantity = form.cleaned_data['quantity']

            # Stock Validation (if not service)
            if not product.is_service and product.current_stock < quantity:
                error_msg = f"Insufficient stock for {product.name}. Available: {product.current_stock}"
                if is_htmx(request):
                    form.add_error('quantity', error_msg)
                    return render(request, 'repairs/partials/part_form_modal.html', {
                        'form': form,
                        'job': job,
                    })
                else:
                    messages.error(request, error_msg)
                    return redirect_to_staff('repair_detail', pk=job.pk)

            with transaction.atomic():
                # 1. Save the part
                part = form.save(commit=False)
                part.repair_job = job
                part.save()

                # 2. REDUCE STOCK (if not service)
                if not product.is_service:
                    # Create StockMovement
                    StockMovement.objects.create(
                        product=product,
                        movement_type='repair_out',
                        quantity=-quantity,  # Negative because stock is going OUT
                        reference=f"REP-{job.job_number}",
                        date=timezone.now().date(),
                        notes=f"Part used in repair {job.job_number}"
                    )
                    # Update product current_stock using F() to avoid race conditions
                    Product.objects.filter(pk=product.pk).update(
                        current_stock=F('current_stock') - quantity
                    )

                # 3. Recalculate job final amount
                parts_total = job.parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
                job.final_amount = parts_total + job.labour_charge
                job.save(update_fields=['final_amount'])

                # 4. Notifications
                send_notification_to_customer(
                    job.customer,
                    title=f"Part Added to Repair: {job.job_number}",
                    message=f"A new part '{product.name}' has been added to your repair.",
                    link=reverse('customer:customer_repair_detail', args=[job.pk]),
                    notif_type='info',
                    category='repairs',
                    send_email=False
                )
                for staff in User.objects.filter(is_staff=True):
                    send_notification_sse(staff)

                # 5. HTMX Response
                if is_htmx(request):
                    parts = job.parts.all()
                    parts_total = parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
                    response = render(request, 'repairs/partials/parts_with_totals.html', {
                        'parts': parts,
                        'job': job,
                        'parts_total': parts_total,
                    })
                    response['HX-Trigger'] = json.dumps({
                        'closeModal': '',
                        'showToast': {
                            'level': 'success',
                            'message': f'Part "{product.name}" added. Stock updated.'
                        }
                    })
                    return response

                messages.success(request, f"Part '{product.name}' added successfully.")
                return redirect_to_staff('repair_detail', pk=job.pk)

        else:
            if is_htmx(request):
                return render(request, 'repairs/partials/part_form_modal.html', {
                    'form': form,
                    'job': job,
                })
    else:
        form = RepairPartForm()
        return render(request, 'repairs/partials/part_form_modal.html', {
            'form': form,
            'job': job,
        })

    return redirect_to_staff('repair_detail', pk=job.pk)

# ============================================================
# 9. REMOVE REPAIR PART (WITH STOCK VALIDATION)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list')
def remove_repair_part(request, part_pk):
    part = get_object_or_404(RepairPart.objects.select_related('repair_job', 'product'), pk=part_pk)
    job = part.repair_job
    product = part.product

    with transaction.atomic():
        if not product.is_service:
            StockMovement.objects.create(
                product=product,
                movement_type='return_in',
                quantity=part.quantity,
                reference=f"REMOVE-{job.job_number}",
                date=timezone.now().date(),
                notes=f"Stock returned due to removal of part from repair job {job.job_number}"
            )
            Product.objects.filter(pk=product.pk).update(current_stock=F('current_stock') + part.quantity)

        part.delete()

        parts = job.parts.all()
        parts_total = parts.aggregate(total=Sum('line_total'))['total'] or Decimal('0')
        job.final_amount = parts_total + job.labour_charge
        job.save(update_fields=['final_amount'])

        send_notification_to_customer(
            job.customer,
            title=f"Part Removed from Repair: {job.job_number}",
            message=f"The part '{product.name}' has been removed from your repair.",
            link=reverse('customer:customer_repair_detail', args=[job.pk]),
            notif_type='info',
            category='repairs',
            send_email=False
        )
        for staff in User.objects.filter(is_staff=True):
            send_notification_sse(staff)

        if is_htmx(request):
            response = render(request, 'repairs/partials/parts_with_totals.html', {
                'parts': parts,
                'job': job,
                'parts_total': parts_total,
            })
            # Toast Notification 
            response['HX-Trigger'] = json.dumps({
                'showToast': {
                    'level': 'success',
                    'message': f'Part "{product.name}" removed successfully.'
                }
            })
            return response

        messages.success(request, f'Part "{product.name}" removed successfully.')
        return redirect_to_staff('repair_detail', pk=job.pk)


# ============================================================
# 10. CREATE INVOICE FROM REPAIR
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list')
def create_invoice_from_repair(request, pk):
    job = get_object_or_404(RepairJob.objects.select_related('customer'), pk=pk)

    if request.method == 'GET':
        return render(request, 'repairs/partials/create_invoice_modal.html', {'job': job})

    invoice_date = timezone.now().date()
    if request.POST.get('invoice_date'):
        try:
            from datetime import datetime
            invoice_date = datetime.strptime(request.POST.get('invoice_date'), '%Y-%m-%d').date()
        except (ValueError, TypeError):
            pass

    # --- Validations ---
    if job.invoice:
        messages.info(request, "Invoice already exists for this repair.")
        return redirect_to_staff('invoice_detail', pk=job.invoice.pk)

    if job.status not in ('ready', 'delivered'):
        messages.error(request, "Repair must be 'Ready' or 'Delivered' to generate invoice.")
        return redirect_to_staff('repair_detail', pk=pk)

    if job.estimated_cost and job.estimated_cost > 0 and job.estimate_status != 'approved':
        messages.error(request, "Estimate must be approved before invoicing.")
        return redirect_to_staff('repair_detail', pk=pk)

    # --- Create Invoice ---
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
            notes=notes
        )
        invoice.save()

        # Parts → Invoice Items
        for part in job.parts.select_related('product'):
            InvoiceItem.objects.create(
                invoice=invoice,
                product=part.product,
                quantity=part.quantity,
                unit_price=part.unit_price,
                tax_rate=part.product.tax_rate,
                description=f"Repair part: {part.product.name}"
            )

        # Labour Charge
        if job.labour_charge > 0:
            tax_rate = company.default_tax_rate
            labour_product, _ = Product.objects.get_or_create(
                name="Repair Labour",
                defaults={
                    'is_service': True,
                    'selling_price': job.labour_charge,
                    'tax_rate': tax_rate,
                    'hsn_code': '998446'
                }
            )
            InvoiceItem.objects.create(
                invoice=invoice,
                product=labour_product,
                quantity=1,
                unit_price=job.labour_charge,
                tax_rate=tax_rate,
                description="Labour Charge"
            )

        invoice.calculate_totals()
        invoice.save()
        invoice.update_stock_from_items()
        create_or_update_invoice_ledger(invoice)

        # Repair Job Link
        job.invoice = invoice
        if job.status == 'ready':
            job.status = 'delivered'
        job.save(update_fields=['invoice', 'status'])

        # Notifications
        send_notification_to_customer(
            job.customer,
            title=f"Invoice Generated: {invoice.invoice_number}",
            message=f"Invoice for repair job {job.job_number} is ready. Date: {invoice_date}",
            link=reverse('customer:customer_invoice_detail', args=[invoice.pk]),
            notif_type='success',
            category='sales',
            send_email=True
        )
        for staff in User.objects.filter(is_staff=True):
            send_notification_sse(staff)

        messages.success(request, f"Invoice {invoice.invoice_number} created successfully for date {invoice_date}.")
        
        if is_htmx(request):
            response = HttpResponse()
            response['HX-Redirect'] = reverse('accounting:invoice_detail', args=[invoice.pk])
            response['HX-Trigger'] = json.dumps({
                'showToast': {'level': 'success', 'message': f'Invoice {invoice.invoice_number} created.'},
                'closeModal': ''
            })
            return response
        
        return redirect_to_staff('invoice_detail', pk=invoice.pk)


# ============================================================
# 11. DELETE REPAIR
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:repair_list')
def repair_delete(request, pk):
    job = get_object_or_404(RepairJob, pk=pk)

    if job.invoice:
        return toast_only_response(
            {'level': 'danger', 'message': 'Cannot delete job with linked invoice.'},
            status=400
        )

    with transaction.atomic():
        for part in job.parts.all():
            StockMovement.objects.create(
                product=part.product,
                movement_type='return_in',
                quantity=part.quantity,
                reference=f"JOB-DELETE-{job.job_number}",
                date=timezone.now().date(),
                notes=f"Stock returned due to deletion of repair job {job.job_number}"
            )
            Product.objects.filter(pk=part.product_id).update(current_stock=F('current_stock') + part.quantity)

        job.delete()
        for staff in User.objects.filter(is_staff=True):
            send_notification_sse(staff)

        if is_htmx(request):
            return htmx_response(
                request,
                'repairs/partials/repair_table.html',
                context=get_paginated_repairs_context(request),
                toast={'level': 'success', 'message': 'Repair job deleted successfully.'}
            )
        messages.success(request, "Repair job deleted successfully.")
        return redirect_to_staff('repair_list')


# ============================================================
# 12. PRINT REPAIR
# ============================================================
def repair_print(request, pk):
    job = get_object_or_404(RepairJob.objects.select_related('customer', 'invoice'), pk=pk)
    parts = job.parts.select_related('product').all()
    company = CompanyProfile.get_instance()
    logo_exists = bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name))

    context = {
        'job': job,
        'parts': parts,
        'company': company,
        'logo_exists': logo_exists,
        'invoice': job.invoice if job.invoice else None,
    }
    return render(request, 'repairs/repair_print.html', context)


# ============================================================
# 13. REPAIR CREATE FOR SPECIFIC CONTACT
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:repair_list', htmx_template='contacts/repair_form_from_contact.html')
def repair_create_for_contact(request, contact_id):
    contact = get_object_or_404(Contact, pk=contact_id)

    if request.method == 'POST':
        form = RepairJobForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                job = form.save(commit=False)
                job.customer = contact
                job.save()
                form.save_m2m()

                send_notification_to_customer(
                    job.customer,
                    title=f"Repair Job Created: {job.job_number}",
                    message=f"Your repair for {job.device_model} has been received.",
                    link=reverse('customer:customer_repair_detail', args=[job.pk]),
                    notif_type='success',
                    category='repairs',
                    send_email=True
                )
                for staff in User.objects.filter(is_staff=True):
                    send_notification_sse(staff)

                if is_htmx(request):
                    messages.success(request, f"Repair job {job.job_number} created.")
                    response = HttpResponse()
                    response['HX-Redirect'] = reverse('accounting:repair_detail', args=[job.pk])
                    return response

                messages.success(request, f"Repair job {job.job_number} created.")
                return redirect_to_staff('repair_list')
        else:
            if is_htmx(request):
                return render(request, 'contacts/repair_form_from_contact.html', {'form': form, 'contact': contact})
    else:
        form = RepairJobForm(initial={'customer': contact})
        return render(request, 'contacts/repair_form_from_contact.html', {'form': form, 'contact': contact})


# ============================================================
# 14. STAFF APPROVE ESTIMATE
# ============================================================
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
            repair.approval_remarks = remarks if remarks else f"Approved by {request.user.get_full_name()} via {dict(RepairJob._meta.get_field('approval_source').choices).get(source, source)}"

            if repair.status in ['pending', 'diagnosis']:
                repair.status = 'repairing'

            repair.save()

            # Notifications
            send_notification_to_customer(
                repair.customer,
                title=f"✅ Your repair {repair.job_number} has been approved",
                message=f"Your repair for {repair.device_model} has been approved and will start shortly.",
                link=reverse('customer:customer_repair_detail', args=[repair.pk]),
                notif_type='success',
                category='repairs',
                send_email=False
            )
            for staff in User.objects.filter(is_staff=True):
                send_notification_sse(staff)

            messages.success(request, f"✅ Estimate for {repair.job_number} approved! (Source: {source})")

            # HTMX → Redirect the whole page (not just modal)
            if is_htmx(request):
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:repair_detail', args=[repair.pk])
                response['HX-Trigger'] = json.dumps({'closeModal': ''})  # Close modal just in case
                return response

            # ❌ Non-HTMX → Normal redirect
            return redirect('accounting:repair_detail', pk=repair.pk)

    # GET request → show the modal form
    return render(request, 'repairs/partials/staff_approve_modal.html', {'repair': repair})

