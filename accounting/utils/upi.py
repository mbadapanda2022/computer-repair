"""
UPI QR generation — no payment gateway.

A UPI QR is just the standard NPCI ``upi://pay?...`` deep link encoded into a
QR image. Any UPI app (PhonePe, GPay, BHIM, Paytm — business or personal) can
scan it, so the shop needs no gateway account, no fees, no API keys and no
webhooks: money lands directly in the shop's own UPI/bank account and the only
system-side action is a staff member confirming it.

Two link shapes are used on purpose:

* with ``am`` — the app pre-fills the amount, which is what we want for
  "pay this invoice's balance" (most apps lock the amount once supplied);
* without ``am`` — the app asks the payer to type the amount, so the same QR
  stays usable for partial payments, advances and over-the-counter sales.
"""
from __future__ import annotations

import base64
import hashlib
import re
from decimal import Decimal, InvalidOperation
from io import BytesIO
from urllib.parse import quote, urlencode

from django.core.cache import cache

import qrcode
from qrcode.constants import ERROR_CORRECT_M

# Same Indian digit grouping the templates use, so the amount a customer reads
# on a screen, on paper and inside a WhatsApp message is one identical string.
from ..templatetags.math_filters import rupee

# QR images are deterministic for a given link, and an invoice page can be
# re-rendered many times while the balance is unchanged.
QR_CACHE_SECONDS = 15 * 60

# NPCI field limits — longer values are truncated rather than produce a QR
# some banks' apps will reject.
_PAYEE_NAME_MAX = 75
_TXN_REF_MAX = 35
_NOTE_MAX = 80

# Control characters and quotes break the link / the QR scanner's parsing.
_STRIP_RE = re.compile(r'[\x00-\x1f\x7f"\'<>]')


def _clean(value, limit):
    text = _STRIP_RE.sub('', str(value or '')).strip()
    return text[:limit].strip()


def _amount_str(amount):
    """UPI expects a plain two-decimal amount (no separators, no currency)."""
    try:
        value = Decimal(str(amount)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return ''
    if value <= 0:
        return ''
    return f'{value:.2f}'


def build_upi_uri(company, amount=None, txn_ref=None, note=None):
    """
    Build the ``upi://pay`` deep link for ``company``.

    Returns '' when the shop has not configured a UPI ID — every caller then
    hides the QR instead of showing a broken one. ``amount=None`` produces the
    editable-amount variant.
    """
    if company is None:
        return ''
    vpa = _clean(getattr(company, 'upi_id', ''), 256)
    if not vpa or '@' not in vpa:
        return ''

    params = [
        ('pa', vpa),
        ('pn', _clean(getattr(company, 'upi_payee_name', '') or company.name,
                      _PAYEE_NAME_MAX) or 'Merchant'),
    ]
    amt = _amount_str(amount)
    if amt:
        params.append(('am', amt))
    if note:
        params.append(('tn', _clean(note, _NOTE_MAX)))
    if txn_ref:
        params.append(('tr', _clean(txn_ref, _TXN_REF_MAX)))
    params.append(('cu', 'INR'))
    return 'upi://pay?' + urlencode(params, quote_via=quote)


def qr_data_uri(uri, box_size=8):
    """Render ``uri`` as an inline base64 PNG data URI (cached)."""
    if not uri:
        return ''
    key = 'upi_qr_' + hashlib.sha256(
        f'{box_size}|{uri}'.encode('utf-8')).hexdigest()[:40]
    cached = cache.get(key)
    if cached:
        return cached

    code = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=box_size,
                         border=2)
    code.add_data(uri)
    code.make(fit=True)
    image = code.make_image(fill_color='#111111', back_color='white')

    buffer = BytesIO()
    image.save(buffer, format='PNG')
    data_uri = ('data:image/png;base64,'
                + base64.b64encode(buffer.getvalue()).decode('ascii'))
    cache.set(key, data_uri, QR_CACHE_SECONDS)
    return data_uri


def invoice_upi_options(invoice, company):
    """
    Everything a pay-now screen needs for one invoice.

    Returns ``None`` when UPI is not configured or the invoice has nothing
    left to pay. ``exact`` carries the balance-locked link/QR and ``flexible``
    the enter-it-yourself variant, so the customer can choose.
    """
    if company is None or not getattr(company, 'accepts_upi', False):
        return None
    if invoice is None:
        return None

    due = Decimal(getattr(invoice, 'balance_due', 0) or 0)
    if due <= 0:
        return None

    ref = getattr(invoice, 'invoice_number', '') or ''
    note = f'Invoice {ref}' if ref else 'Invoice payment'

    exact_uri = build_upi_uri(company, amount=due, txn_ref=ref, note=note)
    flex_uri = build_upi_uri(company, amount=None, txn_ref=ref, note=note)
    if not exact_uri:
        return None

    return {
        'amount': f'{due:.2f}',
        'amount_display': rupee(due),
        'vpa': _clean(company.upi_id, 256),
        'payee_name': _clean(company.upi_payee_name, _PAYEE_NAME_MAX),
        'invoice_number': _clean(ref, _TXN_REF_MAX),
        'exact': {'uri': exact_uri, 'qr': qr_data_uri(exact_uri)},
        'flexible': {'uri': flex_uri, 'qr': qr_data_uri(flex_uri)},
    }
