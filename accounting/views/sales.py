# accounting/views/sales.py
import json
import logging
from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.contrib import messages
from django.db.models import Q, Sum, F
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.utils import timezone
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods
from django.contrib.auth.decorators import login_required
from ..models import *
from ..forms import InvoiceForm, InvoiceItemForm
from accounting.utils.notification_helpers import send_notification_to_customer
from .utils import is_htmx, htmx_response, redirect_to_staff, toast_only_response
from ..decorators import handle_errors

logger = logging.getLogger(__name__)


class _InvoiceEditBlocked(Exception):
    """An edit would leave the invoice in a state that cannot be settled."""


class _InvoiceBelowSettled(_InvoiceEditBlocked):
    """An edit would drop the invoice total below money already on it."""


class _InvoiceDiscountTooLarge(_InvoiceEditBlocked):
    """Discount is bigger than the items it is meant to reduce."""


def _assert_discount_within_subtotal(invoice):
    """Discount is applied to the pre-tax subtotal, so it cannot exceed it.

    ``calculate_totals`` clamps the discounted subtotal at zero, but the tax
    still lands on the bill and the ledger credits the full discount to the
    customer receivable — a ₹5,000 discount on a ₹3,900 bill pushed the
    receivable to −₹1,100 with nothing explaining the credit.
    """
    discount = invoice.discount_amount or Decimal('0')
    if discount > invoice.subtotal:
        raise _InvoiceDiscountTooLarge(
            f'Discount Rs.{discount:.2f} is more than the items subtotal '
            f'Rs.{invoice.subtotal:.2f}. Lower the discount or remove a line.'
        )


def _assert_total_covers_settlements(invoice):
    """Refuse to shrink a bill under the payments/advances it already holds.

    Without this an edit of a fully paid invoice left the allocation above
    the new total and pushed the customer's receivable negative, with no
    invoice showing the credit. The shop must release the payment first.
    """
    settled = (
        invoice.payment_allocations.aggregate(s=Sum('amount'))['s']
        or Decimal('0')
    ) + (
        invoice.advance_adjustments.aggregate(s=Sum('amount'))['s']
        or Decimal('0')
    )
    if settled > invoice.grand_total:
        raise _InvoiceBelowSettled(
            f'{invoice.invoice_number} cannot be reduced to '
            f'Rs.{invoice.grand_total:.2f}: Rs.{settled:.2f} is already '
            f'recorded against it. Remove or reallocate that payment first.'
        )


# ============================================================
# HELPER 1: SESSION ITEM SCOPE + SYNC
# ============================================================
def _invoice_items_key(scope):
    """One line-item cart per invoice (and one for the create form).

    A single global ``temp_invoice_items`` cart meant opening invoice B after
    leaving invoice A still carried A's lines — saving B then replaced B's
    items with A's, and repair-linked lines were duplicated. Purchases already
    scopes its cart this way; sales now matches.
    """
    return f'temp_invoice_items_{scope or "new"}'


def _invoice_items_scope(invoice):
    return invoice.pk if invoice is not None and invoice.pk else 'new'


def _items_total(items):
    """Sum the cart's stored line totals for the form footer."""
    return sum((_safe_decimal(i.get('line_total')) for i in items), Decimal('0'))


def sync_session_items(request, invoice=None, force=False):
    """Ensure the scoped cart holds this invoice's current items.

    On a fresh GET the cart is rebuilt from the database (``force=True``) so
    reopening an edit always reflects what is actually stored; on a failed
    POST the user's unsaved lines are preserved.
    """
    key = _invoice_items_key(_invoice_items_scope(invoice))
    if invoice and (force or key not in request.session):
        # An existing key — even an empty list — is the user's cart: an
        # explicitly emptied cart means "remove every line", not "reload".
        items = []
        for item in invoice.items.select_related('product').all():
            items.append({
                'product_id': item.product.id,
                'product_name': item.product.name,
                'quantity': str(item.quantity),
                'unit_price': str(item.unit_price),
                'tax_rate': str(item.tax_rate),
                'line_total': str(item.line_total),
                'description': item.description or '',
                'db_item_id': item.id,
            })
        request.session[key] = items
    return request.session.get(key, [])


def _apply_invoice_items(invoice, item_rows):
    """Persist the cart onto ``invoice`` without destroying existing rows.

    Update-in-place instead of delete-and-recreate: recreating used to drop
    ``stock_already_deducted`` (repair parts deducted stock a second time) and
    the ``repair_part`` / ``repair_service`` links, and it made every line look
    new in the audit trail. Existing rows keep their identity; only genuinely
    removed lines are deleted (which reverses their stock through
    ``InvoiceItem.delete()``).
    """
    existing = {item.pk: item for item in invoice.items.select_related('product')}
    seen = set()

    for data in item_rows:
        try:
            db_id = int(data.get('db_item_id'))
        except (TypeError, ValueError):
            db_id = None

        item = existing.get(db_id)
        if item is not None and item.pk in seen:
            item = None  # duplicate reference — create a fresh row

        if item is not None and str(item.product_id) != str(data['product_id']):
            # A different product means the old stock deduction must reverse;
            # delete first, then create the replacement below.
            item.delete()
            item = None
            seen.discard(db_id)

        if item is not None:
            item.quantity = Decimal(str(data['quantity']))
            item.unit_price = Decimal(str(data['unit_price']))
            item.tax_rate = Decimal(str(data['tax_rate']))
            item.description = data.get('description', '') or ''
            item.save()
            seen.add(item.pk)
        else:
            InvoiceItem.objects.create(
                invoice=invoice,
                product_id=data['product_id'],
                quantity=Decimal(str(data['quantity'])),
                unit_price=Decimal(str(data['unit_price'])),
                tax_rate=Decimal(str(data['tax_rate'])),
                description=data.get('description', '') or '',
            )

    for pk, item in existing.items():
        if pk not in seen and not item.is_deleted:
            item.delete()


