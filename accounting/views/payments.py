# accounting/views/payments.py

import json
import logging
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.contrib import messages
from django.db.models import Q, Sum
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.template.loader import render_to_string
from django.contrib.auth.models import User
from django.urls import reverse

from ..models import Payment, BankTransaction, Contact, Invoice
from ..forms import PaymentForm
from .utils import is_htmx, htmx_response, redirect_to_staff
from ..decorators import handle_errors

# ============================================================
# NOTIFICATION HELPERS IMPORT (ADDED)
# ============================================================
from accounting.utils.notification_helpers import (
    send_notification_to_customer,
    send_notification_to_staff,
    send_notification_sse
)

logger = logging.getLogger(__name__)


# ============================================================
# HELPER: GET PAGINATED PAYMENTS CONTEXT
# ============================================================
def get_paginated_payments_context(request, queryset=None):
    """Shared logic to filter, paginate, and annotate payments."""
    if queryset is None:
        queryset = Payment.objects.select_related('contact', 'bank_account').all().order_by('-date')

    # Filters
    search = request.GET.get('search', '').strip()
    direction = request.GET.get('direction', '')
    method = request.GET.get('method', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    reset = request.GET.get('reset', '')
    page_number = request.GET.get('page', 1)

    if reset:
        search = direction = method = date_from = date_to = ''

    if search:
        queryset = queryset.filter(
            Q(contact__name__icontains=search) |
            Q(reference__icontains=search) |
            Q(upi_ref__icontains=search) |
            Q(description__icontains=search)
        )
    if direction:
        queryset = queryset.filter(direction=direction)
    if method:
        queryset = queryset.filter(method=method)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    # Pagination (15 per page)
    paginator = Paginator(queryset, 15)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Summary stats (from filtered queryset)
    total_received = queryset.filter(direction='received').aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
    total_paid = queryset.filter(direction='paid').aggregate(Sum('amount'))['amount__sum'] or Decimal('0')
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
# PAYMENT LIST (with HTMX)
# ============================================================
@handle_errors(default_redirect='accounting:payment_list')
def payment_list(request):
    """List payments with filters, pagination, and summary stats."""
    context = get_paginated_payments_context(request)
    if is_htmx(request):
        return render(request, 'payments/partials/payment_table.html', context)
    return render(request, 'payments/payment_list.html', context)


# ============================================================
# PAYMENT CREATE (with Notification)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:payment_list', htmx_template='payments/payment_form.html')
def payment_create(request):
    initial = {}
    contact_id = request.GET.get('contact')
    invoice_id = request.GET.get('invoice')

    if contact_id:
        try:
            contact = Contact.objects.get(pk=contact_id)
            initial['contact'] = contact.id
            if contact.contact_type in ['customer', 'both']:
                initial['direction'] = 'received'
            else:
                initial['direction'] = 'paid'
        except Contact.DoesNotExist:
            pass

    if invoice_id:
        try:
            inv = Invoice.objects.get(pk=invoice_id)
            initial['invoices'] = [inv.id]
        except Invoice.DoesNotExist:
            pass

    if request.method == 'POST':
        form = PaymentForm(request.POST)
        if form.is_valid():
            payment = form.save()
            logger.info(f"Payment #{payment.id} created by {request.user.username}")

            # ============================================================
            # 🔔 NOTIFICATION: Customer (if payment received) & Staff SSE
            # ============================================================
            try:
                # 1. Customer Notification (if direction is 'received')
                if payment.direction == 'received' and payment.contact:
                    send_notification_to_customer(
                        payment.contact,
                        title=f"Payment Received: ₹{payment.amount}",
                        message=f"Your payment of ₹{payment.amount} has been recorded.",
                        link=reverse('customer:customer_payments'),
                        notif_type='success',
                        category='payment',
                        send_email=False   # Optional: set True for email
                    )

                # 2. Staff Notification
                send_notification_to_staff(
                    title=f"New Payment #{payment.id}",
                    message=f"{payment.get_direction_display()} of ₹{payment.amount} from {payment.contact.name}",
                    link=reverse('accounting:payment_list'),
                    notif_type='info',
                    category='payment',
                    send_email=False
                )

                # 3. SSE for all staff (real-time badge update)
                for staff in User.objects.filter(is_staff=True):
                    send_notification_sse(staff)

            except Exception as notif_error:
                logger.error(f"🔥 Payment notification failed for #{payment.id}: {notif_error}", exc_info=True)

            # If HTMX, return updated table + close modal
            if is_htmx(request):
                context = get_paginated_payments_context(request)
                response = render(request, 'payments/partials/payment_table.html', context)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': 'Payment created successfully.'},
                    'closeModal': ''
                })
                return response
            messages.success(request, "Payment created successfully.")
            return redirect_to_staff('payment_list')
        else:
            # Invalid form – retarget to modal
            if is_htmx(request):
                response = render(request, 'payments/payment_form.html', {'form': form})
                response['HX-Retarget'] = '#mainModalContent'
                return response
    else:
        form = PaymentForm(initial=initial)

    return render(request, 'payments/payment_form.html', {'form': form})


