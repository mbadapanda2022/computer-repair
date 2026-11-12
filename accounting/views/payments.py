# accounting/views/payments.py

import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse
from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum, F
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.urls import reverse

from ..models import (
    Payment, BankTransaction, Contact, Invoice, PaymentAllocation,
    Purchase, PurchaseAllocation, CompanyProfile,
)
from ..forms import PaymentForm
from .utils import is_htmx, htmx_response, redirect_to_staff
from ..decorators import handle_errors

from accounting.utils.notification_helpers import (
    send_notification_to_customer,
    send_notification_to_staff,
)

logger = logging.getLogger(__name__)


# ============================================================
# HELPERS
# ============================================================
def get_unpaid_invoices_for_contact(contact, direction='received'):
    """
    Return unpaid or partially-paid invoices for a customer contact.
    Only meaningful for direction='received'.
    """
    if direction == 'received' and contact and contact.contact_type in ('customer', 'both'):
        return (
            Invoice.objects
            .filter(customer=contact, balance_due__gt=0)
            .exclude(payment_status='paid')
            .order_by('date', 'id')
        )
    return Invoice.objects.none()


def _safe_decimal(value, default=Decimal('0')):
    if value is None:
        return default
    s = str(value).strip()
    if s == '':
        return default
    try:
        return Decimal(s)
    except (ValueError, InvalidOperation, TypeError):
        return default


TAX_PRECISION = Decimal('0.01')


class _AllocationBudget:
    """Tracks how much of a payment's allocation pool is still unassigned.

    A payment can settle at most ``allocation_pool`` across every invoice and
    vendor bill. The HTML form clamps this client-side, but a submitted request
    can carry any number, so the server has to enforce the same ceiling.
    """

    def __init__(self, amount):
        self.remaining = max(Decimal('0'), amount)

    def take(self, requested):
        amount = min(requested, self.remaining)
        if amount <= 0:
            return Decimal('0')
        self.remaining = (self.remaining - amount).quantize(TAX_PRECISION)
        return amount


def _payment_budget(payment):
    """Remaining allocation pool for a payment, net of existing rows."""
    used = (
        payment.allocations.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    ) + (
        payment.purchase_allocations.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    ) + (
        payment.advance_adjustments.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    )
    return _AllocationBudget(payment.allocation_pool - used)


class _PaymentDiscountTooLarge(Exception):
    """Discount is not backed by any bill this payment settles."""


def _assert_discount_matches_allocations(payment):
    """A discount is a concession ON a bill, not free credit.

    The ledger credits the contact by ``amount + discount``. If the discount
    is bigger than what the payment actually applies to bills, the receivable
    (or vendor payable) goes negative by a number no bill explains — a ₹5,000
    discount on a ₹100 receipt once pushed the customer to −₹1,200.

    Advances never allocate themselves; their discount is only allowed up to
    the open balance it reduces, so it can waive part of a bill but never
    flip the contact into an unexplained credit.
    """
    discount = payment.discount_amount or Decimal('0')
    if discount <= 0:
        return

    if payment.is_advance:
        contact = payment.contact
        contact.recalc_balance()
        if payment.direction == 'received':
            balance_after = contact.receivable_balance
        else:
            balance_after = contact.payable_balance
        # recalc_balance already includes this payment's discount credit, so
        # add it back to get what was owed before the waiver.
        open_before = balance_after + discount
        if balance_after < Decimal('0'):
            raise _PaymentDiscountTooLarge(
                f'Discount Rs.{discount:.2f} is more than the '
                f'Rs.{open_before:.2f} open on this contact. A discount on '
                f'an advance can only waive part of an open bill.'
            )
        return

    applied = (
        payment.allocations.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    ) + (
        payment.purchase_allocations.aggregate(s=Sum('amount'))['s'] or Decimal('0')
    )
    if discount > applied:
        raise _PaymentDiscountTooLarge(
            f'Discount Rs.{discount:.2f} is more than the '
            f'Rs.{applied:.2f} applied to bills. A discount can only settle '
            f'the bill it is given against — allocate the payment to a bill '
            f'or lower the discount.'
        )


DUPLICATE_LOOKBACK = timedelta(hours=24)


def _find_matching_payments(contact, direction, amount, date, method):
    """Live payments that look like the one being saved right now.

    Re-entering a receipt because the first attempt appeared to fail is how
    a customer gets credited twice (₹4,400 became ₹8,800 in production).
    Same contact + direction + amount + date + method inside the last day
    is the signature of that mistake, so it needs an explicit confirmation.
    """
    return Payment.objects.filter(
        contact=contact,
        direction=direction,
        amount=amount,
        date=date,
        method=method,
        created_at__gte=timezone.now() - DUPLICATE_LOOKBACK,
    ).order_by('created_at')


def _attach_posted_allocations(rows, post, field_prefix):
    """Keep typed allocation amounts when the form re-renders.

    Without this, any validation error wiped every allocation box and a
    resubmit silently saved the payment unallocated — invoices then stayed
    unpaid even though the shop had filled the amounts in.
    """
    for row in rows:
        key = f'{field_prefix}{row.pk}'
        raw = (post.get(key) or '').strip()
        amount = _safe_decimal(raw) if raw else Decimal('0')
        row.has_posted_allocation = key in post
        row.posted_allocation = amount if amount > 0 else Decimal('0')
        if amount > 0 and amount > (row.balance_due or Decimal('0')):
            row.edit_available = amount.quantize(TAX_PRECISION)
    return rows


def _posted_allocation_context(request, contact, direction):
    """Unpaid rows for the bound form, with typed amounts preserved."""
    unpaid_invoices = Invoice.objects.none()
    unpaid_purchases = Purchase.objects.none()
    if contact and direction == 'received':
        unpaid_invoices = _attach_posted_allocations(
            list(get_unpaid_invoices_for_contact(contact, 'received')),
            request.POST, 'allocation_amount_',
        )
    elif contact and direction == 'paid':
        unpaid_purchases = _attach_posted_allocations(
            list(get_unpaid_purchases_for_contact(contact)),
            request.POST, 'purchase_alloc_',
        )
    return unpaid_invoices, unpaid_purchases


