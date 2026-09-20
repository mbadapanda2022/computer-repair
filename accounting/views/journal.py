# accounting/views/journal.py
import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
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
from .utils import is_htmx, htmx_response, redirect_to_staff
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
            ('discount_allowed', 'Discount Allowed (हमने छूट दी)'),
            ('discount_received', 'Discount Received (उन्होंने छूट दी)'),
            ('advance_received', 'Advance Received (उनसे मिला)'),
            ('advance_paid', 'Advance Paid (उन्हें दिया)'),
        ], 'discount_allowed'

    if contact.contact_type == 'vendor':
        return [
            ('discount_received', 'Discount Received (Vendor से मिला)'),
            ('advance_paid', 'Advance Paid (Vendor को)'),
            ('general', 'General Journal'),
        ], 'discount_received'

    # customer
    return [
        ('discount_allowed', 'Discount Allowed (Customer को दिया)'),
        ('advance_received', 'Advance Received (Customer से)'),
        ('general', 'General Journal'),
    ], 'discount_allowed'


# ============================================================
# LIST JOURNALS
# ============================================================
@handle_errors(default_redirect='accounting:journal_list')
def journal_list(request):
    search = request.GET.get('search', '').strip()
    entry_type = request.GET.get('entry_type', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    reset = request.GET.get('reset', '')
    page_number = request.GET.get('page', 1)

    if reset:
        return redirect('accounting:journal_list')

    # Prefetch lines with contact & account to avoid N+1
    lines_prefetch = Prefetch(
        'lines',
        queryset=LedgerLine.objects.select_related('contact', 'account')
    )

    journals = LedgerEntry.objects.filter(entry_type='journal') \
        .prefetch_related(lines_prefetch) \
        .order_by('-date', '-id')

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

    context = {
        'journals': page_obj.object_list,
        'page_obj': page_obj,
        'search': search,
        'entry_type_filter': entry_type,
        'date_from': date_from,
        'date_to': date_to,
    }

    if is_htmx(request):
        return render(request, 'journal/partials/journal_table.html', context)
    return render(request, 'journal/journal_list.html', context)


# ============================================================
# CREATE JOURNAL (general)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:journal_list',
               htmx_template='journal/partials/journal_form_modal.html')
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
                date_val = form.cleaned_data['date'] or timezone.now().date()
                bank_account = form.cleaned_data.get('bank_account')

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
                    # Reload list with pagination context
                    journals = LedgerEntry.objects.filter(entry_type='journal') \
                        .select_related().order_by('-date', '-id')
                    paginator = Paginator(journals, 20)
                    page_obj = paginator.page(1)
                    return htmx_response(
                        request,
                        'journal/partials/journal_table.html',
                        context={
                            'journals': page_obj.object_list,
                            'page_obj': page_obj,
                        },
                        toast={'level': 'success', 'message': 'Journal entry recorded.'},
                        close_modal=True
                    )
                messages.success(request, "Journal entry recorded.")
                return redirect('accounting:journal_list')
            except Exception as e:
                logger.error(f"Journal create error: {e}", exc_info=True)
                messages.error(request, f"Failed to create journal entry: {e}")
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
@handle_errors(default_redirect='accounting:journal_list',
               htmx_template='journal/partials/journal_form_modal.html')
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
                date_val = form.cleaned_data['date'] or timezone.now().date()
                bank_account = form.cleaned_data.get('bank_account')

                entry = LedgerEntry.objects.create(
                    date=date_val,
                    entry_type='journal',
                    journal_type=entry_type,
                    description=narration or f"{entry_type} for {contact.name}",
                    total_amount=amount,
                )
                create_journal_lines(entry, contact, amount, entry_type, bank_account)

                logger.info(
                    f"Journal entry for contact {contact.id} created: {entry.id} "
                    f"by {request.user.username}"
                )

                if is_htmx(request):
                    journals = LedgerEntry.objects.filter(entry_type='journal') \
                        .order_by('-date', '-id')
                    paginator = Paginator(journals, 20)
                    page_obj = paginator.page(1)
                    return htmx_response(
                        request,
                        'journal/partials/journal_table.html',
                        context={
                            'journals': page_obj.object_list,
                            'page_obj': page_obj,
                        },
                        toast={'level': 'success', 'message': 'Transaction recorded.'},
                        close_modal=True
                    )

                messages.success(request, "Transaction recorded.")

                # 'both' type → combined statement
                if contact.contact_type == 'both':
                    return redirect('accounting:combined_statement', contact_id=contact.pk)
                elif contact.contact_type == 'customer':
                    return redirect('accounting:customer_statement', contact_id=contact.pk)
                else:
                    return redirect('accounting:vendor_statement', contact_id=contact.pk)

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
        initial = {'contact': contact, 'date': timezone.now().date()}
        form = JournalForm(initial=initial)
        form.fields['entry_type'].choices = entry_type_choices
        if entry_type_choices:
            form.fields['entry_type'].initial = default_choice

    return render(request, template_name, {'form': form, 'contact': contact})


