# accounting/utils.py

from decimal import Decimal
from django.db import transaction
from .models import LedgerEntry, LedgerLine, Account
from .models import get_account


def create_journal_lines(entry, contact, amount, journal_type):
    """
    Create ledger lines for a journal entry using proper Account FK.
    Uses get_account() helper for consistent Chart of Accounts.
    """
    # Define account codes (matching the COA in models.py)
    CASH_ACCOUNT = get_account('1001', 'Cash', 'asset', '1')
    CUSTOMER_ACCOUNT = get_account('1011', 'Customer Receivable', 'asset', '1')
    VENDOR_ACCOUNT = get_account('2011', 'Vendor Payable', 'liability', '2')
    ADVANCE_RECEIVED_ACCOUNT = get_account('1012', 'Advance from Customer', 'liability', '2')
    ADVANCE_PAID_ACCOUNT = get_account('1014', 'Advance to Vendor', 'asset', '1')
    DISCOUNT_ALLOWED_ACCOUNT = get_account('5010', 'Discount Allowed', 'expense', '5')
    DISCOUNT_RECEIVED_ACCOUNT = get_account('4011', 'Discount Received', 'income', '4')

    if journal_type == 'discount':
        if contact.contact_type in ('customer', 'both'):
            # Customer ko discount diya → Discount Allowed
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=CUSTOMER_ACCOUNT,
                contact=contact,
                debit=0,
                credit=amount
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=DISCOUNT_ALLOWED_ACCOUNT,
                debit=amount,
                credit=0
            )
        else:
            # Vendor se discount mila → Discount Received
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=VENDOR_ACCOUNT,
                contact=contact,
                debit=amount,
                credit=0
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=DISCOUNT_RECEIVED_ACCOUNT,
                debit=0,
                credit=amount
            )

    elif journal_type == 'advance_received':
        # Customer se advance paisa aaya
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=CASH_ACCOUNT,
            debit=amount,
            credit=0
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=ADVANCE_RECEIVED_ACCOUNT,
            contact=contact,
            debit=0,
            credit=amount
        )

    elif journal_type == 'advance_paid':
        # Vendor ko advance paisa diya
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=ADVANCE_PAID_ACCOUNT,
            contact=contact,
            debit=amount,
            credit=0
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=CASH_ACCOUNT,
            debit=0,
            credit=amount
        )

    elif journal_type == 'payment':
        # Vendor ko payment diya
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=VENDOR_ACCOUNT,
            contact=contact,
            debit=amount,
            credit=0
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=CASH_ACCOUNT,
            debit=0,
            credit=amount
        )

    elif journal_type == 'receipt':
        # Customer se receipt (payment received)
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=CASH_ACCOUNT,
            debit=amount,
            credit=0
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=CUSTOMER_ACCOUNT,
            contact=contact,
            debit=0,
            credit=amount
        )

    else:  # general journal
        # Default: Customer Debit, Cash Credit
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=CUSTOMER_ACCOUNT,
            contact=contact,
            debit=amount,
            credit=0
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=CASH_ACCOUNT,
            debit=0,
            credit=amount
        )