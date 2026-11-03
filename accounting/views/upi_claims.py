# accounting/views/upi_claims.py
"""
Staff console for the UPI payment claims customers submit from their portal.

A claim is only a customer's word that money was sent. Nothing moves in the
ledger until staff verify it against their own UPI app or bank statement, at
which point an ordinary Payment + PaymentAllocation is created — the exact
path the manual "Record Payment" screen uses, so invoice totals, the payment
status and the bank/cash ledger all stay consistent by construction.
"""

import logging
from urllib.parse import unquote, urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Count, Q, Sum
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from ..decorators import handle_errors
from ..forms import UpiVerifyForm
from ..models import UpiPaymentRequest
from ..templatetags.math_filters import rupee

from accounting.utils.notification_helpers import (
    send_notification_to_customer,
)

logger = logging.getLogger(__name__)

STATUS_TABS = ('pending', 'verified', 'rejected', 'all')
CLAIM_PAGE_SIZE = 25

# Only these list params may be echoed back into a redirect target — a caller
# supplied query string is never used as-is, so it cannot smuggle an
# off-site URL or junk parameters.
_BACK_PARAMS = ('status', 'search', 'page')


def _back_target(request):
    """
    Where to send staff after an action.

    `next` is either an internal page path (an invoice page that hosts the
    claims panel) or the claims list query string. Off-site paths ('//host')
    are rejected so this cannot be turned into an open redirect.
    """
    raw = (request.POST.get('next') or request.GET.get('next') or '').strip()
    if raw.startswith('/') and not raw.startswith('//'):
        return raw

    query = raw.lstrip('?')
    parts = {}
    for chunk in query.split('&'):
        if '=' not in chunk:
            continue
        key, value = chunk.split('=', 1)
        if key in _BACK_PARAMS:
            parts[key] = unquote(value)
    if parts.get('status') not in STATUS_TABS:
        parts.pop('status', None)
    query = ('?' + urlencode(parts)) if parts else ''
    return reverse('accounting:upi_claim_list') + query


def _filtered_claims(request):
    """Apply tab + search filters. Returns (queryset, params)."""
    queryset = (
        UpiPaymentRequest.objects
        .select_related('invoice', 'contact', 'payment', 'submitted_by',
                        'verified_by')
        .order_by('-created_at', '-id')
    )

    params = {
        'status': request.GET.get('status', 'pending').strip(),
        'search': request.GET.get('search', '').strip(),
    }
    if params['status'] not in STATUS_TABS:
        params['status'] = 'pending'
    if params['status'] != 'all':
        queryset = queryset.filter(status=params['status'])

    if params['search']:
        queryset = queryset.filter(
            Q(invoice__invoice_number__icontains=params['search']) |
            Q(contact__name__icontains=params['search']) |
            Q(contact__phone__icontains=params['search']) |
            Q(upi_reference__icontains=params['search'])
        ).distinct()

    return queryset, params


@login_required
@handle_errors(default_redirect='accounting:upi_claim_list')
def upi_claim_list(request):
    """Claims awaiting verification (default tab), plus verified/rejected history."""
    queryset, params = _filtered_claims(request)

    # One grouped query for every tab badge + the pending rupee total.
    stats = {
        row['status']: row
        for row in UpiPaymentRequest.objects.values('status').annotate(
            n=Count('id'), t=Sum('amount'))
    }
    counts = {key: (stats.get(key) or {}).get('n', 0)
              for key in ('pending', 'verified', 'rejected')}
    pending_total = (stats.get('pending') or {}).get('t') or 0

    paginator = Paginator(queryset, CLAIM_PAGE_SIZE)
    try:
        page_obj = paginator.page(request.GET.get('page', 1))
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    active = {k: v for k, v in params.items() if v and k != 'status'}
    context = {
        'claims': page_obj,
        'page_obj': page_obj,
        'counts': counts,
        'pending_total': pending_total,
        'filters_qs': ('&' + urlencode(active)) if active else '',
        **params,
    }
    return render(request, 'sales/upi_claims.html', context)


