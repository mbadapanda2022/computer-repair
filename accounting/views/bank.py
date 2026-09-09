# accounting/views/bank.py

import logging
from decimal import Decimal
import json
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.utils import timezone

from ..models import BankAccount, BankTransaction
from ..forms import BankAccountForm, BankTransactionForm
from .utils import is_htmx, htmx_response, redirect_to_staff
from ..decorators import handle_errors

logger = logging.getLogger(__name__)


# =============================================================
# HELPER: RECALCULATE ACCOUNT BALANCE
# =============================================================
def recalculate_account_balance(account):
    """
    Recalculate and update current_balance for a bank account
    based on opening_balance and all transactions.
    """
    total_deposits = account.transactions.filter(
        transaction_type='deposit'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

    total_withdrawals = account.transactions.filter(
        transaction_type='withdrawal'
    ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

    account.current_balance = (
        account.opening_balance + total_deposits - total_withdrawals
    ).quantize(Decimal('0.01'))
    account.save(update_fields=['current_balance'])
    return account.current_balance


# =============================================================
# BANK ACCOUNT LIST
# =============================================================
def bank_account_list(request):
    accounts = BankAccount.objects.all().order_by('name')
    context = {'accounts': accounts}

    if is_htmx(request):
        return render(request, 'bank/partials/bank_account_table.html', context)
    return render(request, 'bank/bank_account_list.html', context)


# =============================================================
# BANK ACCOUNT CREATE
# =============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:bank_account_list', htmx_template='bank/partials/bank_account_form.html')
def bank_account_add(request):
    if request.method == 'POST':
        form = BankAccountForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                account = form.save()
                # For new account, current balance = opening balance
                account.current_balance = account.opening_balance
                account.save(update_fields=['current_balance'])

            logger.info(f"Bank account created: {account.name} by {request.user.username}")

            if is_htmx(request):
                return htmx_response(
                    request,
                    'bank/partials/bank_account_table.html',
                    context={'accounts': BankAccount.objects.all().order_by('name')},
                    toast={'level': 'success', 'message': f'Bank account "{account.name}" created.'},
                    close_modal=True
                )
            messages.success(request, f'Bank account "{account.name}" created successfully.')
            return redirect_to_staff('bank_account_list')
        else:
            if is_htmx(request):
                return render(request, 'bank/partials/bank_account_form.html', {'form': form})
    else:
        form = BankAccountForm()

    context = {'form': form}
    if is_htmx(request):
        return render(request, 'bank/partials/bank_account_form.html', context)
    return render(request, 'bank/bank_account_form.html', context)


# =============================================================
# BANK ACCOUNT EDIT (FIXED: removed update_balance() call)
# =============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:bank_account_list', htmx_template='bank/partials/bank_account_form.html')
def bank_account_edit(request, pk):
    account = get_object_or_404(BankAccount, pk=pk)

    if request.method == 'POST':
        form = BankAccountForm(request.POST, instance=account)
        if form.is_valid():
            with transaction.atomic():
                form.save()
                # Recalculate balance after saving (in case opening_balance changed)
                recalculate_account_balance(account)

            logger.info(f"Bank account updated: {account.name} by {request.user.username}")

            if is_htmx(request):
                return htmx_response(
                    request,
                    'bank/partials/bank_account_table.html',
                    context={'accounts': BankAccount.objects.all().order_by('name')},
                    toast={'level': 'success', 'message': f'Bank account "{account.name}" updated.'},
                    close_modal=True
                )
            messages.success(request, f'Bank account "{account.name}" updated successfully.')
            return redirect_to_staff('bank_account_list')
        else:
            if is_htmx(request):
                return render(request, 'bank/partials/bank_account_form.html', {'form': form, 'account': account})
    else:
        form = BankAccountForm(instance=account)

    context = {'form': form, 'account': account}
    if is_htmx(request):
        return render(request, 'bank/partials/bank_account_form.html', context)
    return render(request, 'bank/bank_account_form.html', context)


# =============================================================
# BANK ACCOUNT DELETE
# =============================================================
@csrf_protect
@require_http_methods(["DELETE"])
def bank_account_delete(request, pk):
    account = get_object_or_404(BankAccount, pk=pk)

    if account.transactions.exists():
        response = HttpResponse("Cannot delete account with existing transactions.", status=400)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'danger', 'message': f'Cannot delete "{account.name}" – it has transactions.'}
        })
        return response

    try:
        account_name = account.name
        account.delete()
        logger.info(f"Bank account deleted: {account_name} by {request.user.username}")

        accounts = BankAccount.objects.all().order_by('name')
        response = render(request, 'bank/partials/bank_account_table.html', {'accounts': accounts})
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': f'Bank account "{account_name}" deleted.'}
        })
        return response
    except Exception as e:
        logger.error(f"Error deleting bank account {pk}: {e}")
        response = HttpResponse("Failed to delete bank account.", status=500)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'danger', 'message': 'Failed to delete. Please try again.'}
        })
        return response


