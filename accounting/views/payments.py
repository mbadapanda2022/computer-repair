# accounting/views/payments.py (only for staff)

import json
import logging
from decimal import Decimal, InvalidOperation

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.template.loader import render_to_string
from django.contrib.auth.models import User
from django.urls import reverse

from ..models import (
    Payment, BankTransaction, Contact, Invoice, PaymentAllocation,
)
from ..forms import PaymentForm
from .utils import is_htmx, htmx_response, redirect_to_staff
from ..decorators import handle_errors

from accounting.utils.notification_helpers import (
    send_notification_to_customer,
    send_notification_to_staff,
    send_notification_sse,
)

logger = logging.getLogger(__name__)


# ============================================================
# HELPER: Get unpaid invoices for a contact
# ============================================================
def get_unpaid_invoices_for_contact(contact, direction='received'):
    """
    Get unpaid or partially-paid invoices for a contact.
    Only meaningful for customer-side payments (direction='received').
    """
    if direction == 'received' and contact and contact.contact_type in ('customer', 'both'):
        return Invoice.objects.filter(
            customer=contact,
            balance_due__gt=0,
        ).exclude(
            payment_status='paid'
        ).order_by('date', 'id')
    return Invoice.objects.none()


# ============================================================
# HELPER: GET PAGINATED PAYMENTS CONTEXT
# ============================================================
def get_paginated_payments_context(request, queryset=None):
    """Shared logic to filter, paginate, and annotate payments."""
    if queryset is None:
        queryset = Payment.objects.select_related(
            'contact', 'bank_account'
        ).prefetch_related(
            'allocations__invoice'
        ).all().order_by('-date', '-id')

    search = request.GET.get('search', '').strip()
    direction = request.GET.get('direction', '')
    method = request.GET.get('method', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    page_number = request.GET.get('page', 1)

    if search:
        queryset = queryset.filter(
            Q(contact__name__icontains=search) |
            Q(contact__phone__icontains=search) |
            Q(reference__icontains=search) |
            Q(upi_ref__icontains=search) |
            Q(description__icontains=search) |
            Q(allocations__invoice__invoice_number__icontains=search)
        ).distinct()
    if direction:
        queryset = queryset.filter(direction=direction)
    if method:
        queryset = queryset.filter(method=method)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    paginator = Paginator(queryset, 15)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    total_received = queryset.filter(direction='received').aggregate(
        Sum('amount')
    )['amount__sum'] or Decimal('0')
    total_paid = queryset.filter(direction='paid').aggregate(
        Sum('amount')
    )['amount__sum'] or Decimal('0')
    upi_count = queryset.filter(method='upi').count()
    pending_reconciliation = queryset.filter(reconciled=False).count()
    total_amount = queryset.aggregate(Sum('amount'))['amount__sum'] or Decimal('0')

    context = {
        'payments': page_obj.object_list,
        'page_obj': page_obj,
        'total_received': total_received,
        'total_paid': total_paid,
        'upi_count': upi_count,
        'pending_reconciliation': pending_reconciliation,
        'total_amount': total_amount,
        'search': search,
        'direction': direction,
        'method': method,
        'date_from': date_from,
        'date_to': date_to,
    }
    return context


# ============================================================
# PAYMENT LIST
# ============================================================
@handle_errors(default_redirect='accounting:payment_list')
def payment_list(request):
    context = get_paginated_payments_context(request)
    if is_htmx(request):
        return render(request, 'payments/partials/payment_table.html', context)
    return render(request, 'payments/payment_list.html', context)


# ============================================================
# HELPER: Load Unpaid Invoices (HTMX endpoint for allocation UI)
# ============================================================
@require_http_methods(["GET"])
def load_unpaid_invoices(request):
    """
    HTMX endpoint: return partial HTML with unpaid invoices for a contact.
    Used by payment form to load allocation section when contact selected.
    """
    contact_id = request.GET.get('contact', '').strip()
    direction = request.GET.get('direction', 'received').strip()

    if not contact_id:
        return HttpResponse('')

    try:
        contact = Contact.objects.get(pk=contact_id)
    except (Contact.DoesNotExist, ValueError):
        return HttpResponse('')

    if direction == 'received':
        unpaid_invoices = get_unpaid_invoices_for_contact(contact, 'received')
    else:
        unpaid_invoices = Invoice.objects.none()

    return render(request, 'payments/partials/unpaid_invoices.html', {
        'contact': contact,
        'unpaid_invoices': unpaid_invoices,
        'direction': direction,
    })


# ============================================================
# PAYMENT CREATE (with multi-invoice allocation)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:payment_list', htmx_template='payments/payment_form.html')
def payment_create(request, pk=None):
    """
    Create a new payment.

    Behavior:
    - Pre-fills contact when ?contact=<id> is provided.
    - Pre-fills from invoice when ?invoice=<id> or <pk> is provided.
    - Supports multi-invoice allocation via `allocation_amount_<invoice_id>` POST fields.
    - If is_advance=True, NO allocations are created (kept as advance).
    - If payment.amount > sum of allocations, remaining is treated as unallocated (advance-like).
    """
    initial = {}
    contact_id = request.GET.get('contact')
    invoice_id = request.GET.get('invoice')

    if pk is not None and not invoice_id:
        invoice_id = pk

    # ---- Pre-fill contact from ?contact=<id> ----
    if contact_id:
        try:
            contact = Contact.objects.get(pk=contact_id)
            initial['contact'] = contact.id
            if contact.contact_type in ('customer', 'both'):
                initial['direction'] = 'received'
            else:
                initial['direction'] = 'paid'
        except (Contact.DoesNotExist, ValueError):
            pass

    # ---- Pre-fill from invoice context ----
    target_invoice = None
    if invoice_id:
        try:
            target_invoice = Invoice.objects.select_related('customer').get(pk=invoice_id)
            initial['contact'] = target_invoice.customer.id
            initial['direction'] = 'received'
            initial['amount'] = target_invoice.balance_due
        except (Invoice.DoesNotExist, ValueError):
            pass

    # ============================================================
    # POST HANDLER
    # ============================================================
    if request.method == 'POST':
        form = PaymentForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                payment = form.save()

                # ---------------------------------------------------
                # Allocations (only for non-advance payments)
                # ---------------------------------------------------
                allocations_made = []

                if not payment.is_advance:
                    # 1) Auto-allocate to target invoice (from ?invoice=)
                    if target_invoice and target_invoice.customer_id == payment.contact_id:
                        if not PaymentAllocation.objects.filter(
                            payment=payment, invoice=target_invoice
                        ).exists():
                            alloc_amt = min(payment.amount, target_invoice.balance_due)
                            if alloc_amt > 0:
                                PaymentAllocation.objects.create(
                                    payment=payment,
                                    invoice=target_invoice,
                                    amount=alloc_amt,
                                )
                                allocations_made.append(
                                    f"{target_invoice.invoice_number} Rs.{alloc_amt}"
                                )

                    # 2) Multi-invoice allocation from POST
                    #    POST fields: allocation_amount_<invoice_id> = "123.45"
                    for key, value in request.POST.items():
                        if not key.startswith('allocation_amount_'):
                            continue
                        if not value or not value.strip():
                            continue

                        try:
                            inv_id = int(key.replace('allocation_amount_', ''))
                            amt = Decimal(value.strip())
                        except (ValueError, InvalidOperation):
                            continue

                        if amt <= 0:
                            continue

                        try:
                            inv = Invoice.objects.get(pk=inv_id, customer=payment.contact)
                        except Invoice.DoesNotExist:
                            continue

                        # Skip if already allocated
                        if PaymentAllocation.objects.filter(payment=payment, invoice=inv).exists():
                            continue

                        alloc_amt = min(amt, inv.balance_due)
                        if alloc_amt <= 0:
                            continue

                        PaymentAllocation.objects.create(
                            payment=payment,
                            invoice=inv,
                            amount=alloc_amt,
                        )
                        allocations_made.append(f"{inv.invoice_number} Rs.{alloc_amt}")

                    # 3) Update invoice statuses once
                    if allocations_made:
                        payment.update_invoices()

                logger.info(
                    f"Payment #{payment.id} created by {request.user.username} "
                    f"(Rs.{payment.amount} {payment.direction}). "
                    f"Allocations: {allocations_made or 'None'}"
                )

                # ---------------------------------------------------
                # Notifications
                # ---------------------------------------------------
                try:
                    if payment.direction == 'received' and payment.contact:
                        send_notification_to_customer(
                            payment.contact,
                            title=f"Payment Received: Rs.{payment.amount}",
                            message=f"Your payment of Rs.{payment.amount} has been recorded.",
                            link=reverse('customer:customer_payments'),
                            notif_type='success',
                            category='payment',
                            send_email=False,
                        )

                    send_notification_to_staff(
                        title=f"New Payment #{payment.id}",
                        message=(
                            f"{payment.get_direction_display()} of "
                            f"Rs.{payment.amount} with {payment.contact.name}"
                        ),
                        link=reverse('accounting:payment_list'),
                        notif_type='info',
                        category='payment',
                        send_email=False,
                    )

                    for staff in User.objects.filter(is_staff=True):
                        send_notification_sse(staff)
                except Exception as notif_error:
                    logger.error(
                        f"Payment notification failed for #{payment.id}: {notif_error}",
                        exc_info=True,
                    )

                # ---------------------------------------------------
                # Response
                # ---------------------------------------------------
                if is_htmx(request):
                    context = get_paginated_payments_context(request)
                    response = render(request, 'payments/partials/payment_table.html', context)
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {
                            'level': 'success',
                            'message': f'Payment Rs.{payment.amount} recorded successfully.',
                        },
                        'closeModal': '',
                    })
                    return response

                messages.success(request, f"Payment Rs.{payment.amount} recorded successfully.")
                return redirect_to_staff('payment_list')
        else:
            # Invalid form
            if is_htmx(request):
                response = render(request, 'payments/payment_form.html', {
                    'form': form,
                    'payment': None,
                    'target_invoice': target_invoice,
                })
                response['HX-Retarget'] = '#mainModalContent'
                return response

    else:
        form = PaymentForm(initial=initial)

    # ============================================================
    # GET: Render form
    # ============================================================
    # Load unpaid invoices for the pre-selected contact (if any)
    unpaid_invoices = Invoice.objects.none()
    pre_contact = None

    initial_contact_id = form.initial.get('contact')
    if initial_contact_id:
        try:
            pre_contact = Contact.objects.get(pk=initial_contact_id)
            if form.initial.get('direction') == 'received':
                unpaid_invoices = get_unpaid_invoices_for_contact(pre_contact, 'received')
        except (Contact.DoesNotExist, ValueError):
            pass

    context = {
        'form': form,
        'payment': None,
        'target_invoice': target_invoice,
        'unpaid_invoices': unpaid_invoices,
        'pre_contact': pre_contact,
    }
    return render(request, 'payments/payment_form.html', context)


