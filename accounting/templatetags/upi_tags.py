"""Template helpers for the UPI QR / pay-now surfaces."""
from __future__ import annotations

from django import template

from ..utils.upi import build_upi_uri, invoice_upi_options, qr_data_uri

register = template.Library()


def _company(company):
    """The context processor already supplies `company` on every page; fall
    back to a direct read for templates rendered outside that pipeline."""
    if company is not None:
        return company
    from ..models import CompanyProfile
    return CompanyProfile.get_instance()


@register.simple_tag
def upi_options(invoice, company=None):
    """
    {% upi_options invoice company as upi %}

    dict with amount / vpa / payee_name and exact + flexible (uri, qr) pairs,
    or None when UPI is not configured or the invoice is already settled.
    """
    return invoice_upi_options(invoice, _company(company))


@register.simple_tag
def upi_qr(company=None, amount=None, ref=None, note=None):
    """QR data URI for an arbitrary amount. amount=None → payer types it."""
    return qr_data_uri(
        build_upi_uri(_company(company), amount=amount, txn_ref=ref,
                      note=note))


@register.simple_tag
def upi_link(company=None, amount=None, ref=None, note=None):
    """Raw upi:// link — used for tap-to-pay buttons on phones."""
    return build_upi_uri(_company(company), amount=amount, txn_ref=ref,
                         note=note)