def _recalc_invoices_by_ids(invoice_ids):
    """Recompute paid_amount/balance_due/status for the given invoices.

    ``Payment.update_invoices()`` only walks rows that still exist, so it cannot
    refresh an invoice whose allocation was just removed. Both edit and trim
    paths use this to make sure dropped bills reopen. Written as a direct
    update (not ``inv.save()``) so recalculating dues never re-runs totals,
    ledger sync or item-based maths — this is strictly a settlement refresh.
    """
    ids = set(invoice_ids)
    if not ids:
        return
    contact_ids = set()
    for inv in Invoice.objects.filter(pk__in=ids).select_related('customer'):
        total_paid = (
            inv.payment_allocations.aggregate(s=Sum('amount'))['s'] or Decimal('0')
        ) + (
            inv.advance_adjustments.aggregate(s=Sum('amount'))['s'] or Decimal('0')
        )
        balance_due = max(
            Decimal('0'), inv.grand_total - total_paid
        ).quantize(TAX_PRECISION)
        if balance_due <= 0:
            status = 'paid'
        elif total_paid > 0 and balance_due < inv.grand_total:
            status = 'partial'
        else:
            status = 'unpaid'
        Invoice.objects.filter(pk=inv.pk).update(
            paid_amount=total_paid,
            balance_due=balance_due,
            payment_status=status,
        )
        if inv.customer_id:
            contact_ids.add(inv.customer_id)
    for contact in Contact.objects.filter(pk__in=contact_ids):
        contact.recalc_balance()
        contact.recalc_advance_balance()


def _recalc_purchases_by_ids(purchase_ids):
    for purchase in Purchase.objects.filter(pk__in=set(purchase_ids)):
        purchase.recalc_settlement()


def _safe_next_url(request):
    """Internal-path redirect target from ?next= (blocks off-site redirects)."""
    candidate = request.POST.get('next') or request.GET.get('next')
    if candidate and candidate.startswith('/') and not candidate.startswith('//'):
        return candidate
    return None


def _filter_payment_queryset(request):
    """Apply list filters from GET params. Returns (queryset, params)."""
    queryset = (
        Payment.objects
        .select_related('contact', 'bank_account', 'expense_account')
        .prefetch_related(
            'allocations__invoice', 'purchase_allocations__purchase',
            'advance_adjustments__invoice',
        )
        .all()
        .order_by('-date', '-id')
    )

    params = {
        'search': request.GET.get('search', '').strip(),
        'direction': request.GET.get('direction', '').strip(),
        'method': request.GET.get('method', '').strip(),
        'date_from': request.GET.get('date_from', '').strip(),
        'date_to': request.GET.get('date_to', '').strip(),
        'reconciled': request.GET.get('reconciled', '').strip(),
    }

    if params['search']:
        queryset = queryset.filter(
            Q(contact__name__icontains=params['search']) |
            Q(contact__phone__icontains=params['search']) |
            Q(reference__icontains=params['search']) |
            Q(upi_ref__icontains=params['search']) |
            Q(description__icontains=params['search']) |
            Q(expense_account__name__icontains=params['search']) |
            Q(allocations__invoice__invoice_number__icontains=params['search']) |
            Q(purchase_allocations__purchase__purchase_number__icontains=params['search'])
        ).distinct()
    if params['direction']:
        queryset = queryset.filter(direction=params['direction'])
    if params['method']:
        queryset = queryset.filter(method=params['method'])
    if params['date_from']:
        queryset = queryset.filter(date__gte=params['date_from'])
    if params['date_to']:
        queryset = queryset.filter(date__lte=params['date_to'])
    if params['reconciled'] in ('yes', 'no'):
        queryset = queryset.filter(reconciled=(params['reconciled'] == 'yes'))

    return queryset, params


def _get_paginated_payments_context(request, queryset=None):
    """Shared filter + paginate + annotate for payments list."""
    if queryset is None:
        queryset, params = _filter_payment_queryset(request)
    else:
        params = {
            'search': request.GET.get('search', '').strip(),
            'direction': request.GET.get('direction', '').strip(),
            'method': request.GET.get('method', '').strip(),
            'date_from': request.GET.get('date_from', '').strip(),
            'date_to': request.GET.get('date_to', '').strip(),
            'reconciled': request.GET.get('reconciled', '').strip(),
        }
    page_number = request.GET.get('page', 1)

    # ── Totals (computed on filtered queryset) ──
    # Note: `.distinct()` + `.aggregate()` is safe here because distinct
    # collapses duplicate rows from JOIN, and SUM runs over the deduped set.
    total_received = queryset.filter(direction='received').aggregate(
        t=Sum('amount')
    )['t'] or Decimal('0')
    total_paid = queryset.filter(direction='paid').aggregate(
        t=Sum('amount')
    )['t'] or Decimal('0')
    total_amount = queryset.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    upi_count = queryset.filter(method='upi').count()
    pending_reconciliation = queryset.filter(reconciled=False).count()

    # ── Pagination ──
    paginator = Paginator(queryset, 15)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    from urllib.parse import urlencode
    active_parts = {k: v for k, v in params.items() if v}
    filters_qs = ('&' + urlencode(active_parts)) if active_parts else ''

    return {
        'payments': page_obj.object_list,
        'page_obj': page_obj,
        'total_received': total_received,
        'total_paid': total_paid,
        'upi_count': upi_count,
        'pending_reconciliation': pending_reconciliation,
        'total_amount': total_amount,
        'filters_qs': filters_qs,
        **params,
    }