# ============================================================
# HELPER 2: GET PAGINATED INVOICES CONTEXT
# ============================================================
def get_paginated_invoices_context(request, queryset=None):
    if queryset is None:
        queryset = Invoice.objects.select_related('customer').all().order_by('-date')

    search = request.GET.get('search', '').strip()
    customer_id = request.GET.get('customer', '')
    status = request.GET.get('status', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    reset = request.GET.get('reset', '')
    page_number = request.GET.get('page', 1)

    if reset:
        search = customer_id = status = date_from = date_to = ''

    if search:
        queryset = queryset.filter(
            Q(invoice_number__icontains=search) |
            Q(customer__name__icontains=search)
        )
    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)
    if status == 'overdue':
        queryset = queryset.filter(
            payment_status__in=Invoice.OUTSTANDING_STATUSES,
            due_date__lt=timezone.now().date(),
        )
    elif status == 'pending':
        # Dashboard "Pending Invoices" / "Total Due" cards ka exact set.
        queryset = queryset.filter(
            payment_status__in=Invoice.OUTSTANDING_STATUSES
        )
    elif status:
        queryset = queryset.filter(payment_status=status)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    paginator = Paginator(queryset, 20)
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # ============================================================
    # FIX: Batch-prefetch linked RepairJobs for the current page.
    # Without this, `{% if invoice.linked_repair %}` in the template
    # triggers 1 DB query per invoice row (N+1).
    # ============================================================
    page_invoices = list(page_obj.object_list)
    invoice_ids = [inv.pk for inv in page_invoices if inv.pk]
    if invoice_ids:
        repair_map = {
            rj.invoice_id: rj
            for rj in RepairJob.objects.filter(
                invoice_id__in=invoice_ids
            ).only('id', 'invoice_id', 'job_number', 'device_model', 'status')
        }
        for inv in page_invoices:
            inv.linked_repair = repair_map.get(inv.pk)

    customers = Contact.objects.filter(contact_type__in=['customer', 'both']).order_by('name')

    total_sales = queryset.aggregate(total=Sum('grand_total'))['total'] or Decimal('0')
    # Received = money actually collected (allocations + advances), not just
    # invoices that reached "paid" — a partial ₹500 payment on a ₹2,000 bill
    # used to be reported as ₹0 received and the full ₹2,000 as unpaid.
    total_received = queryset.aggregate(total=Sum('paid_amount'))['total'] or Decimal('0')
    total_unpaid = queryset.aggregate(total=Sum('balance_due'))['total'] or Decimal('0')

    context = {
        'invoices': page_invoices,      
        'page_obj': page_obj,
        'customers': customers,
        'search': search,
        'customer_id': customer_id,
        'status': status,
        'date_from': date_from,
        'date_to': date_to,
        'total_sales': total_sales,
        'total_received': total_received,
        'total_unpaid': total_unpaid,
    }
    return context


# ============================================================
# 1. INVOICE LIST
# ============================================================
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_list(request):
    context = get_paginated_invoices_context(request)
    if is_htmx(request):
        return render(request, 'sales/partials/invoice_table.html', context)
    return render(request, 'sales/invoice_list.html', context)


# ============================================================
# 2. PRODUCT SEARCH
# ============================================================
@login_required
def product_search(request):
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return render(request, 'sales/partials/product_suggestions.html', {'products': []})

    # Services are billable on invoices too (model only enforces stock for
    # physical products), so they must appear in the autocomplete.
    products = Product.objects.filter(
        Q(name__icontains=q) | Q(hsn_code__icontains=q),
        is_active=True,
    )[:20]

    def relevance_score(p):
        name = p.name.lower()
        hsn = (p.hsn_code or '').lower()
        q_lower = q.lower()
        if name == q_lower or hsn == q_lower:
            return 0
        if name.startswith(q_lower) or hsn.startswith(q_lower):
            return 1
        if q_lower in name or q_lower in hsn:
            return 2
        return 3

    products = sorted(products, key=relevance_score)[:10]
    return render(request, 'sales/partials/product_suggestions.html', {'products': products})


# ============================================================
# 3. INVOICE CREATE
# ============================================================
def _invoice_create_context(request, form, items_key):
    temp_items = request.session.get(items_key, [])
    return {
        'form': form,
        'items': temp_items,
        'items_total': _items_total(temp_items),
        'items_scope': 'new',
        'products': Product.objects.filter(is_active=True).order_by('name'),
        'customers': Contact.objects.filter(
            contact_type__in=['customer', 'both']).order_by('name'),
        'is_htmx': is_htmx(request),
    }