# ============================================================
# UPDATE JOURNAL
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:journal_list',
               htmx_template='journal/partials/journal_form_modal.html')
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
        form = JournalForm(request.POST)
        if contact:
            entry_type_choices, _ = _choices_for_contact(contact)
            form.fields['entry_type'].choices = entry_type_choices

        if form.is_valid():
            try:
                contact = form.cleaned_data['contact']
                amount = form.cleaned_data['amount']
                entry_type = form.cleaned_data['entry_type']
                narration = form.cleaned_data['narration']
                date_val = form.cleaned_data['date'] or timezone.now().date()
                bank_account = form.cleaned_data.get('bank_account')

                entry.date = date_val
                entry.journal_type = entry_type
                entry.description = narration or f"{entry_type} for {contact.name}"
                entry.total_amount = amount
                entry.save()

                entry.lines.all().delete()
                create_journal_lines(entry, contact, amount, entry_type, bank_account)

                logger.info(f"Journal entry {pk} updated by {request.user.username}")

                if is_htmx(request):
                    journals = LedgerEntry.objects.filter(entry_type='journal') \
                        .order_by('-date', '-id')
                    paginator = Paginator(journals, 20)
                    page_obj = paginator.page(1)
                    return htmx_response(
                        request,
                        'journal/partials/journal_table.html',
                        context={
                            'journals': page_obj.object_list,
                            'page_obj': page_obj,
                        },
                        toast={'level': 'success', 'message': 'Journal updated.'},
                        close_modal=True
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
        initial = {
            'contact': contact,
            'amount': entry.total_amount,
            'entry_type': current_type,
            'date': entry.date,
            'narration': entry.description,
        }
        form = JournalForm(initial=initial)

        # Detect money_account from existing lines (bank vs cash)
        if entry.pk:
            bank_line = entry.lines.filter(account__code='1010').exists()
            initial['money_account'] = 'bank' if bank_line else 'cash'
            form = JournalForm(initial=initial)
            # Restore bank_account FK if present
            first_bank_line = entry.lines.filter(account__code='1010').first()
            if first_bank_line and contact:
                # bank_account not directly stored on line — best-effort: leave blank
                pass

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

        journals = LedgerEntry.objects.filter(entry_type='journal') \
            .order_by('-date', '-id')
        paginator = Paginator(journals, 20)
        page_obj = paginator.page(1)

        return htmx_response(
            request,
            'journal/partials/journal_table.html',
            context={'journals': page_obj.object_list, 'page_obj': page_obj},
            toast={'level': 'success', 'message': 'Journal deleted.'}
        )
    except Exception as e:
        logger.error(f"Journal delete error: {e}", exc_info=True)
        journals = LedgerEntry.objects.filter(entry_type='journal') \
            .order_by('-date', '-id')[:20]
        return htmx_response(
            request,
            'journal/partials/journal_table.html',
            context={'journals': journals},
            toast={'level': 'danger', 'message': 'Failed to delete journal entry.'}
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
            return HttpResponse(
                f'<div id="field-{field_name}" class="invalid-feedback d-block">'
                f'<div><i class="bi bi-exclamation-circle me-1"></i>Please select a valid contact.</div></div>'
            )
        return HttpResponse("")

    form = JournalForm(data={field_name: value})
    form.is_valid()
    errors = form.errors.get(field_name, [])

    error_html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
    for err in errors:
        error_html += f'<div><i class="bi bi-exclamation-circle me-1"></i>{err}</div>'
    error_html += '</div>'
    return HttpResponse(error_html)