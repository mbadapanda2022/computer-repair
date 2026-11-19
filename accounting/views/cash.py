# accounting/views/cash.py
"""Cash Book — day register of the shop cash account (1001) + contra entries."""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST
from django.utils import timezone

from ..models import BankAccount, BankTransaction, LedgerLine, get_cash_in_hand
from ..forms import ContraForm
from ..decorators import handle_errors
from .utils import is_htmx, htmx_response

logger = logging.getLogger(__name__)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _resolve_range(source):
    """Read date_from/date_to from GET or POST and normalise the range."""
    today = timezone.localdate()
    date_from = _parse_date(source.get('date_from')) or today.replace(day=1)
    date_to = _parse_date(source.get('date_to')) or today
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    return date_from, date_to


def _cash_book_context(request, date_from, date_to, page_number=None, contra_form=None):
    """Build the full register context, shared by the page and HTMX swaps."""
    opening = get_cash_in_hand(up_to=date_from - timedelta(days=1))

    lines = (
        LedgerLine.objects
        .filter(
            account__code='1001',
            ledger_entry__is_deleted=False,
            ledger_entry__date__gte=date_from,
            ledger_entry__date__lte=date_to,
        )
        .select_related('ledger_entry', 'ledger_entry__bank_transaction')
        .order_by('ledger_entry__date', 'ledger_entry__id', 'id')
    )

    rows = []
    running = opening
    total_in = Decimal('0')
    total_out = Decimal('0')
    for line in lines:
        entry = line.ledger_entry
        txn = getattr(entry, 'bank_transaction', None)
        if txn and txn.source_type == 'transfer':
            label = 'Contra (Cash ⇄ Bank)'
        else:
            label = entry.get_entry_type_display()
        debit = line.debit or Decimal('0')
        credit = line.credit or Decimal('0')
        running += debit - credit
        total_in += debit
        total_out += credit
        rows.append({
            'date': entry.date,
            'particulars': entry.description or '—',
            'label': label,
            'cash_in': debit,
            'cash_out': credit,
            'balance': running,
        })

    paginator = Paginator(rows, 100)
    page_obj = paginator.get_page(page_number)

    bank_accounts = list(BankAccount.objects.filter(is_active=True))
    bank_balances = {b.pk: b.current_balance for b in bank_accounts}

    if contra_form is None:
        contra_form = ContraForm(initial={'date': timezone.localdate()})

    return {
        'date_from': date_from.strftime('%Y-%m-%d'),
        'date_to': date_to.strftime('%Y-%m-%d'),
        'opening_balance': opening,
        'total_in': total_in,
        'total_out': total_out,
        'closing_balance': running,
        'cash_in_hand_now': get_cash_in_hand(),
        'rows': page_obj,
        'page_obj': page_obj,
        'contra_form': contra_form,
        'bank_accounts': bank_accounts,
        'bank_balances': bank_balances,
    }


@login_required
def cash_book(request):
    """Register of every cash in / cash out movement with running balance."""
    date_from, date_to = _resolve_range(request.GET)
    context = _cash_book_context(
        request, date_from, date_to, page_number=request.GET.get('page'),
    )
    return render(request, 'cash/cash_book.html', context)


@login_required
@csrf_protect
@require_POST
@handle_errors(default_redirect='accounting:cash_book')
def contra_create(request):
    """Record a cash ⇄ bank movement (contra entry)."""
    date_from, date_to = _resolve_range(request.POST)
    page_number = request.POST.get('page')

    back_params = urlencode({
        'date_from': date_from.strftime('%Y-%m-%d'),
        'date_to': date_to.strftime('%Y-%m-%d'),
    })
    cash_book_url = f"{reverse('accounting:cash_book')}?{back_params}"

    form = ContraForm(request.POST)
    if not form.is_valid():
        if is_htmx(request):
            context = _cash_book_context(
                request, date_from, date_to,
                page_number=page_number, contra_form=form,
            )
            return htmx_response(
                request,
                'cash/partials/cash_book_content.html',
                context=context,
                toast={
                    'level': 'danger',
                    'message': 'Movement not recorded — please fix the highlighted fields.',
                },
            )
        for field, error in form.errors.items():
            messages.error(request, str(error[0]))
        return redirect(cash_book_url)

    cd = form.cleaned_data
    is_deposit = cd['direction'] == 'deposit'
    description = cd['notes'] or (
        f"Cash deposited into {cd['bank_account'].name}" if is_deposit
        else f"Cash withdrawn from {cd['bank_account'].name}"
    )

    with transaction.atomic():
        BankTransaction.objects.create(
            bank_account=cd['bank_account'],
            transaction_type='deposit' if is_deposit else 'withdrawal',
            source_type='transfer',
            amount=cd['amount'],
            date=cd['date'],
            description=description,
        )

    message = (
        f"₹{cd['amount']} {'deposited into' if is_deposit else 'withdrawn from'} "
        f"{cd['bank_account'].name} recorded in the Cash Book."
    )

    if is_htmx(request):
        # Keep the new movement visible even if it falls outside the
        # date range the user was viewing.
        if cd['date'] < date_from:
            date_from = cd['date']
        if cd['date'] > date_to:
            date_to = cd['date']
        context = _cash_book_context(
            request, date_from, date_to, page_number=page_number,
            contra_form=ContraForm(initial={'date': cd['date']}),
        )
        return htmx_response(
            request,
            'cash/partials/cash_book_content.html',
            context=context,
            toast={'level': 'success', 'message': message},
        )

    messages.success(request, message)
    return redirect(cash_book_url)
