# accounting/views/cash.py
"""Cash Book — day register of the shop cash account (1001) + contra entries."""

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST
from django.utils import timezone

from ..models import BankAccount, BankTransaction, LedgerLine, get_cash_in_hand
from ..forms import ContraForm
from ..decorators import handle_errors

logger = logging.getLogger(__name__)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


@login_required
def cash_book(request):
    """Register of every cash in / cash out movement with running balance."""
    today = timezone.localdate()
    date_from = _parse_date(request.GET.get('date_from')) or today.replace(day=1)
    date_to = _parse_date(request.GET.get('date_to')) or today
    if date_from > date_to:
        date_from, date_to = date_to, date_from

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
    page_obj = paginator.get_page(request.GET.get('page'))

    context = {
        'date_from': date_from.strftime('%Y-%m-%d'),
        'date_to': date_to.strftime('%Y-%m-%d'),
        'opening_balance': opening,
        'total_in': total_in,
        'total_out': total_out,
        'closing_balance': running,
        'cash_in_hand_now': get_cash_in_hand(),
        'rows': page_obj,
        'page_obj': page_obj,
        'contra_form': ContraForm(initial={'date': today}),
        'bank_accounts': BankAccount.objects.filter(is_active=True),
    }
    return render(request, 'cash/cash_book.html', context)


@login_required
@csrf_protect
@require_POST
@handle_errors(default_redirect='accounting:cash_book')
def contra_create(request):
    """Record a cash ⇄ bank movement (contra entry)."""
    form = ContraForm(request.POST)
    if not form.is_valid():
        for field, error in form.errors.items():
            messages.error(request, str(error[0]))
        return redirect('accounting:cash_book')

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

    messages.success(
        request,
        f"₹{cd['amount']} {'deposited into' if is_deposit else 'withdrawn from'} "
        f"{cd['bank_account'].name} recorded in the Cash Book."
    )
    return redirect('accounting:cash_book')
