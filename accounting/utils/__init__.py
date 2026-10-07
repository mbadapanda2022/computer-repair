# accounting/utils/__init__.py

# ============================================================
# LEDGER HELPERS
# ============================================================

def create_or_update_invoice_ledger(invoice):
    """
    Create or update ledger entries for an invoice.
    Always delete old entry first to avoid ghost lines.
    """
    from ..models import LedgerEntry, LedgerLine   # ✅ Local Import

    # Delete old entry (including all its LedgerLine children)
    LedgerEntry.objects.filter(
        reference_id=invoice.id,
        entry_type='sales'
    ).delete()

    if invoice.grand_total == 0:
        return

    entry = LedgerEntry.objects.create(
        date=invoice.date,
        entry_type='sales',
        reference_id=invoice.id,
        description=f"Invoice {invoice.invoice_number}",
        total_amount=invoice.grand_total
    )

    LedgerLine.objects.create(
        ledger_entry=entry,
        account=f'Customer: {invoice.customer.name}',
        contact=invoice.customer,
        debit=invoice.grand_total,
        credit=0
    )

    LedgerLine.objects.create(
        ledger_entry=entry,
        account='Sales',
        debit=0,
        credit=invoice.grand_total
    )


def create_journal_lines(entry, contact, amount, journal_type, bank_account=None):
    """
    Create ledger lines for a journal entry.

    Idempotent: clears existing lines first (per-instance so their
    post_delete signals fire and Contact balances recalc correctly).

    Persists the specific bank_account on the entry (for Journal edit
    restore — see journal.py). If bank_account is None, entry stays
    as Cash.
    """
    from ..models import LedgerLine, get_account

    # Persist / clear the specific bank account on the entry
    if bank_account is not None and entry.bank_account_id != bank_account.pk:
        entry.bank_account = bank_account
        entry.save(update_fields=['bank_account'])
    elif bank_account is None and entry.bank_account_id:
        entry.bank_account = None
        entry.save(update_fields=['bank_account'])

    # Clear existing lines — per-instance (fires post_delete signals)
    for old_line in list(entry.lines.all()):
        old_line.delete()

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