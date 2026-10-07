# accounting/views/payments.py

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
)

logger = logging.getLogger(__name__)


# ============================================================
# HELPERS
# ============================================================
def get_unpaid_invoices_for_contact(contact, direction='received'):
    """
    Return unpaid or partially-paid invoices for a customer contact.
    Only meaningful for direction='received'.
    """
    if direction == 'received' and contact and contact.contact_type in ('customer', 'both'):
        return (
            Invoice.objects
            .filter(customer=contact, balance_due__gt=0)
            .exclude(payment_status='paid')
            .order_by('date', 'id')
        )
    return Invoice.objects.none()


def _safe_decimal(value, default=Decimal('0')):
    if value is None:
        return default
    s = str(value).strip()
    if s == '':
        return default
    try:
        return Decimal(s)
    except (ValueError, InvalidOperation, TypeError):
        return default


def _safe_next_url(request):
    """Internal-path redirect target from ?next= (blocks off-site redirects)."""
    candidate = request.POST.get('next') or request.GET.get('next')
    if candidate and candidate.startswith('/') and not candidate.startswith('//'):
        return candidate
    return None


def _get_paginated_payments_context(request, queryset=None):
    """Shared filter + paginate + annotate for payments list."""
    if queryset is None:
        queryset = (
            Payment.objects
            .select_related('contact', 'bank_account')
            .prefetch_related('allocations__invoice')
            .all()
            .order_by('-date', '-id')
        )

    search = request.GET.get('search', '').strip()
    direction = request.GET.get('direction', '')
    method = request.GET.get('method', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    page_number = request.GET.get('page', 1)

    # ── Filters ──
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

    # ── Totals (computed on filtered queryset) ──
    # Note: `.distinct()` + `.aggregate()` is safe here because distinct
    # collapses duplicate rows from JOIN, and SUM runs over the deduped set.
    total_received = queryset.filter(direction='received').aggregate(
        t=Sum('amount')
    )['t'] or Decimal('0')
    total_paid = queryset.filter(direction='paid').aggregate(
        t=Sum('amount')
    )['t'] or Decimal('0')
    total_amount = queryset.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    upi_count = queryset.filter(method='upi').count()
    pending_reconciliation = queryset.filter(reconciled=False).count()

    # ── Pagination ──
    paginator = Paginator(queryset, 15)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return {
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


def _collect_allocations_from_post(request, payment, existing_invoice_ids=None):
    """
    Parse `allocation_amount_<invoice_id>` fields from POST and create
    PaymentAllocation rows. Returns list of description strings.

    `existing_invoice_ids` — set of invoice ids already allocated
                             (used to skip duplicates when pre-allocated).
    """
    made = []
    skip_ids = set(existing_invoice_ids or [])
    contact = payment.contact

    for key, value in request.POST.items():
        if not key.startswith('allocation_amount_'):
            continue
        if not value or not value.strip():
            continue

        try:
            inv_id = int(key.replace('allocation_amount_', ''))
            amt = _safe_decimal(value, default=Decimal('0'))
        except (ValueError, TypeError):
            continue

        if amt <= 0:
            continue

        try:
            inv = Invoice.objects.get(pk=inv_id, customer=contact)
        except Invoice.DoesNotExist:
            continue

        if inv.pk in skip_ids:
            continue

        alloc_amt = min(amt, inv.balance_due)
        if alloc_amt <= 0:
            continue

        PaymentAllocation.objects.create(
            payment=payment,
            invoice=inv,
            amount=alloc_amt,
        )
        made.append(f"{inv.invoice_number} Rs.{alloc_amt}")
        skip_ids.add(inv.pk)

    return made


# ============================================================
# LIST
# ============================================================
@handle_errors(default_redirect='accounting:payment_list')
def payment_list(request):
    context = _get_paginated_payments_context(request)
    if is_htmx(request):
        return render(request, 'payments/partials/payment_table.html', context)
    return render(request, 'payments/payment_list.html', context)


# ============================================================
# LOAD UNPAID INVOICES (HTMX endpoint)
# ============================================================
@require_http_methods(["GET"])
def load_unpaid_invoices(request):
    """Return partial HTML with unpaid invoices for a contact."""
    contact_id = request.GET.get('contact', '').strip()
    direction = request.GET.get('direction', 'received').strip()

    if not contact_id:
        return HttpResponse('')

    try:
        contact = Contact.objects.get(pk=contact_id)
    except (Contact.DoesNotExist, ValueError, TypeError):
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
# CREATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:payment_list',
               htmx_template='payments/payment_form.html')
def payment_create(request, pk=None):
    """
    Create a new payment.

    Prefill support:
      - ?contact=<id>  → prefill contact, set direction from contact type
      - ?invoice=<id>  → prefill contact + amount, auto-allocate on save

    Multi-invoice allocation via POST fields:
      - allocation_amount_<invoice_id> = "123.45"
    """
    initial = {}
    contact_id = request.GET.get('contact')
    invoice_id = request.GET.get('invoice')
    if pk is not None and not invoice_id:
        invoice_id = pk

    # Prefill from contact query param
    if contact_id:
        try:
            c = Contact.objects.get(pk=contact_id)
            initial['contact'] = c.id
            initial['direction'] = 'received' if c.contact_type in ('customer', 'both') else 'paid'
        except (Contact.DoesNotExist, ValueError, TypeError):
            pass

    # Prefill from invoice
    target_invoice = None
    if invoice_id:
        try:
            target_invoice = Invoice.objects.select_related('customer').get(pk=invoice_id)
            initial['contact'] = target_invoice.customer_id
            initial['direction'] = 'received'
            initial['amount'] = target_invoice.balance_due
        except (Invoice.DoesNotExist, ValueError, TypeError):
            pass

    # ============================================================
    # POST
    # ============================================================
    if request.method == 'POST':
        form = PaymentForm(request.POST)
        if form.is_valid():
            allocation_summary = []

            # ── 1. Save payment atomically ──
            with transaction.atomic():
                payment = form.save()

                if not payment.is_advance:
                    # Auto-allocate to target invoice (from ?invoice=)
                    if target_invoice and target_invoice.customer_id == payment.contact_id:
                        alloc_amt = min(payment.amount, target_invoice.balance_due)
                        if alloc_amt > 0:
                            PaymentAllocation.objects.create(
                                payment=payment,
                                invoice=target_invoice,
                                amount=alloc_amt,
                            )
                            allocation_summary.append(
                                f"{target_invoice.invoice_number} Rs.{alloc_amt}"
                            )

                    # Multi-invoice allocations from POST
                    allocation_summary += _collect_allocations_from_post(
                        request, payment,
                        existing_invoice_ids=[target_invoice.pk] if target_invoice else [],
                    )

                    if allocation_summary:
                        payment.update_invoices()

            logger.info(
                "Payment #%s created by %s (Rs.%s %s). Allocations: %s",
                payment.pk, request.user.username, payment.amount,
                payment.direction, allocation_summary or 'None',
            )

            # ── 2. Notifications OUTSIDE atomic block ──
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
                    title=f"New Payment #{payment.pk}",
                    message=(
                        f"{payment.get_direction_display()} of "
                        f"Rs.{payment.amount} with {payment.contact.name}"
                    ),
                    link=reverse('accounting:payment_list'),
                    notif_type='info',
                    category='payment',
                    send_email=False,
                )
            except Exception:
                logger.exception("Payment notification failed for #%s", payment.pk)

            # ── 3. Response ──
            if is_htmx(request):
                context = _get_paginated_payments_context(request)
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
            next_url = _safe_next_url(request)
            if next_url:
                return redirect(next_url)
            return redirect_to_staff('payment_list')

        # ── Invalid form — re-render with full context ──
        if is_htmx(request):
            pre_contact = None
            pre_unpaid = Invoice.objects.none()
            try:
                pre_contact = form.data.get('contact') and Contact.objects.get(pk=form.data.get('contact'))
                if pre_contact and form.data.get('direction') == 'received':
                    pre_unpaid = get_unpaid_invoices_for_contact(pre_contact, 'received')
            except (Contact.DoesNotExist, ValueError, TypeError):
                pass

            response = render(request, 'payments/payment_form.html', {
                'form': form,
                'payment': None,
                'target_invoice': target_invoice,
                'pre_contact': pre_contact,
                'unpaid_invoices': pre_unpaid,
            })
            response['HX-Retarget'] = '#mainModalContent'
            return response

        # Non-HTMX (native submit from pages without #payment-table-container):
        # bounce back to the caller instead of rendering a bare partial page.
        first_error = next(iter(e[0] for e in form.errors.values() if e), None)
        messages.error(request, first_error or "Please correct the payment details.")
        next_url = _safe_next_url(request)
        if next_url:
            return redirect(next_url)
        return redirect_to_staff('payment_list')

    else:
        form = PaymentForm(initial=initial)

    # ============================================================
    # GET render
    # ============================================================
    unpaid_invoices = Invoice.objects.none()
    pre_contact = None

    initial_contact_id = form.initial.get('contact')
    if initial_contact_id:
        try:
            pre_contact = Contact.objects.get(pk=initial_contact_id)
            if form.initial.get('direction') == 'received':
                unpaid_invoices = get_unpaid_invoices_for_contact(pre_contact, 'received')
        except (Contact.DoesNotExist, ValueError, TypeError):
            pass

    return render(request, 'payments/payment_form.html', {
        'form': form,
        'payment': None,
        'target_invoice': target_invoice,
        'unpaid_invoices': unpaid_invoices,
        'pre_contact': pre_contact,
    })