@login_required
@csrf_protect
@handle_errors(default_redirect='accounting:upi_claim_list')
def upi_claim_verify(request, pk):
    """
    GET  → the confirm-payment modal (HTMX partial).
    POST → book the money: create Payment + allocation against the invoice.

    The amount is editable on purpose — the figure the customer claims and the
    figure that actually landed in the shop's UPI account are not always the
    same, and the ledger must show the real one.
    """
    claim = get_object_or_404(
        UpiPaymentRequest.objects.select_related('invoice', 'contact'), pk=pk)
    back = _back_target(request)

    if request.method == 'POST':
        if claim.status != 'pending':
            messages.warning(request, 'This claim has already been reviewed.')
            return redirect(back)

        form = UpiVerifyForm(request.POST, claim=claim)
        if form.is_valid():
            try:
                payment = claim.record_as_payment(
                    amount=form.cleaned_data['amount'],
                    bank_account=form.cleaned_data['bank_account'],
                    date=form.cleaned_data['paid_on'],
                    user=request.user,
                )
            except ValidationError as exc:
                # The invoice balance can shrink between opening this modal
                # and submitting it; the model re-checks under a row lock, so
                # surface exactly what it said instead of booking anyway.
                logger.warning(
                    "UPI claim %s could not be verified | error=%s", claim.pk, exc)
                for text in exc.messages:
                    form.add_error(None, text)
            else:
                claim.invoice.refresh_from_db()
                try:
                    send_notification_to_customer(
                        claim.contact,
                        title=f'Payment received: ₹{rupee(payment.amount)}',
                        message=(f'Thank you {claim.contact.name} — we have '
                                 f'received your UPI payment of ₹{rupee(payment.amount)} '
                                 f'on {payment.date} against invoice '
                                 f'{claim.invoice.invoice_number}. '
                                 f'{_balance_line(claim.invoice)}'),
                        link=reverse('customer:customer_invoice_detail',
                                     args=[claim.invoice_id]),
                        notif_type='success',
                        category='payment',
                        send_email=True,
                    )
                except Exception:
                    logger.exception(
                        "Customer notification failed for UPI claim %s", claim.pk)

                messages.success(
                    request,
                    f'Payment of ₹{rupee(payment.amount)} recorded against '
                    f'{claim.invoice.invoice_number}.')
                return redirect(back)

        # Validation failed. The modal is only ever opened through HTMX, and
        # its form posts natively, so a browser reload with the reason in the
        # flash message is the honest path for both.
        messages.error(
            request,
            'Payment was not recorded: ' + ' '.join(
                [str(e) for e in form.non_field_errors()]
                + [str(e) for errors in form.errors.values() for e in errors]))
        return redirect(back)

    return render(request, 'sales/_upi_verify_modal.html',
                  _verify_context(claim, UpiVerifyForm(claim=claim), request))


def _verify_context(claim, form, request):
    return {
        'claim': claim,
        'form': form,
        'next': (request.GET.get('next') or request.POST.get('next') or ''),
        'live_due': claim.invoice.balance_due,
    }


def _balance_line(invoice):
    if invoice.balance_due and invoice.balance_due > 0:
        return f'Balance still due on it is ₹{rupee(invoice.balance_due)}.'
    return 'The invoice is now fully paid.'


@login_required
@csrf_protect
@require_POST
@handle_errors(default_redirect='accounting:upi_claim_list')
def upi_claim_reject(request, pk):
    """Turn down a claim that does not match the UPI statement. Books nothing."""
    claim = get_object_or_404(
        UpiPaymentRequest.objects.select_related('invoice', 'contact'), pk=pk)
    back = _back_target(request)
    reason = (request.POST.get('reason') or '').strip()[:200]

    try:
        claim.mark_rejected(reason=reason, user=request.user)
    except ValidationError as exc:
        logger.warning("UPI claim %s could not be rejected | error=%s",
                       claim.pk, exc)
        messages.warning(request, 'This claim has already been reviewed.')
        return redirect(back)

    try:
        send_notification_to_customer(
            claim.contact,
            title='UPI payment not found yet',
            message=(
                f'We have not seen your UPI payment of ₹{rupee(claim.amount)} '
                f'(reference {claim.upi_reference}) against invoice '
                f'{claim.invoice.invoice_number} in our account yet. '
                + (f'Shop note: {reason}' if reason else
                   'Please check the reference once and tell us again, '
                   'or pay at the counter.')
            ),
            link=reverse('customer:customer_upi_pay', args=[claim.invoice_id]),
            notif_type='warning',
            category='payment',
            send_email=True,
        )
    except Exception:
        logger.exception(
            "Customer notification failed for UPI claim %s", claim.pk)

    messages.success(request, f'Claim of ₹{rupee(claim.amount)} rejected.')
    return redirect(back)