@csrf_protect
@handle_errors(default_redirect='accounting:invoice_list', htmx_template='sales/partials/invoice_form_modal.html')
def invoice_create(request):
    # ── Fresh open (GET): always reset session items ──
    # This prevents leaking old items from a previously-cancelled form.
    # POST failures keep the session (form re-renders with items intact).
    items_key = _invoice_items_key('new')
    if request.method == 'GET' and request.GET.get('keep') != '1':
        request.session[items_key] = []
    elif items_key not in request.session:
        request.session[items_key] = []

    template_name = 'sales/partials/invoice_form_modal.html' if is_htmx(request) else 'sales/invoice_form.html'

    if request.method == 'POST':
        form = InvoiceForm(request.POST)
        if form.is_valid():
            temp_items = request.session.get(items_key, [])
            # All DB writes in one transaction: a bad item mid-loop must not
            # leave an orphan invoice with partial items/stock movements.
            try:
                with transaction.atomic():
                    invoice = form.save(commit=False)
                    invoice.save()  # Save to get an ID

                    with defer_invoice_sync(invoice):
                        for item_data in temp_items:
                            InvoiceItem.objects.create(
                                invoice=invoice,
                                product_id=item_data['product_id'],
                                quantity=Decimal(item_data['quantity']),
                                unit_price=Decimal(item_data['unit_price']),
                                tax_rate=Decimal(item_data['tax_rate']),
                                description=item_data.get('description', '')
                            )

                    # Calculate totals and save again (this will also sync ledger)
                    invoice.calculate_totals()
                    _assert_discount_within_subtotal(invoice)
                    invoice.save()  # one ledger rebuild for the whole invoice
            except _InvoiceEditBlocked as exc:
                # Roll the half-built invoice back and keep the cart so the
                # user can correct the discount instead of losing the lines.
                form.add_error(None, str(exc))
                return render(request, template_name,
                              _invoice_create_context(request, form, items_key))
            request.session[items_key] = []

            logger.info(f"Invoice {invoice.invoice_number} created by {request.user.username}")

            # ---- Notify customer ----
            try:
                if invoice.customer and invoice.customer.user:
                    send_notification_to_customer(
                        invoice.customer,
                        title=f"New Invoice: {invoice.invoice_number}",
                        message=(
                            f"An invoice of ₹{invoice.grand_total} has been generated for you. "
                            f"Due: ₹{invoice.balance_due}."
                        ),
                        link=reverse('customer:customer_invoice_detail', args=[invoice.pk]),
                        notif_type='info',
                        category='sales',
                        send_email=False,
                    )
            except Exception as notif_err:
                logger.error(f"Invoice notification failed: {notif_err}", exc_info=True)

            if is_htmx(request):
                messages.success(request, f"Invoice {invoice.invoice_number} created successfully.")
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:invoice_detail', args=[invoice.pk])
                return response

            messages.success(request, "Invoice created.")
            return redirect_to_staff('invoice_detail', pk=invoice.pk)

        else:
            if is_htmx(request):
                return render(
                    request, 'sales/partials/invoice_form_modal.html',
                    _invoice_create_context(request, form, items_key))
            return render(request, 'sales/invoice_form.html',
                          _invoice_create_context(request, form, items_key))

    else:
        form = InvoiceForm()
        return render(request, template_name,
                      _invoice_create_context(request, form, items_key))