def _collect_allocations_from_post(request, payment, existing_invoice_ids=None,
                                   budget=None):
    """
    Parse `allocation_amount_<invoice_id>` fields from POST and create
    PaymentAllocation rows. Returns list of description strings.

    `existing_invoice_ids` — set of invoice ids already allocated
                             (used to skip duplicates when pre-allocated).
    `budget`               — shared _AllocationBudget so invoice + vendor-bill
                             rows together never exceed payment.allocation_pool.
    """
    made = []
    skip_ids = set(existing_invoice_ids or [])
    contact = payment.contact
    if budget is None:
        budget = _payment_budget(payment)

    for key, value in request.POST.items():
        if not key.startswith('allocation_amount_'):
            continue
        if not value or not value.strip():
            continue

        try:
            inv_id = int(key.replace('allocation_amount_', ''))
            amt = _safe_decimal(value, default=Decimal('0'))
        except (ValueError, TypeError):
            continue

        if amt <= 0:
            continue

        try:
            inv = Invoice.objects.get(pk=inv_id, customer=contact)
        except Invoice.DoesNotExist:
            continue

        if inv.pk in skip_ids:
            continue

        alloc_amt = budget.take(min(amt, inv.balance_due))
        if alloc_amt <= 0:
            continue

        PaymentAllocation.objects.create(
            payment=payment,
            invoice=inv,
            amount=alloc_amt,
        )
        made.append(f"{inv.invoice_number} Rs.{alloc_amt}")
        skip_ids.add(inv.pk)

    return made


def get_unpaid_purchases_for_contact(contact):
    """Vendor bills with a balance due for a vendor contact."""
    if contact and contact.contact_type in ('vendor', 'both'):
        return (
            Purchase.objects
            .filter(vendor=contact, paid_amount__lt=F('grand_total'))
            .order_by('date', 'id')
        )
    return Purchase.objects.none()


def _collect_purchase_allocations_from_post(request, payment,
                                            existing_purchase_ids=None,
                                            budget=None):
    """
    Parse `purchase_alloc_<purchase_id>` fields from POST and create
    PurchaseAllocation rows. Returns list of description strings.
    """
    made = []
    skip_ids = set(existing_purchase_ids or [])
    contact = payment.contact
    if budget is None:
        budget = _payment_budget(payment)

    for key, value in request.POST.items():
        if not key.startswith('purchase_alloc_'):
            continue
        if not value or not value.strip():
            continue

        try:
            p_id = int(key.replace('purchase_alloc_', ''))
            amt = _safe_decimal(value, default=Decimal('0'))
        except (ValueError, TypeError):
            continue

        if amt <= 0:
            continue

        try:
            purchase = Purchase.objects.get(pk=p_id, vendor=contact)
        except Purchase.DoesNotExist:
            continue

        if purchase.pk in skip_ids:
            continue

        alloc_amt = budget.take(min(amt, purchase.balance_due))
        if alloc_amt <= 0:
            continue

        PurchaseAllocation.objects.create(
            payment=payment,
            purchase=purchase,
            amount=alloc_amt,
        )
        made.append(f"{purchase.purchase_number} Rs.{alloc_amt}")
        skip_ids.add(purchase.pk)

    return made


# ============================================================
# LIST
# ============================================================
@handle_errors(default_redirect='accounting:payment_list')
def payment_list(request):
    context = _get_paginated_payments_context(request)
    if is_htmx(request):
        return render(request, 'payments/partials/payment_table.html', context)
    return render(request, 'payments/payment_list.html', context)


# ============================================================
# PAYMENT DETAIL MODAL
# ============================================================
def payment_detail_modal(request, pk):
    payment = get_object_or_404(
        Payment.objects.select_related(
            'contact', 'bank_account', 'expense_account',
        ).prefetch_related(
            'allocations__invoice', 'advance_adjustments__invoice',
            'purchase_allocations__purchase',
        ),
        pk=pk,
    )
    allocated_total = sum(
        (a.amount for a in payment.allocations.all()), Decimal('0')
    ) + sum(
        (a.amount for a in payment.advance_adjustments.all()), Decimal('0')
    ) + sum(
        (a.amount for a in payment.purchase_allocations.all()), Decimal('0')
    )
    unallocated = payment.allocation_pool - allocated_total
    return render(request, 'payments/partials/payment_detail_modal.html', {
        'payment': payment,
        'allocated_total': allocated_total,
        'unallocated': unallocated,
    })


# ============================================================
# PAYMENT PRINT (single receipt voucher)
# ============================================================
def payment_print(request, pk):
    payment = get_object_or_404(
        Payment.objects.select_related(
            'contact', 'bank_account', 'expense_account',
        ).prefetch_related(
            'allocations__invoice', 'advance_adjustments__invoice',
            'purchase_allocations__purchase',
        ),
        pk=pk,
    )
    company = CompanyProfile.get_instance()
    logo_exists = bool(
        company.logo and company.logo.name and company.logo.storage.exists(company.logo.name)
    )
    return render(request, 'payments/payment_print.html', {
        'payment': payment,
        'company': company,
        'logo_exists': logo_exists,
    })


# ============================================================
# PAYMENT LIST — PRINT (filtered, no pagination)
# ============================================================
@handle_errors(default_redirect='accounting:payment_list')
def payment_list_print(request):
    queryset, params = _filter_payment_queryset(request)
    # Cap: unfiltered whole-table dumps OOM the 512 MB free-tier dyno.
    payments = list(queryset[:5000])
    company = CompanyProfile.get_instance()

    total_received = sum(
        (p.amount for p in payments if p.direction == 'received'), Decimal('0')
    )
    total_paid = sum(
        (p.amount for p in payments if p.direction == 'paid'), Decimal('0')
    )

    return render(request, 'payments/payment_list_print.html', {
        'payments': payments,
        'company': company,
        'total_received': total_received,
        'total_paid': total_paid,
        **params,
    })