# ============================================================
# PAYMENT UPDATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:payment_list', htmx_template='payments/payment_form.html')
def payment_update(request, pk):
    """Update an existing payment."""
    payment = get_object_or_404(Payment, pk=pk)

    if request.method == 'POST':
        form = PaymentForm(request.POST, instance=payment)
        if form.is_valid():
            with transaction.atomic():
                # Save payment (this triggers model-level ledger sync)
                payment = form.save()

                # Handle allocation changes ONLY if amount changed
                # (For simplicity: recompute allocations to match new amount)
                # Delete old allocations and re-create from POST if provided
                if not payment.is_advance and 'reset_allocations' in request.POST:
                    payment.allocations.all().delete()

                    for key, value in request.POST.items():
                        if not key.startswith('allocation_amount_'):
                            continue
                        if not value or not value.strip():
                            continue

                        try:
                            inv_id = int(key.replace('allocation_amount_', ''))
                            amt = Decimal(value.strip())
                        except (ValueError, InvalidOperation):
                            continue

                        if amt <= 0:
                            continue

                        try:
                            inv = Invoice.objects.get(pk=inv_id, customer=payment.contact)
                        except Invoice.DoesNotExist:
                            continue

                        alloc_amt = min(amt, inv.balance_due)
                        if alloc_amt > 0:
                            PaymentAllocation.objects.create(
                                payment=payment,
                                invoice=inv,
                                amount=alloc_amt,
                            )

                    payment.update_invoices()

                logger.info(f"Payment #{pk} updated by {request.user.username}")

                # Notifications
                try:
                    if payment.direction == 'received' and payment.contact:
                        send_notification_to_customer(
                            payment.contact,
                            title=f"Payment Updated: Rs.{payment.amount}",
                            message=f"Your payment of Rs.{payment.amount} has been updated.",
                            link=reverse('customer:customer_payments'),
                            notif_type='info',
                            category='payment',
                            send_email=False,
                        )
                    for staff in User.objects.filter(is_staff=True):
                        send_notification_sse(staff)
                except Exception as notif_error:
                    logger.error(
                        f"Payment update notification failed for #{pk}: {notif_error}",
                        exc_info=True,
                    )

                if is_htmx(request):
                    context = get_paginated_payments_context(request)
                    response = render(request, 'payments/partials/payment_table.html', context)
                    response['HX-Trigger'] = json.dumps({
                        'showToast': {'level': 'success', 'message': 'Payment updated successfully.'},
                        'closeModal': '',
                    })
                    return response

                messages.success(request, "Payment updated successfully.")
                return redirect_to_staff('payment_list')
        else:
            if is_htmx(request):
                response = render(request, 'payments/payment_form.html', {
                    'form': form,
                    'payment': payment,
                })
                response['HX-Retarget'] = '#mainModalContent'
                return response

    else:
        form = PaymentForm(instance=payment)

    unpaid_invoices = Invoice.objects.none()
    if payment.contact and payment.direction == 'received':
        allocated_invoice_ids = list(
            payment.allocations.values_list('invoice_id', flat=True)
        )
        unpaid_invoices = Invoice.objects.filter(
            customer=payment.contact,
        ).filter(
            Q(balance_due__gt=0) | Q(pk__in=allocated_invoice_ids)
        ).exclude(
            payment_status='paid',
            pk__in=[  
                i for i in Invoice.objects.filter(
                    customer=payment.contact,
                    payment_status='paid',
                ).values_list('pk', flat=True)
                if i not in allocated_invoice_ids
            ]
        ).order_by('date', 'id')

    # Also include already-allocated invoices (for display)
    existing_allocations = payment.allocations.select_related('invoice').all()
    allocation_map = {a.invoice_id: a.amount for a in existing_allocations}

    context = {
        'form': form,
        'payment': payment,
        'target_invoice': None,
        'unpaid_invoices': unpaid_invoices,
        'allocation_map': allocation_map,
        'pre_contact': payment.contact,
    }
    return render(request, 'payments/payment_form.html', context)