# ============================================================
# 4. INVOICE UPDATE
# ============================================================
@csrf_protect
@handle_errors(default_redirect='accounting:invoice_list', htmx_template='sales/partials/invoice_form_modal.html')
def invoice_update(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    scope = _invoice_items_scope(invoice)
    items_key = _invoice_items_key(scope)

    if request.method == 'GET':
        # Reopening an edit always reflects what is stored, never a cart left
        # behind by an earlier, abandoned edit.
        sync_session_items(request, invoice, force=True)
    else:
        sync_session_items(request, invoice)

    template_name = 'sales/partials/invoice_form_modal.html' if is_htmx(request) else 'sales/invoice_form.html'

    if request.method == 'POST':
        form = InvoiceForm(request.POST, instance=invoice)
        if form.is_valid():
            # Item sync + totals in one transaction: a mid-loop failure must
            # not leave the invoice half-rewritten.
            try:
                with transaction.atomic():
                    invoice = form.save(commit=False)
                    with defer_invoice_sync(invoice):
                        _apply_invoice_items(
                            invoice,
                            request.session.get(items_key, []),
                        )

                    invoice.calculate_totals()
                    _assert_discount_within_subtotal(invoice)
                    _assert_total_covers_settlements(invoice)
                    invoice.save()  # one ledger rebuild for the whole invoice
            except _InvoiceEditBlocked as exc:
                # Roll back the rewritten lines, keep what the user typed and
                # show the reason instead of leaving an over-paid invoice.
                invoice.refresh_from_db()
                form.add_error(None, str(exc))
                temp_items = request.session.get(items_key, [])
                return render(
                    request,
                    'sales/partials/invoice_form_modal.html' if is_htmx(request)
                    else 'sales/invoice_form.html',
                    {
                        'form': form,
                        'invoice': invoice,
                        'items': temp_items,
                        'items_total': _items_total(temp_items),
                        'items_scope': scope,
                        'products': Product.objects.filter(
                            is_active=True).order_by('name'),
                        'customers': Contact.objects.filter(
                            contact_type__in=['customer', 'both']
                        ).order_by('name'),
                        'is_htmx': is_htmx(request),
                    },
                )
            request.session[items_key] = []

            logger.info(f"Invoice {invoice.invoice_number} updated by {request.user.username}")

            if is_htmx(request):
                messages.success(request, f"Invoice {invoice.invoice_number} updated successfully.")
                response = HttpResponse()
                response['HX-Redirect'] = reverse('accounting:invoice_detail', args=[invoice.pk])
                return response

            messages.success(request, "Invoice updated.")
            return redirect_to_staff('invoice_detail', pk=invoice.pk)

        else:
            temp_items = request.session.get(items_key, [])
            context = {
                'form': form,
                'invoice': invoice,
                'items': temp_items,
                'items_total': _items_total(temp_items),
                'items_scope': scope,
                'products': Product.objects.filter(is_active=True).order_by('name'),
                'customers': Contact.objects.filter(contact_type__in=['customer', 'both']).order_by('name'),
                'is_htmx': is_htmx(request),
            }
            if is_htmx(request):
                return render(request, 'sales/partials/invoice_form_modal.html', context)
            return render(request, 'sales/invoice_form.html', context)

    else:
        form = InvoiceForm(instance=invoice)
        temp_items = request.session.get(items_key, [])
        context = {
            'form': form,
            'invoice': invoice,
            'items': temp_items,
            'items_total': _items_total(temp_items),
            'items_scope': scope,
            'products': Product.objects.filter(is_active=True).order_by('name'),
            'customers': Contact.objects.filter(contact_type__in=['customer', 'both']).order_by('name'),
            'is_htmx': is_htmx(request),
        }
        return render(request, template_name, context)


# ============================================================
# 5. INVOICE DELETE
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_delete(request, pk):
    """
    Delete an invoice cleanly.

    - Blocks if payments are allocated (safety).
    - Unlinks linked repair (does NOT touch its status/delivery).
    - Cleans sales ledger entries.
    - Soft-deletes items and invoice header.
    - Recalculates customer balance.
    - Sends customer notification.

    All steps wrapped in transaction.atomic — either everything
    succeeds or nothing changes.
    """
    invoice = get_object_or_404(Invoice, pk=pk)

    # ── Safety: block delete if payments allocated ──
    if invoice.payment_allocations.exists():
        return toast_only_response(
            {
                'level': 'danger',
                'message': (
                    f'Cannot delete {invoice.invoice_number} — '
                    f'payments are linked. Remove/reallocate payments first.'
                )
            },
            status=400,
        )

    # ── Safety: other records that assume this invoice still exists ──
    # Deleting the header under them left advances pointing at a ghost bill,
    # credit notes with no original, and customer UPI claims unverifiable.
    if invoice.advance_adjustments.exists():
        return toast_only_response(
            {
                'level': 'danger',
                'message': (
                    f'Cannot delete {invoice.invoice_number} — an advance is '
                    f'applied to it. Remove that advance application first.'
                )
            },
            status=400,
        )

    if invoice.credit_notes.exists():
        return toast_only_response(
            {
                'level': 'danger',
                'message': (
                    f'Cannot delete {invoice.invoice_number} — credit notes '
                    f'exist against it. Delete those first.'
                )
            },
            status=400,
        )

    pending_upi = invoice.upi_requests.filter(status='pending').exists()
    if pending_upi:
        return toast_only_response(
            {
                'level': 'danger',
                'message': (
                    f'Cannot delete {invoice.invoice_number} — a customer UPI '
                    f'claim is pending verification. Verify or reject it first.'
                )
            },
            status=400,
        )

    invoice_number = invoice.invoice_number
    customer = invoice.customer
    repair_job = None

    try:
        with transaction.atomic():
            # ── 1. Unlink repair (fast DB update — bypasses save()) ──
            repair_job = RepairJob.objects.filter(invoice=invoice).first()
            if repair_job:
                RepairJob.objects.filter(pk=repair_job.pk).update(invoice=None)
                logger.info(
                    f"Repair {repair_job.job_number} unlinked from "
                    f"invoice {invoice_number}."
                )

            # ── 2. Delete sales ledger entries (lines first, then entry) ──
            for entry in LedgerEntry.objects.filter(
                reference_id=invoice.id, entry_type='sales'
            ):
                for line in list(entry.lines.all()):
                    line.delete()
                entry.delete()

            # ── 3. Soft-delete items (reverses stock) ──
            # The ledger entries are already gone above, so the per-item
            # rebuild must stay off — it would recreate them line by line.
            with defer_invoice_sync(invoice):
                for item in list(invoice.items.all()):
                    item.delete()

            # ── 4. Soft-delete invoice header ──
            invoice.soft_delete()

            # One balance pass for the whole delete; the per-item recalcs
            # are batched away above.
            if customer:
                customer.recalc_balance()
                customer.recalc_advance_balance()

    except Exception as e:
        logger.exception(
            "Invoice delete failed | invoice=%s | error=%s", pk, e
        )
        return toast_only_response(
            {
                'level': 'danger',
                'message': (
                    f'Failed to delete invoice {invoice_number}. '
                    f'No changes were made. Please try again.'
                ),
            },
            status=500,
        )

    logger.info(
        f"Invoice {invoice_number} deleted by {request.user.username}"
    )

    # ── 5. Notify customer (non-blocking) ──
    try:
        if customer and customer.user:
            send_notification_to_customer(
                customer,
                title=f"Invoice Deleted: {invoice_number}",
                message=(
                    f"Invoice {invoice_number} has been removed "
                    f"from your account."
                ),
                link=reverse('customer:customer_invoices'),
                notif_type='warning',
                category='sales',
                send_email=False,
            )
    except Exception:
        logger.exception("Invoice delete notification failed")

    # ── 6. Response ──
    message = f'Invoice {invoice_number} deleted.'
    if repair_job:
        message += f' Repair #{repair_job.job_number} unlinked.'

    if is_htmx(request):
        response = HttpResponse()
        response['HX-Trigger'] = json.dumps({
            'showToast': {'level': 'success', 'message': message},
            'reloadInvoices': '',
        })
        return response

    messages.success(request, message)
    return redirect_to_staff('invoice_list')


# ============================================================
# 6. ADD INVOICE ITEM (SESSION)
# ============================================================

def _safe_decimal(value, default=Decimal('0')):
    """Safely parse a value to Decimal, returning default on empty/invalid."""
    if value is None:
        return default
    value = str(value).strip()
    if value == '':
        return default
    try:
        return Decimal(value)
    except (ValueError, TypeError, ArithmeticError):
        return default


@csrf_protect
def add_invoice_item(request):
    if request.method != 'POST':
        return HttpResponse("Method not allowed", status=405)

    try:
        product_id = request.POST.get('product')
        if not product_id:
            return HttpResponse("Product is required.", status=400)

        product = get_object_or_404(Product, pk=product_id)
        qty = _safe_decimal(request.POST.get('quantity'), Decimal('1'))
        price = _safe_decimal(request.POST.get('unit_price'), product.selling_price or Decimal('0'))
        tax = _safe_decimal(request.POST.get('tax_rate'), product.tax_rate or Decimal('0'))
        scope = request.POST.get('items_scope') or 'new'
        items_key = _invoice_items_key(scope)

        if qty <= 0:
            return HttpResponse("Quantity must be positive.", status=400)
        if price < 0:
            return HttpResponse("Price cannot be negative.", status=400)
        if tax < 0 or tax > 100:
            return HttpResponse("Tax rate must be between 0 and 100.", status=400)

        # Fail fast with a clear message instead of letting the whole invoice
        # save blow up on the stock floor at save time. On an edit, lines that
        # already exist in the DB have already deducted their stock — count
        # only the extra quantity the cart adds.
        if not product.is_service:
            original_qty = {}
            if str(scope).isdigit():
                original_qty = {
                    item.pk: item.quantity
                    for item in InvoiceItem.objects.filter(
                        invoice_id=int(scope), product_id=product.id)
                }
            already_in_cart = Decimal('0')
            for i in request.session.get(items_key, []):
                if str(i['product_id']) != str(product.id):
                    continue
                q = _safe_decimal(i.get('quantity'))
                db_id = i.get('db_item_id')
                try:
                    db_id = int(db_id)
                except (TypeError, ValueError):
                    db_id = None
                if db_id in original_qty:
                    q = max(Decimal('0'), q - original_qty[db_id])
                already_in_cart += q
            needed = qty + already_in_cart
            if product.current_stock < needed:
                return HttpResponse(
                    f"Insufficient stock for '{product.name}': "
                    f"available {product.current_stock}, needed {needed}.",
                    status=400
                )

        line_total = (qty * price * (1 + tax / 100)).quantize(Decimal('0.01'))

        item = {
            'product_id': product.id,
            'product_name': product.name,
            'quantity': str(qty),
            'unit_price': str(price),
            'tax_rate': str(tax),
            'line_total': str(line_total),
            'description': request.POST.get('description', '')
        }

        items = request.session.get(items_key, [])
        items.append(item)
        request.session[items_key] = items

        items_total = sum(Decimal(i['line_total']) for i in items)
        return render(request, 'sales/partials/invoice_items.html', {
            'items': items, 'items_total': items_total,
            'items_scope': scope,
        })

    except Exception as e:
        logger.error(f"Error adding invoice item: {e}")
        return HttpResponse("Error adding item.", status=500)


# ============================================================
# 7. REMOVE INVOICE ITEM (SESSION)
# ============================================================
@csrf_protect
def remove_invoice_item(request, index):
    try:
        scope = request.POST.get('items_scope') or request.GET.get('items_scope') or 'new'
        items_key = _invoice_items_key(scope)
        items = request.session.get(items_key, [])
        idx = int(index)
        if 0 <= idx < len(items):
            items.pop(idx)
            request.session[items_key] = items
        else:
            return HttpResponse("Invalid index.", status=400)

        items_total = sum(
            _safe_decimal(i.get('line_total')) for i in items
        )
        return render(request, 'sales/partials/invoice_items.html', {
            'items': items, 'items_total': items_total,
            'items_scope': scope,
        })

    except (ValueError, IndexError):
        return HttpResponse("Invalid index.", status=400)


# ============================================================
# 8. INVOICE DETAIL
# ============================================================
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related('customer'), pk=pk)
    payment_allocations = invoice.payment_allocations.select_related('payment').all().order_by('-payment__date')
    payments = [pa.payment for pa in payment_allocations]
    advance_adjustments = (
        invoice.advance_adjustments
        .select_related('payment').order_by('-date', '-id')
    )
    unapplied = (
        invoice.customer.unapplied_advance() if invoice.customer else Decimal('0')
    )
    apply_amount = min(unapplied, invoice.balance_due)
    context = {
        'invoice': invoice,
        'payments': payments,
        'payment_allocations': payment_allocations,
        'advance_adjustments': (
            advance_adjustments
        ),
        'advance_applied_total': sum(
            (a.amount for a in advance_adjustments), Decimal('0')
        ),
        'unapplied_advance': unapplied,
        'advance_apply_amount': apply_amount,
        'advance_balance_after_apply': invoice.balance_due - apply_amount,
    }
    return render(request, 'sales/invoice_detail.html', context)