# =============================================================
# BANK STATEMENT
# =============================================================
def bank_statement(request, pk):
    account = get_object_or_404(BankAccount, pk=pk)
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    transactions = account.transactions.all().order_by('date', 'id')

    if date_from:
        try:
            transactions = transactions.filter(date__gte=date_from)
        except ValueError:
            pass
    if date_to:
        try:
            transactions = transactions.filter(date__lte=date_to)
        except ValueError:
            pass

    opening_balance = account.opening_balance
    if date_from:
        previous_transactions = account.transactions.filter(date__lt=date_from)
        total_deposit = previous_transactions.filter(transaction_type='deposit').aggregate(total=Sum('amount'))['total'] or Decimal('0')
        total_withdrawal = previous_transactions.filter(transaction_type='withdrawal').aggregate(total=Sum('amount'))['total'] or Decimal('0')
        opening_balance = account.opening_balance + total_deposit - total_withdrawal

    running_balance = opening_balance
    statement_lines = []
    for txn in transactions:
        if txn.transaction_type == 'deposit':
            running_balance += txn.amount
            deposit = txn.amount
            withdrawal = Decimal('0')
        else:
            running_balance -= txn.amount
            deposit = Decimal('0')
            withdrawal = txn.amount
        statement_lines.append({
            'date': txn.date,
            'description': txn.description or txn.get_source_type_display(),
            'deposit': deposit,
            'withdrawal': withdrawal,
            'balance': running_balance,
            'reference': txn.reference or '-',
        })

    context = {
        'account': account,
        'statement_lines': statement_lines,
        'opening_balance': opening_balance,
        'closing_balance': running_balance,
        'date_from': date_from,
        'date_to': date_to,
    }
    return render(request, 'bank/bank_statement.html', context)


# =============================================================
# ADD BANK TRANSACTION (FIXED: updates account balance)
# =============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:bank_account_list', htmx_template='bank/partials/bank_transaction_form.html')
def bank_transaction_add(request, account_pk):
    account = get_object_or_404(BankAccount, pk=account_pk)

    if request.method == 'POST':
        form = BankTransactionForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                txn = form.save(commit=False)
                txn.bank_account = account

                # Insufficient balance check (use latest current_balance)
                if txn.transaction_type == 'withdrawal' and txn.amount > account.current_balance:
                    form.add_error('amount', f'Insufficient balance. Available: ₹{account.current_balance}')
                    if is_htmx(request):
                        return render(request, 'bank/partials/bank_transaction_form.html', {
                            'form': form,
                            'account': account
                        })
                    messages.error(request, f'Insufficient balance. Available: ₹{account.current_balance}')
                    return redirect_to_staff('bank_statement', pk=account.pk)

                txn.save()
                logger.info(f"Bank transaction added: {txn.get_transaction_type_display()} ₹{txn.amount} for {account.name} by {request.user.username}")

                # --- CRITICAL FIX: Update account balance after transaction ---
                recalculate_account_balance(account)

                if is_htmx(request):
                    response = HttpResponse()
                    response['HX-Redirect'] = request.META.get('HTTP_REFERER', f'/bank/statement/{account.pk}/')
                    response['HX-Trigger'] = json.dumps({
                        'closeModal': '',
                        'showToast': {
                            'level': 'success',
                            'message': 'Transaction recorded successfully.'
                        }
                    })
                    return response
                messages.success(request, 'Transaction recorded successfully.')
                return redirect_to_staff('bank_statement', pk=account.pk)
        else:
            if is_htmx(request):
                return render(request, 'bank/partials/bank_transaction_form.html', {
                    'form': form,
                    'account': account
                })
    else:
        form = BankTransactionForm(initial={
            'bank_account': account,
            'date': timezone.now().date(),
        })

    context = {'form': form, 'account': account}
    if is_htmx(request):
        return render(request, 'bank/partials/bank_transaction_form.html', context)
    return render(request, 'bank/bank_transaction_form.html', context)