# ============================================================
# PAYMENT LIST — EXCEL EXPORT (filtered)
# ============================================================
@handle_errors(default_redirect='accounting:payment_list')
def payment_list_excel(request):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    queryset, params = _filter_payment_queryset(request)
    queryset = queryset.prefetch_related(
        'advance_adjustments__invoice'
    ).order_by('-date', '-id')[:5000]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Payments"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    title_font = Font(bold=True, size=14, color="1F4E78")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin'),
    )
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    left = Alignment(horizontal='left', vertical='center', wrap_text=True)
    right = Alignment(horizontal='right', vertical='center')
    money_fmt = '#,##0.00'

    headers = [
        'Date', 'Payment ID', 'Direction', 'Contact', 'Amount (Rs.)',
        'Discount (Rs.)', 'Settled (Rs.)', 'Method', 'UPI / Ref', 'Account',
        'Applied To', 'Expense Head', 'Advance', 'Reconciled', 'Description',
    ]
    TOTAL_COLS = len(headers)
    last_col = get_column_letter(TOTAL_COLS)

    ws.merge_cells(f'A1:{last_col}1')
    t = ws.cell(row=1, column=1, value="Payment Register")
    t.font = title_font
    t.alignment = center
    ws.row_dimensions[1].height = 30

    for col_idx, header in enumerate(headers, 1):
        c = ws.cell(row=2, column=col_idx, value=header)
        c.font = header_font
        c.fill = header_fill
        c.border = thin_border
        c.alignment = center
    ws.row_dimensions[2].height = 25

    for row_idx, p in enumerate(queryset, start=3):
        bill_parts = [
            f"{a.invoice.invoice_number}({a.amount})"
            for a in p.allocations.select_related('invoice').all()
        ] + [
            f"{a.purchase.purchase_number}({a.amount})"
            for a in p.purchase_allocations.select_related('purchase').all()
        ]
        applied = ', '.join(bill_parts) or ', '.join(
            f"ADV:{a.invoice.invoice_number}({a.amount})"
            for a in p.advance_adjustments.select_related('invoice').all()
        )
        row_data = [
            p.date.strftime('%d-%m-%Y'),
            p.pk,
            p.get_direction_display(),
            p.contact.name if p.contact else '',
            float(p.amount),
            float(p.discount_amount or 0),
            float(p.settled_amount),
            p.get_method_display(),
            p.upi_ref or p.reference or '',
            p.bank_account.name if p.bank_account else 'Cash',
            applied,
            p.expense_account.name if p.expense_account else '',
            'Yes' if p.is_advance else '',
            'Yes' if p.reconciled else 'Pending',
            p.description or '',
        ]
        for col_idx, value in enumerate(row_data, 1):
            c = ws.cell(row=row_idx, column=col_idx, value=value)
            c.border = thin_border
            if col_idx in (5, 6, 7):
                c.alignment = right
                c.number_format = money_fmt
            elif col_idx in (1, 2, 13, 14):
                c.alignment = center
            else:
                c.alignment = left

    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = max(min(max_len + 3, 45), 10)

    ws.freeze_panes = 'A3'

    from django.utils import timezone
    fname = f"payments_{timezone.localdate().strftime('%Y%m%d')}.xlsx"
    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="{fname}"'
    wb.save(response)
    return response


# ============================================================
# LOAD UNPAID BILLS (HTMX endpoint) — customer invoices OR vendor purchase
# bills, depending on payment direction.
# ============================================================
@require_http_methods(["GET"])
def load_unpaid_invoices(request):
    """Return partial HTML with unpaid invoices/purchases for a contact."""
    contact_id = request.GET.get('contact', '').strip()
    direction = request.GET.get('direction', 'received').strip()
    payment_pk = request.GET.get('payment', '').strip()

    if not contact_id:
        return HttpResponse('')

    try:
        contact = Contact.objects.get(pk=contact_id)
    except (Contact.DoesNotExist, ValueError, TypeError):
        return HttpResponse('')

    payment = None
    if payment_pk:
        try:
            payment = Payment.objects.get(pk=payment_pk)
        except (Payment.DoesNotExist, ValueError, TypeError):
            payment = None

    # The edit helpers expose "existing allocation" capacity for THIS payment.
    # They only apply while the form still points at the same contact and
    # direction — after the user switches either, the new contact's ordinary
    # unpaid bills are what should load.
    use_edit_view = bool(
        payment
        and payment.contact_id == contact.pk
        and payment.direction == direction
    )

    if direction == 'received':
        if use_edit_view:
            unpaid = _unpaid_invoices_for_edit(payment)
        else:
            unpaid = get_unpaid_invoices_for_contact(contact, 'received')
        return render(request, 'payments/partials/unpaid_invoices.html', {
            'contact': contact,
            'unpaid_invoices': unpaid,
            'direction': direction,
            'payment': payment,
        })

    if use_edit_view:
        unpaid_purchases = _unpaid_purchases_for_edit(payment)
    else:
        unpaid_purchases = get_unpaid_purchases_for_contact(contact)
    return render(request, 'payments/partials/unpaid_purchases.html', {
        'contact': contact,
        'unpaid_purchases': unpaid_purchases,
        'direction': direction,
        'payment': payment,
    })


# ============================================================
# CREATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:payment_list',
               htmx_template='payments/payment_form.html')