# ============================================================
# 8b. ADVANCE SETTLE (advance → invoice)
# ============================================================
@login_required
@csrf_protect
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_apply_advance(request, pk):
    """Settle the customer's unapplied advance(s) onto this invoice."""
    invoice = get_object_or_404(
        Invoice.objects.select_related('customer'), pk=pk
    )
    applied = invoice.apply_available_advance(
        note=f'Applied by {request.user.username}'
    )
    if applied:
        total = sum(a for _, a in applied)
        messages.success(
            request,
            f"₹{total} advance applied to {invoice.invoice_number}. "
            f"Balance due: ₹{invoice.balance_due}.",
        )
    else:
        available = (
            invoice.customer.unapplied_advance() if invoice.customer
            else Decimal('0')
        )
        if available <= 0:
            messages.info(request, "Customer has no unapplied advance.")
        else:
            messages.info(
                request,
                "Nothing to apply — the invoice balance is already covered.",
            )
    return redirect('accounting:invoice_detail', pk=invoice.pk)


@login_required
@csrf_protect
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_unapply_advance(request, pk):
    """Move advance(s) applied to this invoice back to the customer's pool."""
    invoice = get_object_or_404(
        Invoice.objects.select_related('customer'), pk=pk
    )
    removed = invoice.unapply_advance()
    if removed:
        total = sum(a.amount for a in removed)
        messages.success(
            request,
            f"₹{total} advance moved back to {invoice.customer.name}'s "
            f"advance pool. Balance due: ₹{invoice.balance_due}.",
        )
    else:
        messages.info(request, "No advance was applied to this invoice.")
    return redirect('accounting:invoice_detail', pk=invoice.pk)


