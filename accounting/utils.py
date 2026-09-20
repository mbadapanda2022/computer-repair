# accounting/utils.py
from decimal import Decimal
from django.db import transaction
from .models import LedgerEntry, LedgerLine, Account
from .models import get_account


def create_journal_lines(entry, contact, amount, journal_type, bank_account=None):
    """
    Create ledger lines for a journal entry.

    Supported journal_type:
        Professional:
            discount_allowed   → Dr Discount Allowed / Cr Customer Receivable
            discount_received  → Dr Vendor Payable / Cr Discount Received
            advance_received   → Dr Cash/Bank / Cr Advance from Customer
            advance_paid       → Dr Advance to Vendor / Cr Cash/Bank
            general            → Dr Customer Receivable / Cr Cash/Bank
        Legacy (backward compat):
            discount  → contact_type based (customer/both = allowed, vendor = received)
            payment   → Dr Vendor Payable / Cr Cash/Bank
            receipt   → Dr Cash/Bank / Cr Customer Receivable
    """
    # Money account (Cash or Bank)
    if bank_account:
        money_account = get_account('1010', 'Bank Account', 'asset', '1')
    else:
        money_account = get_account('1001', 'Cash', 'asset', '1')

    CUSTOMER_ACCOUNT = get_account('1011', 'Customer Receivable', 'asset', '1')
    VENDOR_ACCOUNT = get_account('2011', 'Vendor Payable', 'liability', '2')
    ADVANCE_RECEIVED_ACCOUNT = get_account('1012', 'Advance from Customer', 'liability', '2')
    ADVANCE_PAID_ACCOUNT = get_account('1014', 'Advance to Vendor', 'asset', '1')
    DISCOUNT_ALLOWED_ACCOUNT = get_account('5010', 'Discount Allowed', 'expense', '5')
    DISCOUNT_RECEIVED_ACCOUNT = get_account('4011', 'Discount Received', 'income', '4')

    # ── PROFESSIONAL TYPES ──
    if journal_type == 'discount_allowed':
        LedgerLine.objects.create(ledger_entry=entry, account=DISCOUNT_ALLOWED_ACCOUNT, debit=amount, credit=0)
        LedgerLine.objects.create(ledger_entry=entry, account=CUSTOMER_ACCOUNT, contact=contact, debit=0, credit=amount)

    elif journal_type == 'discount_received':
        LedgerLine.objects.create(ledger_entry=entry, account=VENDOR_ACCOUNT, contact=contact, debit=amount, credit=0)
        LedgerLine.objects.create(ledger_entry=entry, account=DISCOUNT_RECEIVED_ACCOUNT, debit=0, credit=amount)

    elif journal_type == 'advance_received':
        LedgerLine.objects.create(ledger_entry=entry, account=money_account, debit=amount, credit=0)
        LedgerLine.objects.create(ledger_entry=entry, account=ADVANCE_RECEIVED_ACCOUNT, contact=contact, debit=0, credit=amount)

    elif journal_type == 'advance_paid':
        LedgerLine.objects.create(ledger_entry=entry, account=ADVANCE_PAID_ACCOUNT, contact=contact, debit=amount, credit=0)
        LedgerLine.objects.create(ledger_entry=entry, account=money_account, debit=0, credit=amount)

    # ── LEGACY TYPES ──
    elif journal_type == 'discount':
        if contact.contact_type in ('customer', 'both'):
            LedgerLine.objects.create(ledger_entry=entry, account=CUSTOMER_ACCOUNT, contact=contact, debit=0, credit=amount)
            LedgerLine.objects.create(ledger_entry=entry, account=DISCOUNT_ALLOWED_ACCOUNT, debit=amount, credit=0)
        else:
            LedgerLine.objects.create(ledger_entry=entry, account=VENDOR_ACCOUNT, contact=contact, debit=amount, credit=0)
            LedgerLine.objects.create(ledger_entry=entry, account=DISCOUNT_RECEIVED_ACCOUNT, debit=0, credit=amount)

    elif journal_type == 'payment':
        LedgerLine.objects.create(ledger_entry=entry, account=VENDOR_ACCOUNT, contact=contact, debit=amount, credit=0)
        LedgerLine.objects.create(ledger_entry=entry, account=money_account, debit=0, credit=amount)

    elif journal_type == 'receipt':
        LedgerLine.objects.create(ledger_entry=entry, account=money_account, debit=amount, credit=0)
        LedgerLine.objects.create(ledger_entry=entry, account=CUSTOMER_ACCOUNT, contact=contact, debit=0, credit=amount)

    # ── GENERAL (fallback) ──
    else:
        LedgerLine.objects.create(ledger_entry=entry, account=CUSTOMER_ACCOUNT, contact=contact, debit=amount, credit=0)
        LedgerLine.objects.create(ledger_entry=entry, account=money_account, debit=0, credit=amount)