# ============================================================
# PAYMENT UPDATE (with Notification)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:payment_list', htmx_template='payments/payment_form.html')
def payment_update(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    if request.method == 'POST':
        form = PaymentForm(request.POST, instance=payment)
        if form.is_valid():
            form.save()
            logger.info(f"Payment #{pk} updated by {request.user.username}")

            # ============================================================
            # 🔔 NOTIFICATION: Update customer & staff
            # ============================================================
            try:
                if payment.direction == 'received' and payment.contact:
                    send_notification_to_customer(
                        payment.contact,
                        title=f"Payment Updated: ₹{payment.amount}",
                        message=f"Your payment of ₹{payment.amount} has been updated.",
                        link=reverse('customer:customer_payments'),
                        notif_type='info',
                        category='payment',
                        send_email=False
                    )

                # SSE for all staff
                for staff in User.objects.filter(is_staff=True):
                    send_notification_sse(staff)

            except Exception as notif_error:
                logger.error(f"🔥 Payment update notification failed for #{pk}: {notif_error}", exc_info=True)

            if is_htmx(request):
                context = get_paginated_payments_context(request)
                response = render(request, 'payments/partials/payment_table.html', context)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': 'Payment updated successfully.'},
                    'closeModal': ''
                })
                return response
            messages.success(request, "Payment updated successfully.")
            return redirect_to_staff('payment_list')
        else:
            if is_htmx(request):
                response = render(request, 'payments/payment_form.html', {'form': form, 'payment': payment})
                response['HX-Retarget'] = '#mainModalContent'
                return response
    else:
        form = PaymentForm(instance=payment)
    return render(request, 'payments/payment_form.html', {'form': form, 'payment': payment})


# ============================================================
# PAYMENT DELETE (with Notification)
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:payment_list')
def payment_delete(request, pk):
    payment = get_object_or_404(Payment, pk=pk)

    # Store info before deletion for notification
    contact = payment.contact
    amount = payment.amount
    direction = payment.direction

    payment.delete()
    logger.info(f"Payment #{pk} deleted by {request.user.username}")

    # ============================================================
    # NOTIFICATION: Staff (deletion alert)
    # ============================================================
    try:
        send_notification_to_staff(
            title=f"Payment Deleted: ₹{amount}",
            message=f"{direction} payment of ₹{amount} from {contact.name if contact else 'Unknown'} was deleted.",
            link=reverse('accounting:payment_list'),
            notif_type='warning',
            category='payment',
            send_email=False
        )
        for staff in User.objects.filter(is_staff=True):
            send_notification_sse(staff)
    except Exception as notif_error:
        logger.error(f"🔥 Payment deletion notification failed: {notif_error}", exc_info=True)

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
# PAYMENT RECONCILIATION TOGGLE (with Notification)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:payment_list')
def reconcile_payment(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    payment.reconciled = not payment.reconciled
    payment.save()
    BankTransaction.objects.filter(payment=payment).update(reconciled=payment.reconciled)
    logger.info(f"Payment #{pk} reconciliation toggled to {payment.reconciled} by {request.user.username}")

    # ============================================================
    # 🔔 NOTIFICATION: Staff about reconciliation change
    # ============================================================
    try:
        for staff in User.objects.filter(is_staff=True):
            send_notification_sse(staff)
    except Exception as notif_error:
        logger.error(f"🔥 Reconciliation notification failed: {notif_error}", exc_info=True)

    if is_htmx(request):
        context = get_paginated_payments_context(request)
        response = render(request, 'payments/partials/payment_table.html', context)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'info', 'message': f"Payment #{payment.id} reconciliation updated."}
        })
        return response
    messages.success(request, "Reconciliation status updated.")
    return redirect_to_staff('payment_list')