# ============================================================
# PAYMENT DELETE (soft delete with cleanup)
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:payment_list')
def payment_delete(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    contact = payment.contact
    amount = payment.amount
    direction = payment.direction

    payment.delete()  # Soft delete + ledger/bank cleanup happens in model
    logger.info(f"Payment #{pk} deleted by {request.user.username}")

    try:
        send_notification_to_staff(
            title=f"Payment Deleted: Rs.{amount}",
            message=f"{direction} payment of Rs.{amount} from {contact.name if contact else 'Unknown'} was deleted.",
            link=reverse('accounting:payment_list'),
            notif_type='warning',
            category='payment',
            send_email=False,
        )
        for staff in User.objects.filter(is_staff=True):
            send_notification_sse(staff)
    except Exception as notif_error:
        logger.error(f"Payment deletion notification failed: {notif_error}", exc_info=True)

    if is_htmx(request):
        context = get_paginated_payments_context(request)
        response = render(request, 'payments/partials/payment_table.html', context)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': 'Payment deleted successfully.'}
        })
        return response

    messages.success(request, "Payment deleted.")
    return redirect_to_staff('payment_list')


# ============================================================
# PAYMENT RECONCILIATION TOGGLE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:payment_list')
def reconcile_payment(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    payment.reconciled = not payment.reconciled
    payment.save()  # Model's update_bank_transaction() syncs BankTransaction.reconciled

    logger.info(
        f"Payment #{pk} reconciliation toggled to {payment.reconciled} "
        f"by {request.user.username}"
    )

    try:
        for staff in User.objects.filter(is_staff=True):
            send_notification_sse(staff)
    except Exception as notif_error:
        logger.error(f"Reconciliation notification failed: {notif_error}", exc_info=True)

    if is_htmx(request):
        context = get_paginated_payments_context(request)
        response = render(request, 'payments/partials/payment_table.html', context)
        response['HX-Trigger'] = json.dumps({
            'showToast': {
                'level': 'info',
                'message': f"Payment #{payment.id} reconciliation updated."
            }
        })
        return response

    messages.success(request, "Reconciliation status updated.")
    return redirect_to_staff('payment_list')