def payment_create(request, pk=None):
    """
    Create a new payment.

    Prefill support:
      - ?contact=<id>  → prefill contact, set direction from contact type
      - ?invoice=<id>  → prefill contact + amount, auto-allocate on save

    Multi-invoice allocation via POST fields:
      - allocation_amount_<invoice_id> = "123.45"
    """
    initial = {}
    contact_id = request.GET.get('contact')
    invoice_id = request.GET.get('invoice')
    purchase_id = request.GET.get('purchase')
    if pk is not None and not invoice_id:
        invoice_id = pk

    # Prefill from contact query param
    if contact_id:
        try:
            c = Contact.objects.get(pk=contact_id)
            initial['contact'] = c.id
            initial['direction'] = 'received' if c.contact_type in ('customer', 'both') else 'paid'
        except (Contact.DoesNotExist, ValueError, TypeError):
            pass

    # Prefill from invoice
    target_invoice = None
    if invoice_id:
        try:
            target_invoice = Invoice.objects.select_related('customer').get(pk=invoice_id)
            initial['contact'] = target_invoice.customer_id
            initial['direction'] = 'received'
            initial['amount'] = target_invoice.balance_due
        except (Invoice.DoesNotExist, ValueError, TypeError):
            pass

    # Prefill from purchase bill (vendor settlement)
    target_purchase = None
    if purchase_id:
        try:
            target_purchase = Purchase.objects.select_related('vendor').get(pk=purchase_id)
            initial['contact'] = target_purchase.vendor_id
            initial['direction'] = 'paid'
            initial['amount'] = target_purchase.balance_due
        except (Purchase.DoesNotExist, ValueError, TypeError):
            pass

    # ============================================================
    # POST
    # ============================================================
    if request.method == 'POST':
        form = PaymentForm(request.POST)
        if form.is_valid():
            allocation_summary = []

            # ── 0. Duplicate guard ──
            # A second identical entry is nearly always a re-submit after
            # the first one looked like it failed, not a separate receipt.
            if not request.POST.get('confirm_duplicate'):
                duplicates = _find_matching_payments(
                    form.cleaned_data['contact'],
                    form.cleaned_data['direction'],
                    form.cleaned_data['amount'],
                    form.cleaned_data['date'],
                    form.cleaned_data['method'],
                )
                if duplicates.exists():
                    unpaid_invoices, unpaid_purchases = (
                        _posted_allocation_context(
                            request,
                            form.cleaned_data['contact'],
                            form.cleaned_data['direction'],
                        )
                    )
                    context = {
                        'form': form,
                        'payment': None,
                        'target_invoice': target_invoice,
                        'target_purchase': target_purchase,
                        'pre_contact': form.cleaned_data['contact'],
                        'unpaid_invoices': unpaid_invoices,
                        'unpaid_purchases': unpaid_purchases,
                        'duplicate_payments': duplicates,
                    }
                    if is_htmx(request):
                        response = render(
                            request, 'payments/payment_form.html', context)
                        response['HX-Retarget'] = '#mainModalContent'
                        return response
                    return render(
                        request, 'payments/payment_form_page.html', context)

            # ── 1. Save payment atomically ──
            try:
                with transaction.atomic():
                    payment = form.save()

                    if not payment.is_advance and not payment.expense_account_id:
                        # Fields the user actually filled in win over the prefill
                        # fallback: if they typed a different amount for the
                        # ?invoice= / ?purchase= bill, honour it instead of also
                        # auto-allocating the full settled amount.
                        explicit_invoice_ids = {
                            int(k.rsplit('_', 1)[1])
                            for k in request.POST
                            if k.startswith('allocation_amount_')
                            and k.rsplit('_', 1)[1].isdigit()
                            and request.POST.get(k, '').strip()
                        }
                        explicit_purchase_ids = {
                            int(k.rsplit('_', 1)[1])
                            for k in request.POST
                            if k.startswith('purchase_alloc_')
                            and k.rsplit('_', 1)[1].isdigit()
                            and request.POST.get(k, '').strip()
                        }
                        budget = _payment_budget(payment)
                        auto_invoice_ids = []
                        auto_purchase_ids = []

                        # Auto-allocate to target invoice (from ?invoice=)
                        if (target_invoice
                                and target_invoice.customer_id == payment.contact_id
                                and target_invoice.pk not in explicit_invoice_ids):
                            alloc_amt = budget.take(
                                min(payment.allocation_pool, target_invoice.balance_due))
                            if alloc_amt > 0:
                                PaymentAllocation.objects.create(
                                    payment=payment,
                                    invoice=target_invoice,
                                    amount=alloc_amt,
                                )
                                auto_invoice_ids.append(target_invoice.pk)
                                allocation_summary.append(
                                    f"{target_invoice.invoice_number} Rs.{alloc_amt}"
                                )

                        # Auto-allocate to target purchase bill (from ?purchase=)
                        if (target_purchase
                                and target_purchase.vendor_id == payment.contact_id
                                and target_purchase.pk not in explicit_purchase_ids):
                            alloc_amt = budget.take(
                                min(payment.allocation_pool, target_purchase.balance_due))
                            if alloc_amt > 0:
                                PurchaseAllocation.objects.create(
                                    payment=payment,
                                    purchase=target_purchase,
                                    amount=alloc_amt,
                                )
                                auto_purchase_ids.append(target_purchase.pk)
                                allocation_summary.append(
                                    f"{target_purchase.purchase_number} Rs.{alloc_amt}"
                                )

                        # Multi-invoice allocations from POST. Only skip a
                        # target bill when the auto-allocation above actually
                        # created its row — an explicitly typed box for the
                        # target must still be honoured.
                        allocation_summary += _collect_allocations_from_post(
                            request, payment,
                            existing_invoice_ids=auto_invoice_ids,
                            budget=budget,
                        )

                        # Vendor bill allocations from POST
                        allocation_summary += _collect_purchase_allocations_from_post(
                            request, payment,
                            existing_purchase_ids=auto_purchase_ids,
                            budget=budget,
                        )

                    # Runs for advances/direct expenses too: they never allocate,
                    # so a discount on them is rejected here.
                    _assert_discount_matches_allocations(payment)

                    if allocation_summary:
                        payment.update_invoices()
                        payment.update_purchases()
            except _PaymentDiscountTooLarge as exc:
                # Roll back the payment, its allocations and ledger lines; keep
                # the typed allocation boxes so one correction is enough.
                form.add_error(None, str(exc))
                unpaid_invoices, unpaid_purchases = _posted_allocation_context(
                    request, form.cleaned_data['contact'],
                    form.cleaned_data['direction'])
                context = {
                    'form': form,
                    'payment': None,
                    'target_invoice': target_invoice,
                    'target_purchase': target_purchase,
                    'pre_contact': form.cleaned_data['contact'],
                    'unpaid_invoices': unpaid_invoices,
                    'unpaid_purchases': unpaid_purchases,
                }
                if is_htmx(request):
                    response = render(request, 'payments/payment_form.html', context)
                    response['HX-Retarget'] = '#mainModalContent'
                    return response
                return render(request, 'payments/payment_form_page.html', context)

            logger.info(
                "Payment #%s created by %s (Rs.%s %s). Allocations: %s",
                payment.pk, request.user.username, payment.amount,
                payment.direction, allocation_summary or 'None',
            )

            # ── 2. Notifications OUTSIDE atomic block ──
            try:
                if payment.direction == 'received' and payment.contact:
                    send_notification_to_customer(
                        payment.contact,
                        title=f"Payment Received: Rs.{payment.amount}",
                        message=f"Your payment of Rs.{payment.amount} has been recorded.",
                        link=reverse('customer:customer_payments'),
                        notif_type='success',
                        category='payment',
                        send_email=False,
                    )

                send_notification_to_staff(
                    title=f"New Payment #{payment.pk}",
                    message=(
                        f"{payment.get_direction_display()} of "
                        f"Rs.{payment.amount} with {payment.contact.name}"
                    ),
                    link=reverse('accounting:payment_list'),
                    notif_type='info',
                    category='payment',
                    send_email=False,
                )
            except Exception:
                logger.exception("Payment notification failed for #%s", payment.pk)

            # ── 3. Response ──
            if allocation_summary:
                success_msg = (
                    f'Payment Rs.{payment.amount} recorded. '
                    f'Applied: {", ".join(allocation_summary)}.'
                )
            else:
                success_msg = (
                    f'Payment Rs.{payment.amount} recorded — no invoice '
                    f'applied, so it stays as unapplied credit.'
                )

            if is_htmx(request):
                context = _get_paginated_payments_context(request)
                response = render(request, 'payments/partials/payment_table.html', context)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {
                        'level': 'success',
                        'message': success_msg,
                    },
                    'closeModal': '',
                })
                return response

            messages.success(request, success_msg)
            next_url = _safe_next_url(request)
            if next_url:
                return redirect(next_url)
            return redirect_to_staff('payment_list')

        # ── Invalid form — re-render with full context ──
        if is_htmx(request):
            pre_contact = None
            try:
                pre_contact = (
                    form.data.get('contact')
                    and Contact.objects.get(pk=form.data.get('contact'))
                )
            except (Contact.DoesNotExist, ValueError, TypeError):
                pass

            pre_unpaid, pre_unpaid_purchases = _posted_allocation_context(
                request, pre_contact, form.data.get('direction'))

            response = render(request, 'payments/payment_form.html', {
                'form': form,
                'payment': None,
                'target_invoice': target_invoice,
                'target_purchase': target_purchase,
                'pre_contact': pre_contact,
                'unpaid_invoices': pre_unpaid,
                'unpaid_purchases': pre_unpaid_purchases,
            })
            response['HX-Retarget'] = '#mainModalContent'
            return response

        # Non-HTMX (native submit from pages without #payment-table-container):
        # bounce back to the caller instead of rendering a bare partial page.
        first_error = next(iter(e[0] for e in form.errors.values() if e), None)
        messages.error(request, first_error or "Please correct the payment details.")
        next_url = _safe_next_url(request)
        if next_url:
            return redirect(next_url)
        return redirect_to_staff('payment_list')

    else:
        form = PaymentForm(initial=initial)

    # ============================================================
    # GET render
    # ============================================================
    unpaid_invoices = Invoice.objects.none()
    unpaid_purchases = Purchase.objects.none()
    pre_contact = None

    initial_contact_id = form.initial.get('contact')
    if initial_contact_id:
        try:
            pre_contact = Contact.objects.get(pk=initial_contact_id)
            if form.initial.get('direction') == 'received':
                unpaid_invoices = get_unpaid_invoices_for_contact(pre_contact, 'received')
            elif form.initial.get('direction') == 'paid':
                unpaid_purchases = get_unpaid_purchases_for_contact(pre_contact)
        except (Contact.DoesNotExist, ValueError, TypeError):
            pass

    return render(request, 'payments/payment_form.html' if is_htmx(request)
                  else 'payments/payment_form_page.html', {
        'form': form,
        'payment': None,
        'target_invoice': target_invoice,
        'target_purchase': target_purchase,
        'unpaid_invoices': unpaid_invoices,
        'unpaid_purchases': unpaid_purchases,
        'pre_contact': pre_contact,
    })


