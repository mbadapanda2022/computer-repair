# accounting/views/journal.py

import json
import logging
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.contrib import messages
from django.db.models import Q
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
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
        
    description = entry.description.lower()
    accounts = [line.account.lower() for line in entry.lines.all()]
    combined = ' '.join(accounts)
    
    if 'discount' in description or 'discount' in combined:
        return 'discount'
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
# LIST JOURNALS (with filters & pagination)
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

    # Use journal_type field instead of string matching
    journals = LedgerEntry.objects.filter(entry_type='journal').order_by('-date', '-id')

    if search:
        journals = journals.filter(
            Q(description__icontains=search) |
            Q(lines__account__icontains=search)
        ).distinct()
    
    # Clean filter using journal_type
    if entry_type:
        journals = journals.filter(journal_type=entry_type)

    if date_from:
        try:
            journals = journals.filter(date__gte=date_from)
        except ValueError:
            pass
    if date_to:
        try:
            journals = journals.filter(date__lte=date_to)
        except ValueError:
            pass

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
@handle_errors(default_redirect='accounting:journal_list', htmx_template='journal/partials/journal_form_modal.html')
def journal_create(request):
    template_name = 'journal/partials/journal_form_modal.html' if is_htmx(request) else 'journal/journal_form.html'

    if request.method == 'POST':
        form = JournalForm(request.POST)
        if form.is_valid():
            try:
                contact = form.cleaned_data['contact']
                amount = form.cleaned_data['amount']
                entry_type = form.cleaned_data['entry_type']  
                narration = form.cleaned_data['narration']
                date_val = form.cleaned_data['date'] or timezone.now().date()

                # Create entry with journal_type stored
                entry = LedgerEntry.objects.create(
                    date=date_val,
                    entry_type='journal',
                    journal_type=entry_type,  
                    description=narration or f"{entry_type} for {contact.name}",
                    total_amount=amount,
                )

                # Use helper to create lines (supports all scenarios)
                create_journal_lines(entry, contact, amount, entry_type)

                logger.info(f"Journal entry created: {entry.id} by {request.user.username}")

                if is_htmx(request):
                    journals = LedgerEntry.objects.filter(entry_type='journal').order_by('-date', '-id')
                    return htmx_response(
                        request,
                        'journal/partials/journal_table.html',
                        context={'journals': journals[:20]},
                        toast={'level': 'success', 'message': 'Journal entry recorded.'},
                        close_modal=True
                    )
                messages.success(request, "Journal entry recorded.")
                return redirect('accounting:journal_list')
            except Exception as e:
                logger.error(f"Journal create error: {e}")
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
@handle_errors(default_redirect='accounting:journal_list', htmx_template='journal/partials/journal_form_modal.html')
def journal_create_for_contact(request, contact_id):
    contact = get_object_or_404(Contact, pk=contact_id)
    
    # Dynamic choices based on contact type
    if contact.contact_type in ('vendor', 'both'):
        entry_type_choices = [
            ('payment', 'Payment to Vendor'),
            ('advance_paid', 'Advance Paid'),
            ('discount', 'Discount Received'),
        ]
    else:
        entry_type_choices = [
            ('receipt', 'Receipt from Customer'),
            ('advance_received', 'Advance Received'),
            ('discount', 'Discount Allowed'),
        ]

    template_name = 'journal/partials/journal_form_modal.html' if is_htmx(request) else 'journal/journal_form.html'

    if request.method == 'POST':
        form = JournalForm(request.POST)
        # Override choices dynamically
        form.fields['entry_type'].choices = entry_type_choices
        
        if form.is_valid():
            try:
                amount = form.cleaned_data['amount']
                entry_type = form.cleaned_data['entry_type']
                narration = form.cleaned_data['narration']
                date_val = form.cleaned_data['date'] or timezone.now().date()

                entry = LedgerEntry.objects.create(
                    date=date_val,
                    entry_type='journal',
                    journal_type=entry_type, 
                    description=narration or f"{entry_type} for {contact.name}",
                    total_amount=amount,
                )

                # Helper (supports all scenarios)
                create_journal_lines(entry, contact, amount, entry_type)

                logger.info(f"Journal entry for contact {contact.id} created: {entry.id} by {request.user.username}")

                if is_htmx(request):
                    journals = LedgerEntry.objects.filter(entry_type='journal').order_by('-date', '-id')
                    return htmx_response(
                        request,
                        'journal/partials/journal_table.html',
                        context={'journals': journals[:20]},
                        toast={'level': 'success', 'message': 'Transaction recorded.'},
                        close_modal=True
                    )
                messages.success(request, "Transaction recorded.")
                if contact.contact_type in ('customer', 'both'):
                    return redirect('accounting:customer_statement', contact_id=contact.pk)
                else:
                    return redirect('accounting:vendor_statement', contact_id=contact.pk)
            except Exception as e:
                logger.error(f"Journal create for contact error: {e}")
                messages.error(request, "Failed to record transaction.")
                if is_htmx(request):
                    return render(request, 'journal/partials/journal_form_modal.html', {'form': form, 'contact': contact})
        else:
            if is_htmx(request):
                return render(request, 'journal/partials/journal_form_modal.html', {'form': form, 'contact': contact})
    else:
        initial = {
            'contact': contact,
            'date': timezone.now().date()
        }
        form = JournalForm(initial=initial)
        form.fields['entry_type'].choices = entry_type_choices
        if entry_type_choices:
            form.fields['entry_type'].initial = entry_type_choices[0][0]

    return render(request, template_name, {'form': form, 'contact': contact})