# ============================================================
# UPDATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:payment_list',
               htmx_template='payments/payment_form.html')
def payment_update(request, pk):
    """Update an existing payment (with optional allocation reset)."""
    payment = get_object_or_404(Payment, pk=pk)

    if request.method == 'POST':
        form = PaymentForm(request.POST, instance=payment)
        if form.is_valid():
            with transaction.atomic():
                payment = form.save()

                # Reset allocations ONLY if flag was explicitly set
                # (form JS sets it to '1' on submit)
                reset_flag = request.POST.get('reset_allocations', '0') == '1'
                if not payment.is_advance and reset_flag:
                    # HARD delete — unique constraint (payment, invoice) does
                    # NOT respect soft-delete; soft rows would block re-creation.
                    for alloc in list(payment.allocations.all()):
                        alloc.hard_delete()

                    _collect_allocations_from_post(request, payment)
                    payment.update_invoices()

            logger.info("Payment #%s updated by %s", pk, request.user.username)

            # Notifications OUTSIDE atomic
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
            except Exception:
                logger.exception("Payment update notification failed for #%s", pk)

            if is_htmx(request):
                context = _get_paginated_payments_context(request)
                response = render(request, 'payments/partials/payment_table.html', context)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': 'Payment updated successfully.'},
                    'closeModal': '',
                })
                return response

            messages.success(request, "Payment updated successfully.")
            return redirect_to_staff('payment_list')

        # Invalid form — re-render with full context
        if is_htmx(request):
            unpaid_invoices = _unpaid_invoices_for_edit(payment)
            response = render(request, 'payments/payment_form.html', {
                'form': form,
                'payment': payment,
                'unpaid_invoices': unpaid_invoices,
                'pre_contact': payment.contact,
            })
            response['HX-Retarget'] = '#mainModalContent'
            return response

    else:
        form = PaymentForm(instance=payment)

    # GET: render edit form
    unpaid_invoices = _unpaid_invoices_for_edit(payment)

    return render(request, 'payments/payment_form.html', {
        'form': form,
        'payment': payment,
        'target_invoice': None,
        'unpaid_invoices': unpaid_invoices,
        'pre_contact': payment.contact,
    })