# ============================================================
# UPDATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:payment_list',
               htmx_template='payments/payment_form.html')
def payment_update(request, pk):
    """Update an existing payment (with optional allocation reset)."""
    payment = get_object_or_404(Payment, pk=pk)

    if request.method == 'POST':
        # Snapshot what the payment used to point at, so edits that change
        # contact/direction/advance status can release the old bills.
        original_contact_id = payment.contact_id
        original_direction = payment.direction
        original_is_advance = payment.is_advance
        original_expense_id = payment.expense_account_id

        form = PaymentForm(request.POST, instance=payment)
        if form.is_valid():
            affected_invoice_ids = set(
                payment.allocations.values_list('invoice_id', flat=True))
            affected_purchase_ids = set(
                payment.purchase_allocations.values_list('purchase_id', flat=True))
            advance_invoice_ids = set(
                payment.advance_adjustments.values_list('invoice_id', flat=True))
            affected_invoice_ids |= advance_invoice_ids

            try:
                with transaction.atomic():
                    payment = form.save()

                    # The rendered form always carries one field per candidate
                    # bill, so their presence means the POST is authoritative:
                    # a cleared box must remove that allocation. The hidden flag
                    # is still honoured for older clients/tests.
                    reset_flag = (
                        request.POST.get('reset_allocations', '0') == '1'
                        or any(
                            k.startswith('allocation_amount_')
                            or k.startswith('purchase_alloc_')
                            for k in request.POST
                        )
                    )
                    identity_changed = (
                        payment.contact_id != original_contact_id
                        or payment.direction != original_direction
                        or payment.is_advance != original_is_advance
                        or bool(payment.expense_account_id) != bool(original_expense_id)
                    )
                    # Applied advances belong to the old contact/direction. Toggling
                    # is_advance alone must NOT release them — the cash was already
                    # settled against real bills.
                    advance_identity_changed = (
                        payment.contact_id != original_contact_id
                        or payment.direction != original_direction
                        or bool(payment.expense_account_id) != bool(original_expense_id)
                    )

                    # A direct-expense payment never settles invoices/bills, an
                    # advance pools its own cash for later, and a receipt cannot
                    # sit on vendor bills (or vice versa). On any identity change
                    # the old rows are meaningless — drop them and reopen the bills.
                    must_drop = bool(
                        payment.expense_account_id
                        or payment.is_advance
                        or identity_changed
                    )
                    if not must_drop:
                        if payment.direction == 'received' and affected_purchase_ids:
                            must_drop = True
                        elif payment.direction == 'paid' and affected_invoice_ids:
                            must_drop = True

                    if must_drop or reset_flag:
                        # HARD delete — unique constraint (payment, invoice/purchase)
                        # does NOT respect soft-delete; soft rows would block
                        # re-creation.
                        for alloc in list(payment.allocations.all()):
                            alloc.hard_delete()
                        for alloc in list(payment.purchase_allocations.all()):
                            alloc.hard_delete()

                        if advance_identity_changed:
                            for adj in list(payment.advance_adjustments.all()):
                                adj.hard_delete()

                        # Dropped rows no longer show up in payment.update_invoices(),
                        # so the bills they used to settle have to be reopened from
                        # the IDs captured earlier. Without this the stale
                        # balance_due also clamped the fresh allocation and an edit
                        # could never fully pay a bill.
                        _recalc_invoices_by_ids(affected_invoice_ids)
                        _recalc_purchases_by_ids(affected_purchase_ids)

                    # Re-collect only what the (possibly new) form says this
                    # payment settles, sharing one pool across both bill types.
                    allocation_summary = []
                    if not payment.is_advance and not payment.expense_account_id:
                        budget = _payment_budget(payment)
                        allocation_summary = _collect_allocations_from_post(
                            request, payment, budget=budget)
                        allocation_summary += _collect_purchase_allocations_from_post(
                            request, payment, budget=budget)

                    # Advance/expense edits carry no allocations, so a discount
                    # on them fails this check too.
                    _assert_discount_matches_allocations(payment)
                    payment.update_invoices()
                    payment.update_purchases()
            except _PaymentDiscountTooLarge as exc:
                # Undo the whole edit (fields, dropped rows, ledger) and show
                # the reason on the bound form with the typed boxes intact.
                payment.refresh_from_db()
                form.add_error(None, str(exc))
                if is_htmx(request):
                    unpaid_invoices = _attach_posted_allocations(
                        _unpaid_invoices_for_edit(payment),
                        request.POST, 'allocation_amount_',
                    )
                    unpaid_purchases = _attach_posted_allocations(
                        _unpaid_purchases_for_edit(payment),
                        request.POST, 'purchase_alloc_',
                    )
                    response = render(request, 'payments/payment_form.html', {
                        'form': form,
                        'payment': payment,
                        'unpaid_invoices': unpaid_invoices,
                        'unpaid_purchases': unpaid_purchases,
                        'pre_contact': payment.contact,
                    })
                    response['HX-Retarget'] = '#mainModalContent'
                    return response
                return render(request, 'payments/payment_form_page.html', {
                    'form': form,
                    'payment': payment,
                    'unpaid_invoices': _unpaid_invoices_for_edit(payment),
                    'unpaid_purchases': _unpaid_purchases_for_edit(payment),
                    'pre_contact': payment.contact,
                })

            logger.info("Payment #%s updated by %s", pk, request.user.username)

            # Say what the edit actually settled. A cleared allocation box
            # leaves money on the payment, and without this the invoice
            # looked unpaid while the customer balance showed settled.
            if payment.is_advance:
                update_msg = (
                    'Payment updated. The amount stays available as advance.')
            elif payment.expense_account_id:
                update_msg = 'Payment updated.'
            elif allocation_summary:
                update_msg = (
                    f'Payment updated. Applied: {", ".join(allocation_summary)}.'
                )
                unapplied = _payment_budget(payment).remaining
                if unapplied > 0:
                    update_msg += (
                        f' Rs.{unapplied} is not applied to any bill.'
                    )
            else:
                update_msg = (
                    f'Payment updated — no bill applied, so the amount '
                    f'stays as unapplied credit.'
                )

            # Notifications OUTSIDE atomic
            try:
                if payment.direction == 'received' and payment.contact:
                    send_notification_to_customer(
                        payment.contact,
                        title=f"Payment Updated: Rs.{payment.amount}",
                        message=f"Your payment of Rs.{payment.amount} has been updated.",
                        link=reverse('customer:customer_payments'),
                        notif_type='info',
                        category='payment',
                        send_email=False,
                    )
            except Exception:
                logger.exception("Payment update notification failed for #%s", pk)

            if is_htmx(request):
                context = _get_paginated_payments_context(request)
                response = render(request, 'payments/partials/payment_table.html', context)
                response['HX-Trigger'] = json.dumps({
                    'showToast': {'level': 'success', 'message': update_msg},
                    'closeModal': '',
                })
                return response

            messages.success(request, update_msg)
            return redirect_to_staff('payment_list')

        # Invalid form — re-render with full context
        if is_htmx(request):
            unpaid_invoices = _attach_posted_allocations(
                _unpaid_invoices_for_edit(payment),
                request.POST, 'allocation_amount_',
            )
            unpaid_purchases = _attach_posted_allocations(
                _unpaid_purchases_for_edit(payment),
                request.POST, 'purchase_alloc_',
            )
            response = render(request, 'payments/payment_form.html', {
                'form': form,
                'payment': payment,
                'unpaid_invoices': unpaid_invoices,
                'unpaid_purchases': unpaid_purchases,
                'pre_contact': payment.contact,
            })
            response['HX-Retarget'] = '#mainModalContent'
            return response

    else:
        form = PaymentForm(instance=payment)

    # GET: render edit form
    unpaid_invoices = _unpaid_invoices_for_edit(payment)
    unpaid_purchases = _unpaid_purchases_for_edit(payment)

    return render(request, 'payments/payment_form.html' if is_htmx(request)
                  else 'payments/payment_form_page.html', {
        'form': form,
        'payment': payment,
        'target_invoice': None,
        'unpaid_invoices': unpaid_invoices,
        'unpaid_purchases': unpaid_purchases,
        'pre_contact': payment.contact,
    })