# ============================================================
# 9. INVOICE PRINT
# ============================================================
def invoice_print(request, pk):
    invoice = get_object_or_404(
        Invoice.objects.select_related('customer').prefetch_related(
            'payment_allocations__payment',
            'advance_adjustments__payment',
        ),
        pk=pk,
    )
    company = CompanyProfile.get_instance()
    logo_exists = bool(company.logo and company.logo.name and company.logo.storage.exists(company.logo.name))
    gst_breakup = invoice.get_gst_breakup()  # Ensure this method exists; if not, implement
    payment_discount_total = sum(
        ((pa.amount / pa.payment.settled_amount) * pa.payment.discount_amount)
        for pa in invoice.payment_allocations.all()
        if pa.payment and pa.payment.discount_amount and pa.payment.settled_amount
    ) if invoice.payment_allocations.exists() else 0
    context = {
        'invoice': invoice,
        'company': company,
        'logo_exists': logo_exists,
        'gst_breakup': gst_breakup,
        'payment_discount_total': payment_discount_total,
        'advance_applied_total': sum(
            (a.amount for a in invoice.advance_adjustments.all()), 0
        ),
        'cash_received_total': max(
            (invoice.paid_amount or 0)
            - payment_discount_total
            - sum((a.amount for a in invoice.advance_adjustments.all()), 0),
            0,
        ),
    }
    return render(request, 'sales/invoice_print.html', context)

# ============================================================
# INVOICE LIST — PRINT
# ============================================================
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_list_print(request):
    """
    Print-friendly invoice list with current filters — no pagination.

    Reads filters directly from request.GET (not from a paginated context)
    so we can apply batch-prefetch on the FULL result set.
    """
    search = request.GET.get('search', '').strip()
    customer_id = request.GET.get('customer', '')
    status = request.GET.get('status', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    queryset = Invoice.objects.select_related('customer').all().order_by('-date')

    if search:
        queryset = queryset.filter(
            Q(invoice_number__icontains=search) |
            Q(customer__name__icontains=search)
        )
    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)
    if status == 'overdue':
        queryset = queryset.filter(
            payment_status__in=Invoice.OUTSTANDING_STATUSES,
            due_date__lt=timezone.now().date(),
        )
    elif status == 'pending':
        # Dashboard "Pending Invoices" / "Total Due" cards ka exact set.
        queryset = queryset.filter(
            payment_status__in=Invoice.OUTSTANDING_STATUSES
        )
    elif status:
        queryset = queryset.filter(payment_status=status)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    # Materialise list once — avoids re-evaluation.
    # Capped: unfiltered whole-table dumps OOM the 512 MB free-tier dyno.
    invoices_list = list(queryset[:5000])

    # ============================================================
    # Batch-prefetch linked RepairJobs for the FULL list.
    # Mirrors the logic in get_paginated_invoices_context but
    # operates on all rows (no pagination).
    # ============================================================
    invoice_ids = [inv.pk for inv in invoices_list if inv.pk]
    if invoice_ids:
        repair_map = {
            rj.invoice_id: rj
            for rj in RepairJob.objects
                .filter(invoice_id__in=invoice_ids)
                .only('id', 'invoice_id', 'job_number', 'device_model', 'status')
        }
        for inv in invoices_list:
            inv.linked_repair = repair_map.get(inv.pk)

    # Totals over filtered set
    total_sales = queryset.aggregate(
        total=Sum('grand_total')
    )['total'] or Decimal('0')
    total_received = queryset.filter(payment_status='paid').aggregate(
        total=Sum('grand_total')
    )['total'] or Decimal('0')
    total_unpaid = total_sales - total_received

    # Customer name for filter-display header
    customer_name = None
    if customer_id:
        try:
            customer_name = Contact.objects.get(pk=customer_id).name
        except (Contact.DoesNotExist, ValueError, TypeError):
            pass

    company = CompanyProfile.get_instance()

    context = {
        'invoices': invoices_list,
        'company': company,
        'logo_exists': bool(company.logo and company.logo.name),
        'search': search,
        'customer_id': customer_id,
        'customer_name': customer_name,
        'status': status,
        'date_from': date_from,
        'date_to': date_to,
        'total_sales': total_sales,
        'total_received': total_received,
        'total_unpaid': total_unpaid,
    }
    return render(request, 'sales/invoice_list_print.html', context)


