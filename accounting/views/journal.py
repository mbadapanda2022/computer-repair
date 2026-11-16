# accounting/views/journal.py
import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.http import HttpResponse
from django.contrib import messages
from django.db.models import Q, Sum, Prefetch
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db import transaction
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.utils import timezone

from ..models import LedgerEntry, LedgerLine, Contact
from ..forms import JournalForm
from .utils import htmx_field_error_response, is_htmx, htmx_response, redirect_to_staff
from ..decorators import handle_errors
from ..utils import create_journal_lines

logger = logging.getLogger(__name__)


# ============================================================
# HELPER: Detect journal_type from existing entry (Backfill)
# ============================================================
def detect_journal_type(entry):
    """Detect journal_type from description/accounts (only for old data)."""
    if entry.journal_type:
        return entry.journal_type

    description = (entry.description or '').lower()
    # line.account is FK → use .name
    account_names = [line.account.name.lower() for line in entry.lines.all() if line.account]
    combined = ' '.join(account_names)

    if 'discount received' in description or 'discount received' in combined:
        return 'discount_received'
    if 'discount allowed' in description or 'discount allowed' in combined:
        return 'discount_allowed'
    if 'discount' in description or 'discount' in combined:
        return 'discount'  # legacy fallback
    if 'advance received' in description or 'advance_received' in description:
        return 'advance_received'
    if 'advance paid' in description or 'advance_paid' in description:
        return 'advance_paid'
    if 'payment' in description or 'vendor' in combined:
        return 'payment'
    if 'receipt' in description or 'customer' in combined:
        return 'receipt'
    return 'general'


# ============================================================
# HELPER: Dynamic entry-type choices for a contact
# ============================================================
def _choices_for_contact(contact):
    """
    Return (choices, default) for entry_type based on contact type.

    - 'both'     → all 4 professional types (staff decides side)
    - 'vendor'   → discount_received, advance_paid, general
    - customer   → discount_allowed, advance_received, general
    """
    if contact is None:
        return JournalForm.ENTRY_TYPE_CHOICES, 'general'

    if contact.contact_type == 'both':
        return [
            ('discount_allowed', 'Discount Allowed (we waived)'),
            ('discount_received', 'Discount Received (they waived)'),
            ('advance_received', 'Advance Received (from them)'),
            ('advance_paid', 'Advance Paid (to them)'),
        ], 'discount_allowed'

    if contact.contact_type == 'vendor':
        return [
            ('discount_received', 'Discount Received (from vendor)'),
            ('advance_paid', 'Advance Paid (to vendor)'),
            ('general', 'General Journal'),
        ], 'discount_received'

    # customer
    return [
        ('discount_allowed', 'Discount Allowed (to customer)'),
        ('advance_received', 'Advance Received (from customer)'),
        ('general', 'General Journal'),
    ], 'discount_allowed'


# ============================================================
# HELPER: Prefetched journal queryset
# ============================================================
def _journal_queryset():
    lines_prefetch = Prefetch(
        'lines',
        queryset=LedgerLine.objects.select_related('contact', 'account')
    )
    contact_lines_prefetch = Prefetch(
        'lines',
        queryset=LedgerLine.objects.filter(contact__isnull=False).select_related('contact'),
        to_attr='contact_lines'
    )
    return (
        LedgerEntry.objects.filter(entry_type='journal')
        .prefetch_related(lines_prefetch, contact_lines_prefetch)
        .order_by('-date', '-id')
    )