def _unpaid_purchases_for_edit(payment):
    """
    Return vendor bills for the edit form:
      - bills with a balance due for the vendor, PLUS
      - already-allocated bills (even if fully settled)

    Each row carries ``existing_allocation`` and ``edit_available`` (= current
    due + what this payment already applies). The edit box must be able to hold
    the existing value; using ``balance_due`` as the max was what clamped a
    fully-allocated bill down to its ₹0 remainder on every open.
    """
    if not payment.contact or payment.direction != 'paid':
        return []
    if payment.is_advance or payment.expense_account_id:
        return []

    existing = {
        row['purchase_id']: row['total']
        for row in payment.purchase_allocations
        .values('purchase_id')
        .annotate(total=Sum('amount'))
    }

    rows = list(
        Purchase.objects
        .filter(vendor=payment.contact)
        .filter(Q(paid_amount__lt=F('grand_total')) | Q(pk__in=existing))
        .order_by('date', 'id')
    )
    for row in rows:
        row.existing_allocation = existing.get(row.pk, Decimal('0'))
        row.edit_available = (row.balance_due + row.existing_allocation).quantize(
            TAX_PRECISION)
    return rows


def _unpaid_invoices_for_edit(payment):
    """
    Return invoices for the edit form:
      - unpaid/partial invoices for the customer, PLUS
      - already-allocated invoices (even if fully paid)

    See ``_unpaid_purchases_for_edit`` for why ``edit_available`` is exposed.
    """
    if not payment.contact or payment.direction != 'received':
        return []

    existing = {
        row['invoice_id']: row['total']
        for row in payment.allocations
        .values('invoice_id')
        .annotate(total=Sum('amount'))
    }

    rows = list(
        Invoice.objects
        .filter(customer=payment.contact)
        .filter(Q(balance_due__gt=0) | Q(pk__in=existing))
        .order_by('date', 'id')
    )
    for row in rows:
        row.existing_allocation = existing.get(row.pk, Decimal('0'))
        row.edit_available = (row.balance_due + row.existing_allocation).quantize(
            TAX_PRECISION)
    return rows


