# accounting/utils/__init__.py

# ============================================================
# LEDGER HELPERS
# ============================================================

from ..models import LedgerEntry, LedgerLine


def create_or_update_invoice_ledger(invoice):
    """
    Create or update ledger entries for an invoice.
    Always delete old entry first to avoid ghost lines.
    """
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
    
    if journal_type == 'discount':
        if contact.contact_type in ('customer', 'both'):
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=f'Customer: {contact.name}',
                contact=contact,
                debit=0,
                credit=amount
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account='Discount Allowed',
                debit=amount,
                credit=0
            )
        else:
            LedgerLine.objects.create(
                ledger_entry=entry,
                account=f'Vendor: {contact.name}',
                contact=contact,
                debit=amount,
                credit=0
            )
            LedgerLine.objects.create(
                ledger_entry=entry,
                account='Discount Received',
                debit=0,
                credit=amount
            )
            
    elif journal_type == 'advance_received':
        LedgerLine.objects.create(
            ledger_entry=entry,
            account='Cash',
            debit=amount,
            credit=0
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=f'Customer: {contact.name}',
            contact=contact,
            debit=0,
            credit=amount
        )
        
    elif journal_type == 'advance_paid':
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=f'Vendor: {contact.name}',
            contact=contact,
            debit=amount,
            credit=0
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account='Cash',
            debit=0,
            credit=amount
        )
        
    elif journal_type == 'payment':
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=f'Vendor: {contact.name}',
            contact=contact,
            debit=amount,
            credit=0
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account='Cash',
            debit=0,
            credit=amount
        )
        
    elif journal_type == 'receipt':
        LedgerLine.objects.create(
            ledger_entry=entry,
            account='Cash',
            debit=amount,
            credit=0
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=f'Customer: {contact.name}',
            contact=contact,
            debit=0,
            credit=amount
        )
        
    else:  # general journal
        LedgerLine.objects.create(
            ledger_entry=entry,
            account=f'Customer: {contact.name}',
            contact=contact,
            debit=amount,
            credit=0
        )
        LedgerLine.objects.create(
            ledger_entry=entry,
            account='Cash',
            debit=0,
            credit=amount
        )