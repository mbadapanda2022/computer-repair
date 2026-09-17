# accounting/views/global_search.py
"""
Global search — one query, all modules.

Searches across: Invoices, Purchases, Repairs, Payments, Contacts, Products.
Returns an HTML partial for the navbar dropdown (HTMX).
"""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from ..models import (
    Contact, Invoice, Payment, Product, Purchase, RepairJob,
)


LIMIT_PER_CATEGORY = 5


@login_required
@require_http_methods(["GET"])
def global_search(request):
    q = (request.GET.get('q') or '').strip()

    # Too short — show nothing (avoid noisy queries)
    if len(q) < 2:
        return HttpResponse('')

    invoices = (
        Invoice.objects
        .filter(Q(invoice_number__icontains=q) | Q(customer__name__icontains=q))
        .select_related('customer')
        .order_by('-date')[:LIMIT_PER_CATEGORY]
    )

    purchases = (
        Purchase.objects
        .filter(Q(purchase_number__icontains=q) | Q(vendor__name__icontains=q))
        .select_related('vendor')
        .order_by('-date')[:LIMIT_PER_CATEGORY]
    )

    repairs = (
        RepairJob.objects
        .filter(
            Q(job_number__icontains=q) |
            Q(device_model__icontains=q) |
            Q(serial_number__icontains=q) |
            Q(customer__name__icontains=q)
        )
        .select_related('customer')
        .order_by('-date_in')[:LIMIT_PER_CATEGORY]
    )

    payments = (
        Payment.objects
        .filter(
            Q(reference__icontains=q) |
            Q(upi_ref__icontains=q) |
            Q(contact__name__icontains=q)
        )
        .select_related('contact')
        .order_by('-date')[:LIMIT_PER_CATEGORY]
    )

    contacts = (
        Contact.objects
        .filter(
            Q(name__icontains=q) |
            Q(company_name__icontains=q) |
            Q(phone__icontains=q) |
            Q(email__icontains=q) |
            Q(gstin__icontains=q)
        )
        .order_by('name')[:LIMIT_PER_CATEGORY]
    )

    products = (
        Product.objects
        .filter(Q(name__icontains=q) | Q(hsn_code__icontains=q))
        .order_by('name')[:LIMIT_PER_CATEGORY]
    )

    total = (
        len(invoices) + len(purchases) + len(repairs) +
        len(payments) + len(contacts) + len(products)
    )

    return render(request, 'search/results.html', {
        'q': q,
        'invoices': invoices,
        'purchases': purchases,
        'repairs': repairs,
        'payments': payments,
        'contacts': contacts,
        'products': products,
        'total': total,
    })