# ============================================================
# DELETE
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:payment_list')
def payment_delete(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    contact = payment.contact
    amount = payment.amount
    direction = payment.direction
    reopened_invoices = set(
        payment.allocations.values_list('invoice_id', flat=True)
    ) | set(
        payment.advance_adjustments.values_list('invoice_id', flat=True)
    )
    reopened_purchases = set(
        payment.purchase_allocations.values_list('purchase_id', flat=True))
    reopened = len(reopened_invoices) + len(reopened_purchases)

    # Model cleanup re-adjusts invoices/purchases/ledger across tables —
    # must be one all-or-nothing unit.
    with transaction.atomic():
        payment.delete()  # model-level cleanup
    logger.info("Payment #%s deleted by %s", pk, request.user.username)

    delete_msg = (
        f'Payment deleted — {reopened} bill{"s" if reopened != 1 else ""} '
        f'reopened.'
        if reopened else 'Payment deleted.'
    )

    # Notify staff AFTER delete
    try:
        send_notification_to_staff(
            title=f"Payment Deleted: Rs.{amount}",
            message=(
                f"{direction} payment of Rs.{amount} from "
                f"{contact.name if contact else 'Unknown'} was deleted."
            ),
            link=reverse('accounting:payment_list'),
            notif_type='warning',
            category='payment',
            send_email=False,
        )
    except Exception:
        logger.exception("Payment deletion notification failed for #%s", pk)

    if is_htmx(request):
        context = _get_paginated_payments_context(request)
        response = render(request, 'payments/partials/payment_table.html', context)
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': delete_msg}
        })
        return response

    messages.success(request, delete_msg)
    return redirect_to_staff('payment_list')


# ============================================================
# RECONCILE TOGGLE — LIGHTWEIGHT
# ============================================================
@csrf_protect
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:payment_list')
def reconcile_payment(request, pk):
    """
    Toggle reconciliation status — LIGHTWEIGHT.

    Uses queryset.update() + manual bank txn sync to avoid rebuilding
    ledger entries / allocations / contact balances. Previously this
    called payment.save() which rebuilt everything on every toggle.
    """
    payment = get_object_or_404(Payment, pk=pk)

    # Accept explicit target state if provided (avoids double-toggle races)
    target = request.POST.get('reconciled')
    if target in ('true', '1', 'on'):
        new_value = True
    elif target in ('false', '0', 'off'):
        new_value = False
    else:
        new_value = not payment.reconciled

    # Fast DB update — no signals, no ledger rebuild
    Payment.objects.filter(pk=pk).update(reconciled=new_value)

    # Mirror to bank transaction (lightweight single-row update)
    BankTransaction.objects.filter(payment_id=pk).update(reconciled=new_value)

    logger.info(
        "Payment #%s reconciliation toggled to %s by %s",
        pk, new_value, request.user.username,
    )

    if is_htmx(request):
        context = _get_paginated_payments_context(request)
        response = render(request, 'payments/partials/payment_table.html', context)
        response['HX-Trigger'] = json.dumps({
            'showToast': {
                'level': 'info',
                'message': f"Payment #{payment.pk} reconciliation updated.",
            }
        })
        return response

    messages.success(request, "Reconciliation status updated.")
    return redirect_to_staff('payment_list')