def _unpaid_invoices_for_edit(payment):
    """
    Return invoices for the edit form:
      - unpaid/partial invoices for the customer, PLUS
      - already-allocated invoices (even if fully paid)
    """
    if not payment.contact or payment.direction != 'received':
        return Invoice.objects.none()

    allocated_ids = list(payment.allocations.values_list('invoice_id', flat=True))

    return (
        Invoice.objects
        .filter(customer=payment.contact)
        .filter(Q(balance_due__gt=0) | Q(pk__in=allocated_ids))
        .order_by('date', 'id')
    )


# ============================================================
# DELETE
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:payment_list')
def payment_delete(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    contact = payment.contact
    amount = payment.amount
    direction = payment.direction

    payment.delete()  # model-level cleanup
    logger.info("Payment #%s deleted by %s", pk, request.user.username)

    # Notify staff AFTER delete
    try:
        send_notification_to_staff(
            title=f"Payment Deleted: Rs.{amount}",
            message=(
                f"{direction} payment of Rs.{amount} from "
                f"{contact.name if contact else 'Unknown'} was deleted."
            ),
            link=reverse('accounting:payment_list'),
            notif_type='warning',
            category='payment',
            send_email=False,
        )
    except Exception:
        logger.exception("Payment deletion notification failed for #%s", pk)

    if is_htmx(request):
        context = _get_paginated_payments_context(request)
        response = render(request, 'payments/partials/payment_table.html', context)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': 'Payment deleted successfully.'}
        })
        return response

    messages.success(request, "Payment deleted.")
    return redirect_to_staff('payment_list')


# ============================================================
# RECONCILE TOGGLE — LIGHTWEIGHT
# ============================================================
@csrf_protect
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:payment_list')
def reconcile_payment(request, pk):
    """
    Toggle reconciliation status — LIGHTWEIGHT.

    Uses queryset.update() + manual bank txn sync to avoid rebuilding
    ledger entries / allocations / contact balances. Previously this
    called payment.save() which rebuilt everything on every toggle.
    """
    payment = get_object_or_404(Payment, pk=pk)

    # Accept explicit target state if provided (avoids double-toggle races)
    target = request.POST.get('reconciled')
    if target in ('true', '1', 'on'):
        new_value = True
    elif target in ('false', '0', 'off'):
        new_value = False
    else:
        new_value = not payment.reconciled

    # Fast DB update — no signals, no ledger rebuild
    Payment.objects.filter(pk=pk).update(reconciled=new_value)

    # Mirror to bank transaction (lightweight single-row update)
    BankTransaction.objects.filter(payment_id=pk).update(reconciled=new_value)

    logger.info(
        "Payment #%s reconciliation toggled to %s by %s",
        pk, new_value, request.user.username,
    )

    if is_htmx(request):
        context = _get_paginated_payments_context(request)
        response = render(request, 'payments/partials/payment_table.html', context)
        response['HX-Trigger'] = json.dumps({
            'showToast': {
                'level': 'info',
                'message': f"Payment #{payment.pk} reconciliation updated.",
            }
        })
        return response

    messages.success(request, "Reconciliation status updated.")
    return redirect_to_staff('payment_list')