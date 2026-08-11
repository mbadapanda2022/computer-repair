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


def create_journal_lines(entry, contact, amount, journal_type):
    """
    Create ledger lines for a journal entry based on type.
    Handles ALL scenarios:
    - Customer Receipt (customer se paisa aaya)
    - Vendor Payment (vendor ko paisa diya)
    - Advance Received (customer se advance aaya)
    - Advance Paid (vendor ko advance diya)
    - Discount Allowed (customer ko discount diya)
    - Discount Received (vendor se discount mila)
    - General Journal (default)
    """
    # इस फंक्शन को models की ज़रूरत नहीं – यह सिर्फ entry (already passed) का उपयोग करता है
    # इसलिए यहाँ import की ज़रूरत नहीं
    # ... बाकी कोड वैसा ही रहेगा ...