def _journal_table_context(request, page_number=1):
    """Build filtered + paginated context for the journal table partial.

    Every HTMX response that swaps `#journal-table-container` goes through
    this helper, so all rows always carry the `contact_lines` prefetch
    (without it the Contact column renders as em-dash).
    """
    search = request.GET.get('search', '').strip()
    entry_type = request.GET.get('entry_type', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    journals = _journal_queryset()

    if search:
        journals = journals.filter(
            Q(description__icontains=search) |
            Q(lines__account__name__icontains=search) |
            Q(lines__account__code__icontains=search) |
            Q(lines__contact__name__icontains=search)
        ).distinct()

    if entry_type:
        journals = journals.filter(journal_type=entry_type)
    if date_from:
        journals = journals.filter(date__gte=date_from)
    if date_to:
        journals = journals.filter(date__lte=date_to)

    paginator = Paginator(journals, 20)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return {
        'journals': page_obj.object_list,
        'page_obj': page_obj,
        'search': search,
        'entry_type_filter': entry_type,
        'date_from': date_from,
        'date_to': date_to,
    }


def _statement_url_for_contact(contact):
    if contact.contact_type == 'both':
        return reverse('accounting:combined_statement', args=[contact.pk])
    if contact.contact_type == 'customer':
        return reverse('accounting:customer_statement', args=[contact.pk])
    return reverse('accounting:vendor_statement', args=[contact.pk])


# ============================================================
# LIST JOURNALS
# ============================================================
@handle_errors(default_redirect='accounting:journal_list')
def journal_list(request):
    reset = request.GET.get('reset', '')
    if reset:
        return redirect('accounting:journal_list')

    context = _journal_table_context(request, request.GET.get('page', 1))

    if is_htmx(request):
        return render(request, 'journal/partials/journal_table.html', context)
    return render(request, 'journal/journal_list.html', context)


# ============================================================
# CREATE JOURNAL (general)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:journal_list')
def journal_create(request):
    template_name = (
        'journal/partials/journal_form_modal.html'
        if is_htmx(request) else 'journal/journal_form.html'
    )

    if request.method == 'POST':
        form = JournalForm(request.POST)
        if form.is_valid():
            try:
                contact = form.cleaned_data['contact']
                amount = form.cleaned_data['amount']
                entry_type = form.cleaned_data['entry_type']
                narration = form.cleaned_data['narration']
                date_val = form.cleaned_data['date'] or timezone.localdate()
                bank_account = form.cleaned_data.get('bank_account')

                with transaction.atomic():
                    entry = LedgerEntry.objects.create(
                        date=date_val,
                        entry_type='journal',
                        journal_type=entry_type,
                        description=narration or f"{entry_type} for {contact.name}",
                        total_amount=amount,
                    )
                    create_journal_lines(entry, contact, amount, entry_type, bank_account)

                logger.info(f"Journal entry created: {entry.id} by {request.user.username}")

                if is_htmx(request):
                    return htmx_response(
                        request,
                        'journal/partials/journal_table.html',
                        context=_journal_table_context(request),
                        toast={'level': 'success', 'message': 'Journal entry recorded.'},
                        close_modal=True,
                        extra_headers={
                            'HX-Retarget': '#journal-table-container',
                            'HX-Reswap': 'innerHTML',
                        },
                    )
                messages.success(request, "Journal entry recorded.")
                return redirect('accounting:journal_list')
            except Exception as e:
                logger.error(f"Journal create error: {e}", exc_info=True)
                messages.error(request, "Failed to create journal entry.")
                if is_htmx(request):
                    return render(request, 'journal/partials/journal_form_modal.html', {'form': form})
        else:
            if is_htmx(request):
                return render(request, 'journal/partials/journal_form_modal.html', {'form': form})
    else:
        form = JournalForm()

    return render(request, template_name, {'form': form})


# ============================================================
# CREATE JOURNAL FOR SPECIFIC CONTACT
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:journal_list')
def journal_create_for_contact(request, contact_id):
    contact = get_object_or_404(Contact, pk=contact_id)

    # Dynamic choices based on contact type
    entry_type_choices, default_choice = _choices_for_contact(contact)

    template_name = (
        'journal/partials/journal_form_modal.html'
        if is_htmx(request) else 'journal/journal_form.html'
    )

    if request.method == 'POST':
        form = JournalForm(request.POST)
        form.fields['entry_type'].choices = entry_type_choices

        if form.is_valid():
            try:
                amount = form.cleaned_data['amount']
                entry_type = form.cleaned_data['entry_type']
                narration = form.cleaned_data['narration']
                date_val = form.cleaned_data['date'] or timezone.localdate()
                bank_account = form.cleaned_data.get('bank_account')

                with transaction.atomic():
                    entry = LedgerEntry.objects.create(
                        date=date_val,
                        entry_type='journal',
                        journal_type=entry_type,
                        description=narration or f"{entry_type} for {contact.name}",
                        total_amount=amount,
                        bank_account=bank_account,
                    )
                    create_journal_lines(entry, contact, amount, entry_type, bank_account)

                logger.info(
                    f"Journal entry for contact {contact.id} created: {entry.id} "
                    f"by {request.user.username}"
                )
                statement_url = _statement_url_for_contact(contact)

                if is_htmx(request):
                    # Opened from a statement/contact page — return there so the
                    # new transaction is visible in the ledger statement.
                    messages.success(request, "Transaction recorded.")
                    return htmx_response(
                        request,
                        'journal/partials/journal_form_modal.html',
                        context={'form': form, 'contact': contact},
                        toast={'level': 'success', 'message': 'Transaction recorded.'},
                        close_modal=True,
                        redirect_url=statement_url,
                    )

                messages.success(request, "Transaction recorded.")
                return redirect(statement_url)

            except Exception as e:
                logger.error(f"Journal create for contact error: {e}", exc_info=True)
                messages.error(request, "Failed to record transaction.")
                if is_htmx(request):
                    return render(request, 'journal/partials/journal_form_modal.html', {
                        'form': form, 'contact': contact
                    })
        else:
            if is_htmx(request):
                return render(request, 'journal/partials/journal_form_modal.html', {
                    'form': form, 'contact': contact
                })
    else:
        initial = {'contact': contact, 'date': timezone.localdate()}
        form = JournalForm(initial=initial)
        form.fields['entry_type'].choices = entry_type_choices
        if entry_type_choices:
            form.fields['entry_type'].initial = default_choice

    return render(request, template_name, {'form': form, 'contact': contact})


# ============================================================
# UPDATE JOURNAL
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:journal_list')
def journal_update(request, pk):
    entry = get_object_or_404(LedgerEntry, pk=pk, entry_type='journal')

    current_type = entry.journal_type or detect_journal_type(entry)
    # Get contact from first line that has it
    first_contact_line = entry.lines.filter(contact__isnull=False).first()
    contact = first_contact_line.contact if first_contact_line else None

    template_name = (
        'journal/partials/journal_form_modal.html'
        if is_htmx(request) else 'journal/journal_form.html'
    )

    if request.method == 'POST':
        # Full choice list on POST: the user may have switched the contact
        # inside the form, and JournalForm.clean() validates the pairing.
        form = JournalForm(request.POST)

        if form.is_valid():
            try:
                contact = form.cleaned_data['contact']
                amount = form.cleaned_data['amount']
                entry_type = form.cleaned_data['entry_type']
                narration = form.cleaned_data['narration']
                date_val = form.cleaned_data['date'] or timezone.localdate()
                bank_account = form.cleaned_data.get('bank_account')

                with transaction.atomic():
                    entry.date = date_val
                    entry.journal_type = entry_type
                    entry.description = narration or f"{entry_type} for {contact.name}"
                    entry.total_amount = amount
                    entry.save()

                    # create_journal_lines clears the old lines per-instance
                    # (fires LedgerLine signals) and re-syncs entry.bank_account.
                    create_journal_lines(entry, contact, amount, entry_type, bank_account)

                logger.info(f"Journal entry {pk} updated by {request.user.username}")

                if is_htmx(request):
                    return htmx_response(
                        request,
                        'journal/partials/journal_table.html',
                        context=_journal_table_context(request),
                        toast={'level': 'success', 'message': 'Journal updated.'},
                        close_modal=True,
                        extra_headers={
                            'HX-Retarget': '#journal-table-container',
                            'HX-Reswap': 'innerHTML',
                        },
                    )
                messages.success(request, "Journal updated.")
                return redirect('accounting:journal_list')
            except Exception as e:
                logger.error(f"Journal update error: {e}", exc_info=True)
                messages.error(request, "Failed to update journal entry.")
                if is_htmx(request):
                    return render(request, 'journal/partials/journal_form_modal.html', {
                        'form': form, 'entry': entry
                    })
        else:
            if is_htmx(request):
                return render(request, 'journal/partials/journal_form_modal.html', {
                    'form': form, 'entry': entry
                })
    else:
        # Don't surface the auto-generated fallback description as narration.
        auto_description = (
            contact and entry.description == f"{current_type} for {contact.name}"
        )
        has_bank_line = entry.bank_account_id or entry.lines.filter(
            account__code='1010'
        ).exists()

        initial = {
            'contact': contact,
            'amount': entry.total_amount,
            'entry_type': current_type,
            'date': entry.date,
            'narration': '' if auto_description else entry.description,
            'money_account': 'bank' if has_bank_line else 'cash',
            'bank_account': entry.bank_account_id,
        }
        form = JournalForm(initial=initial)

        if contact:
            entry_type_choices, _ = _choices_for_contact(contact)
            form.fields['entry_type'].choices = entry_type_choices
            form.fields['entry_type'].initial = current_type

    return render(request, template_name, {'form': form, 'entry': entry})


# ============================================================
# DELETE JOURNAL
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:journal_list')
def journal_delete(request, pk):
    entry = get_object_or_404(LedgerEntry, pk=pk, entry_type='journal')
    try:
        with transaction.atomic():
            for line in list(entry.lines.all()):
                line.delete()
            entry.delete()

        logger.info(f"Journal entry {pk} deleted by {request.user.username}")

        return htmx_response(
            request,
            'journal/partials/journal_table.html',
            context=_journal_table_context(request),
            toast={'level': 'success', 'message': 'Journal deleted.'},
            extra_headers={'HX-Retarget': '#journal-table-container', 'HX-Reswap': 'innerHTML'},
        )
    except Exception as e:
        logger.error(f"Journal delete error: {e}", exc_info=True)
        return htmx_response(
            request,
            'journal/partials/journal_table.html',
            context=_journal_table_context(request),
            toast={'level': 'danger', 'message': 'Failed to delete journal entry.'},
            extra_headers={'HX-Retarget': '#journal-table-container', 'HX-Reswap': 'innerHTML'},
        )


# ============================================================
# REAL-TIME FIELD VALIDATION
# ============================================================
@require_http_methods(["GET"])
def validate_journal_field(request):
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    value = request.GET.get(field_name, '')

    if field_name == 'contact':
        try:
            Contact.objects.get(pk=value)
        except (Contact.DoesNotExist, ValueError):
            return htmx_field_error_response(
                field_name, ['Please select a valid contact.'],
            )
        return HttpResponse("")

    form = JournalForm(data={field_name: value})
    form.is_valid()
    errors = form.errors.get(field_name, [])

    return htmx_field_error_response(field_name, errors)