# ============================================================
# INVOICE LIST — EXCEL EXPORT
# ============================================================
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_list_excel(request):
    """Export filtered invoices to Excel."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return toast_only_response(
            {'level': 'danger', 'message': 'Openpyxl is not installed.'},
            status=400
        )

    # Reuse filter logic
    queryset = Invoice.objects.select_related('customer').all().order_by('-date')
    search = request.GET.get('search', '').strip()
    customer_id = request.GET.get('customer', '')
    status = request.GET.get('status', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')

    if search:
        queryset = queryset.filter(
            Q(invoice_number__icontains=search) | Q(customer__name__icontains=search)
        )
    if customer_id:
        queryset = queryset.filter(customer_id=customer_id)
    if status == 'overdue':
        queryset = queryset.filter(
            payment_status__in=Invoice.OUTSTANDING_STATUSES,
            due_date__lt=timezone.now().date(),
        )
    elif status == 'pending':
        queryset = queryset.filter(
            payment_status__in=Invoice.OUTSTANDING_STATUSES
        )
    elif status:
        queryset = queryset.filter(payment_status=status)
    if date_from:
        queryset = queryset.filter(date__gte=date_from)
    if date_to:
        queryset = queryset.filter(date__lte=date_to)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoices"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    thin = Side(style='thin', color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center')
    left = Alignment(horizontal='left', vertical='center')
    right = Alignment(horizontal='right', vertical='center')
    money_fmt = '#,##0.00'

    company = CompanyProfile.get_instance()
    ws.merge_cells('A1:H1')
    ws['A1'] = f"{company.name or 'My Company'} — Invoice List"
    ws['A1'].font = Font(bold=True, size=14)
    ws['A1'].alignment = center
    ws.merge_cells('A2:H2')
    ws['A2'] = f"Generated: {timezone.now().strftime('%d-%m-%Y %H:%M')} | Total: {queryset.count()}"
    ws['A2'].alignment = center

    headers = ['Invoice #', 'Date', 'Customer', 'Amount', 'Paid', 'Due',
               'GST Type', 'Status']
    for col, h in enumerate(headers, 1):
        c = ws.cell(row=4, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = center
        c.border = border

    row = 5
    total_amt = Decimal('0')
    total_paid = Decimal('0')
    total_due = Decimal('0')

    for inv in queryset:
        total_amt += inv.grand_total
        total_paid += inv.paid_amount
        total_due += inv.balance_due

        ws.cell(row=row, column=1, value=inv.invoice_number)
        ws.cell(row=row, column=2, value=inv.date.strftime('%d-%m-%Y'))
        ws.cell(row=row, column=3, value=inv.customer.name)
        ws.cell(row=row, column=4, value=float(inv.grand_total)).number_format = money_fmt
        ws.cell(row=row, column=5, value=float(inv.paid_amount)).number_format = money_fmt
        ws.cell(row=row, column=6, value=float(inv.balance_due)).number_format = money_fmt
        ws.cell(row=row, column=7, value=inv.get_gst_type_display())
        ws.cell(row=row, column=8, value=inv.get_payment_status_display())

        for col in range(1, 9):
            ws.cell(row=row, column=col).border = border
            if col in (4, 5, 6):
                ws.cell(row=row, column=col).alignment = right
        row += 1

    # Totals row
    ws.cell(row=row, column=3, value='Totals').font = Font(bold=True)
    ws.cell(row=row, column=4, value=float(total_amt)).number_format = money_fmt
    ws.cell(row=row, column=5, value=float(total_paid)).number_format = money_fmt
    ws.cell(row=row, column=6, value=float(total_due)).number_format = money_fmt
    for col in (4, 5, 6):
        ws.cell(row=row, column=col).font = Font(bold=True)
    for col in range(1, 9):
        ws.cell(row=row, column=col).border = border

    widths = [14, 12, 30, 14, 14, 14, 18, 12]
    for idx, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(idx)].width = w
    ws.freeze_panes = 'A5'

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="invoices_{timezone.now().strftime("%Y%m%d_%H%M%S")}.xlsx"'
    wb.save(response)
    return response


# ============================================================
# 10. INVOICE DUPLICATE (copy invoice + items for fast entry)
# ============================================================
@login_required
@csrf_protect
@require_http_methods(["POST"])
@handle_errors(default_redirect='accounting:invoice_list')
def invoice_duplicate(request, pk):
    """
    Clone an invoice into a fresh draft (same customer, items, discount
    settings). New invoice number, today's date, no payments.
    """
    from django.db import transaction

    source = get_object_or_404(Invoice, pk=pk)

    with transaction.atomic():
        new_invoice = Invoice(
            customer=source.customer,
            date=timezone.now().date(),
            due_date=None,
            gst_type=source.gst_type,
            discount_amount=source.discount_amount,
            discount_type=source.discount_type,
            discount_note=source.discount_note,
            discount_date=None,
            notes=source.notes,
        )

        # Create the header and all lines under one deferral, then sync once:
        # without this the empty header synced (and cleared) a ledger entry
        # before every line was copied, doubling the work on each duplicate.
        with defer_invoice_sync(new_invoice):
            new_invoice.save()
            for item in source.items.all():
                InvoiceItem.objects.create(
                    invoice=new_invoice,
                    product=item.product,
                    description=item.description,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    tax_rate=item.tax_rate,
                )

        new_invoice.calculate_totals()
        new_invoice.save()

    logger.info(
        f"Invoice {source.invoice_number} duplicated as "
        f"{new_invoice.invoice_number} by {request.user.username}"
    )

    if is_htmx(request):
        response = HttpResponse()
        response['HX-Redirect'] = reverse('accounting:invoice_update', args=[new_invoice.pk])
        return response
    messages.success(request, f"Duplicated as {new_invoice.invoice_number}.")
    return redirect_to_staff('invoice_update', pk=new_invoice.pk)


# ============================================================
# 11. INVOICE WHATSAPP SHARE (returns JSON with wa.me URL)
# ============================================================
@login_required
@require_http_methods(["GET"])
def invoice_whatsapp(request, pk):
    """
    Return JSON { url: <wa.me deep-link> } for sharing invoice via WhatsApp.
    Frontend opens it in a new tab.
    """
    invoice = get_object_or_404(Invoice, pk=pk)
    url = invoice.whatsapp_share_url
    if not url:
        return JsonResponse(
            {'error': 'Customer has no phone number on file.'},
            status=400
        )
    return JsonResponse({'url': url})


# ============================================================
# 12. CREDIT NOTES — LIST
# ============================================================
def _credit_notes_context(request):
    """Shared context for CN list + table."""
    search = request.GET.get('search', '').strip()
    customer_id = request.GET.get('customer', '')
    reason = request.GET.get('reason', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    page_number = request.GET.get('page', 1)

    qs = CreditNote.objects.select_related('customer', 'invoice').all().order_by('-date', '-id')

    if search:
        qs = qs.filter(
            Q(credit_note_number__icontains=search) |
            Q(invoice__invoice_number__icontains=search) |
            Q(customer__name__icontains=search)
        )
    if customer_id:
        qs = qs.filter(customer_id=customer_id)
    if reason:
        qs = qs.filter(reason=reason)
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)

    paginator = Paginator(qs, 20)
    try:
        page_obj = paginator.page(page_number)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    total_amount = qs.aggregate(t=Sum('total_amount'))['t'] or Decimal('0')

    return {
        'credit_notes': page_obj.object_list,
        'page_obj': page_obj,
        'customers': Contact.objects.filter(
            contact_type__in=['customer', 'both']
        ).order_by('name'),
        'reason_choices': CreditNote.REASON_CHOICES,
        'search': search,
        'customer_id': customer_id,
        'reason': reason,
        'date_from': date_from,
        'date_to': date_to,
        'total_amount': total_amount,
    }


@handle_errors(default_redirect='accounting:credit_note_list')
def credit_note_list(request):
    context = _credit_notes_context(request)
    if is_htmx(request):
        return render(request, 'sales/partials/credit_note_table.html', context)
    return render(request, 'sales/credit_note_list.html', context)


# ============================================================
# 13. CREDIT NOTES — CREATE (from Invoice)
# ============================================================
@login_required
@csrf_protect
@handle_errors(
    default_redirect='accounting:invoice_list',
    htmx_template='sales/partials/credit_note_form.html',
)
def credit_note_create(request, invoice_pk):
    invoice = get_object_or_404(
        Invoice.objects.select_related('customer'), pk=invoice_pk
    )

    if request.method == 'POST':
        # ── Parse item selections ──
        reason = request.POST.get('reason', 'sales_return')
        notes = request.POST.get('notes', '').strip()
        refund_method = request.POST.get('refund_method', 'credit')
        is_stock_return = request.POST.get('is_stock_return') == 'on'
        cn_date = request.POST.get('date') or timezone.now().date()

        # items: item_<id>=on  qty_<id>=N
        selections = []
        for item in invoice.items.all():
            checked = request.POST.get(f'item_{item.pk}') == 'on'
            if not checked:
                continue
            qty_str = request.POST.get(f'qty_{item.pk}', '').strip()
            if not qty_str:
                continue
            try:
                qty = Decimal(qty_str)
            except (ValueError, TypeError, InvalidOperation):
                continue
            if qty <= 0:
                continue
            if qty > item.quantity:
                messages.error(
                    request,
                    f'Quantity for {item.product.name} cannot exceed {item.quantity}.'
                )
                # fall-through to render error form
                break
            selections.append((item, qty))

        if not selections:
            return htmx_response(
                request,
                'sales/partials/credit_note_form.html',
                context={
                    'invoice': invoice,
                    'error': 'Please select at least one item with quantity > 0.',
                },
                extra_headers={'HX-Retarget': '#mainModalContent'},
                status=400,
            )

        with transaction.atomic():
            cn = CreditNote.objects.create(
                invoice=invoice,
                customer=invoice.customer,
                date=cn_date,
                reason=reason,
                notes=notes,
                refund_method=refund_method,
                is_stock_return=is_stock_return,
                created_by=request.user,
            )

            for item, qty in selections:
                CreditNoteItem.objects.create(
                    credit_note=cn,
                    invoice_item=item,
                    product=item.product,
                    quantity_returned=qty,
                    unit_price=item.unit_price,
                    tax_rate=item.tax_rate,
                )

            cn.calculate_totals()
            cn.save()  # triggers ledger sync

        logger.info(
            f"Credit Note {cn.credit_note_number} created against "
            f"{invoice.invoice_number} by {request.user.username}"
        )

        # Customer notification
        try:
            if invoice.customer and invoice.customer.user:
                send_notification_to_customer(
                    invoice.customer,
                    title=f"Credit Note: {cn.credit_note_number}",
                    message=(
                        f"A credit of ₹{cn.total_amount} has been issued "
                        f"against invoice {invoice.invoice_number}."
                    ),
                    link=reverse('customer:customer_invoices'),
                    notif_type='info',
                    category='sales',
                    send_email=False,
                )
        except Exception as notif_err:
            logger.error(f"CN notification failed: {notif_err}")

        if is_htmx(request):
            response = HttpResponse()
            response['HX-Redirect'] = reverse(
                'accounting:credit_note_detail', args=[cn.pk]
            )
            return response
        return redirect_to_staff('credit_note_detail', pk=cn.pk)

    # GET — show form
    return render(
        request,
        'sales/partials/credit_note_form.html',
        {'invoice': invoice},
    )


# ============================================================
# 14. CREDIT NOTES — DETAIL
# ============================================================
def credit_note_detail(request, pk):
    cn = get_object_or_404(
        CreditNote.objects.select_related('invoice', 'customer'), pk=pk
    )
    context = {
        'credit_note': cn,
        'invoice': cn.invoice,
    }
    return render(request, 'sales/credit_note_detail.html', context)


# ============================================================
# 15. CREDIT NOTES — PRINT
# ============================================================
def credit_note_print(request, pk):
    cn = get_object_or_404(
        CreditNote.objects.select_related('invoice', 'customer'), pk=pk
    )
    company = CompanyProfile.get_instance()
    logo_exists = bool(
        company.logo and company.logo.name and company.logo.storage.exists(company.logo.name)
    )
    return render(request, 'sales/credit_note_print.html', {
        'credit_note': cn,
        'invoice': cn.invoice,
        'company': company,
        'logo_exists': logo_exists,
    })


# ============================================================
# 16. CREDIT NOTES — DELETE
# ============================================================
@csrf_protect
@require_http_methods(["DELETE"])
@handle_errors(default_redirect='accounting:credit_note_list')
def credit_note_delete(request, pk):
    cn = get_object_or_404(CreditNote, pk=pk)
    cn_number = cn.credit_note_number
    customer = cn.customer

    with transaction.atomic():
        # Delete ledger 
        LedgerEntry.objects.filter(
            entry_type='credit_note', reference_id=cn.id
        ).delete()
        # Delete items (their delete() reverses stock)
        cn.items.all().delete()
        # Hard-delete CN row (soft delete would leave ledger out of sync)
        cn.delete()

    logger.info(f"Credit Note {cn_number} deleted by {request.user.username}")

    if is_htmx(request):
        response = HttpResponse()
        response['HX-Trigger'] = json.dumps({
            'showToast': {
                'level': 'success',
                'message': f'Credit Note {cn_number} deleted.',
            },
            'reloadCreditNotes': '',
        })
        return response
    messages.success(request, f'Credit Note {cn_number} deleted.')
    return redirect_to_staff('credit_note_list')


# ============================================================
# 17. CREDIT NOTES — WHATSAPP SHARE URL
# ============================================================
@login_required
@require_http_methods(["GET"])
def credit_note_whatsapp(request, pk):
    cn = get_object_or_404(CreditNote, pk=pk)
    url = cn.whatsapp_share_url
    if not url:
        return JsonResponse(
            {'error': 'Customer has no phone number on file.'}, status=400
        )
    return JsonResponse({'url': url})