# ============================================================
# UPDATE JOURNAL (Edit)
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:journal_list', htmx_template='journal/partials/journal_form_modal.html')
def journal_update(request, pk):
    entry = get_object_or_404(LedgerEntry, pk=pk, entry_type='journal')
    
    # Read journal_type directly from DB
    current_type = entry.journal_type or detect_journal_type(entry)
    contact = entry.lines.filter(contact__isnull=False).first().contact if entry.lines.filter(contact__isnull=False).exists() else None

    template_name = 'journal/partials/journal_form_modal.html' if is_htmx(request) else 'journal/journal_form.html'

    if request.method == 'POST':
        form = JournalForm(request.POST)
        if contact:
            if contact.contact_type in ('vendor', 'both'):
                form.fields['entry_type'].choices = [
                    ('payment', 'Payment to Vendor'),
                    ('advance_paid', 'Advance Paid'),
                    ('discount', 'Discount Received'),
                ]
            else:
                form.fields['entry_type'].choices = [
                    ('receipt', 'Receipt from Customer'),
                    ('advance_received', 'Advance Received'),
                    ('discount', 'Discount Allowed'),
                ]
                
        if form.is_valid():
            try:
                contact = form.cleaned_data['contact']
                amount = form.cleaned_data['amount']
                entry_type = form.cleaned_data['entry_type']
                narration = form.cleaned_data['narration']
                date_val = form.cleaned_data['date'] or timezone.now().date()

                # Update entry
                entry.date = date_val
                entry.journal_type = entry_type  # Update type
                entry.description = narration or f"{entry_type} for {contact.name}"
                entry.total_amount = amount
                entry.save()
                
                # Delete old lines and recreate
                entry.lines.all().delete()
                create_journal_lines(entry, contact, amount, entry_type)

                logger.info(f"Journal entry {pk} updated by {request.user.username}")

                if is_htmx(request):
                    journals = LedgerEntry.objects.filter(entry_type='journal').order_by('-date', '-id')
                    return htmx_response(
                        request,
                        'journal/partials/journal_table.html',
                        context={'journals': journals[:20]},
                        toast={'level': 'success', 'message': 'Journal updated.'},
                        close_modal=True
                    )
                messages.success(request, "Journal updated.")
                return redirect('accounting:journal_list')
            except Exception as e:
                logger.error(f"Journal update error: {e}")
                messages.error(request, "Failed to update journal entry.")
                if is_htmx(request):
                    return render(request, 'journal/partials/journal_form_modal.html', {'form': form, 'entry': entry})
        else:
            if is_htmx(request):
                return render(request, 'journal/partials/journal_form_modal.html', {'form': form, 'entry': entry})
    else:
        initial = {
            'contact': contact,
            'amount': entry.total_amount,
            'entry_type': current_type,
            'date': entry.date,
            'narration': entry.description,
        }
        form = JournalForm(initial=initial)
        if contact:
            if contact.contact_type in ('vendor', 'both'):
                form.fields['entry_type'].choices = [
                    ('payment', 'Payment to Vendor'),
                    ('advance_paid', 'Advance Paid'),
                    ('discount', 'Discount Received'),
                ]
            else:
                form.fields['entry_type'].choices = [
                    ('receipt', 'Receipt from Customer'),
                    ('advance_received', 'Advance Received'),
                    ('discount', 'Discount Allowed'),
                ]
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
        entry.delete()
        logger.info(f"Journal entry {pk} deleted by {request.user.username}")
        journals = LedgerEntry.objects.filter(entry_type='journal').order_by('-date', '-id')
        return htmx_response(
            request,
            'journal/partials/journal_table.html',
            context={'journals': journals[:20]},
            toast={'level': 'success', 'message': 'Journal deleted.'}
        )
    except Exception as e:
        logger.error(f"Journal delete error: {e}")
        return htmx_response(
            request,
            'journal/partials/journal_table.html',
            context={'journals': LedgerEntry.objects.filter(entry_type='journal').order_by('-date', '-id')[:20]},
            toast={'level': 'danger', 'message': 'Failed to delete journal entry.'}
        )


# ============================================================
# REAL-TIME FIELD VALIDATION (HTMX) — PROFESSIONAL
# ============================================================
@require_http_methods(["GET"])
def validate_journal_field(request):
    """Real-time validation using the form (no string matching)."""
    field_name = request.GET.get('field')
    if not field_name:
        return HttpResponse("")

    value = request.GET.get(field_name, '')
    
    # Use the form for validation
    from ..forms import JournalForm
    
    # Build data dict
    data = {field_name: value}
    # For contact validation, we need the actual contact object, but we just validate the ID
    if field_name == 'contact':
        try:
            Contact.objects.get(pk=value)
        except (Contact.DoesNotExist, ValueError):
            errors = ["Please select a valid contact."]
            html = f'<div id="field-{field_name}" class="invalid-feedback d-block">{"".join(f"<div>{e}</div>" for e in errors)}</div>'
            return HttpResponse(html)
        return HttpResponse("")  # Valid
    
    # For other fields, use the form
    form = JournalForm(data)
    form.is_valid()
    errors = form.errors.get(field_name, [])
    
    error_html = f'<div id="field-{field_name}" class="invalid-feedback d-block">'
    for err in errors:
        error_html += f'<div><i class="bi bi-exclamation-circle me-1"></i>{err}</div>'
    error_html += '</div>'
    return HttpResponse(error_html)