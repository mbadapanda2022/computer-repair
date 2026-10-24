# accounting/views/tracking.py
import hashlib
import re
from datetime import timedelta
from decimal import Decimal

from django.shortcuts import render, redirect
from django.http import HttpResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_protect
from django.core.cache import cache
from django.db import models
from django.utils import timezone

from ..models import RepairJob, CompanyProfile, Payment, AdvanceAdjustment
from ..utils.tracking import verify_tracking_token, generate_tracking_token


RATE_LIMIT_PER_MIN = 30
LOOKUP_RATE_LIMIT_PER_MIN = 8

# Advance paid a few days before the device came in still belongs to this job
ADVANCE_LOOKBACK_DAYS = 7

# Progress % shown on the public tracking page
STATUS_PROGRESS = {
    'pending': 10,
    'received': 30,
    'diagnosis': 50,
    'repairing': 70,
    'ready': 90,
    'delivered': 100,
    'cancelled': 0,
}


def _repair_advance_paid(repair):
    """
    Advance related to this repair (in rupees):
      (a) AdvanceAdjustments applied on the invoice, PLUS
      (b) the customer's unapplied advance Payments received around the job
          start (advance at intake, invoice not created yet).
    Soft-deleted payments/adjustment settled amounts are excluded
    automatically (SoftDeleteManager).
    """
    total = Decimal('0')
    if repair.invoice_id:
        total += repair.invoice.advance_adjustments.aggregate(
            t=models.Sum('amount')
        )['t'] or Decimal('0')

    job_start = (
        repair.date_in
        or (repair.submitted_at.date() if repair.submitted_at else None)
        or repair.created_at.date()
    )
    payments = Payment.objects.filter(
        contact_id=repair.customer_id,
        direction='received',
        is_advance=True,
        date__gte=job_start - timedelta(days=ADVANCE_LOOKBACK_DAYS),
        # An advance dated after today may belong to a future job —
        # do not attribute it to this job.
        date__lte=timezone.now().date(),
    )
    settled = {
        row['payment_id']: row['t']
        for row in AdvanceAdjustment.objects.filter(payment__in=payments)
                                .values('payment_id')
                                .annotate(t=models.Sum('amount'))
    }
    for pay in payments:
        remaining = pay.amount - settled.get(pay.pk, Decimal('0'))
        if remaining > 0:
            total += remaining
    return total


@require_http_methods(["GET"])
def repair_track(request, token):
    """
    Public tracking page — no login required.
    Shows only safe fields: status, timeline, device, issue, estimate,
    cost breakdown and payment status. No DB writes. Rate limited.
    """
    # ---- Light rate limit per IP ----
    ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() \
         or request.META.get('REMOTE_ADDR', 'unknown')
    # Hashed key: raw XFF strings can exceed cache key length and collide.
    cache_key = 'track_rl_' + hashlib.sha256(ip.encode()).hexdigest()[:24]
    hits = cache.get(cache_key, 0)
    if hits >= RATE_LIMIT_PER_MIN:
        return HttpResponse(
            '<h3>Too many requests. Please try again in a minute.</h3>',
            status=429,
            content_type='text/html',
        )
    cache.set(cache_key, hits + 1, timeout=60)

    # ---- Verify token ----
    job_id = verify_tracking_token(token)
    if not job_id:
        return render(request, 'tracking/invalid.html', status=404)

    # ---- Fetch repair ----
    try:
        repair = RepairJob.objects.select_related(
            'customer', 'invoice'
        ).prefetch_related(
            'parts__product', 'services__product'
        ).get(pk=job_id)
    except RepairJob.DoesNotExist:
        return render(request, 'tracking/invalid.html', status=404)

    company = CompanyProfile.get_instance()

    context = {
        'repair': repair,
        'company': company,
        'advance_paid': _repair_advance_paid(repair),
        'progress': STATUS_PROGRESS.get(repair.status, 0),
        'show_costs': (
            repair.status in ('ready', 'delivered')
            or repair.invoice_id is not None
        ),
    }
    return render(request, 'tracking/track.html', context)


GENERIC_LOOKUP_ERROR = ('No repair job found for this Job Number + Mobile Number. '
                        'Please check both and try again, or contact us.')


def redirect_to_track(request, token=None, error='', job='', mobile=''):
    """Return to the landing page with the track widget state preserved."""
    from urllib.parse import urlencode
    params = {'track': 1}
    if token:
        params['token'] = token
    if error:
        params['err'] = error
    if job:
        params['job'] = job
    if mobile:
        params['mobile'] = mobile
    return redirect(request.build_absolute_uri('/') + '?' + urlencode(params) + '#track-repair')


@csrf_protect
@require_http_methods(["POST"])
def repair_lookup(request):
    """
    Public lookup from the landing page: Job Number + registered mobile number.
    Works for every customer — registered or created by staff in Contacts.
    On success, redirects to the signed tracking page. No DB writes.
    """
    ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() \
         or request.META.get('REMOTE_ADDR', 'unknown')
    cache_key = 'track_lookup_rl_' + hashlib.sha256(ip.encode()).hexdigest()[:24]
    hits = cache.get(cache_key, 0)
    if hits >= LOOKUP_RATE_LIMIT_PER_MIN:
        return redirect_to_track(request, error='Too many attempts. Please try again in a minute.')
    cache.set(cache_key, hits + 1, timeout=60)

    job_number = (request.POST.get('job_number') or '').strip().upper()
    phone = re.sub(r'\D', '', request.POST.get('phone') or '')
    if phone.startswith('91') and len(phone) == 12:
        phone = phone[2:]

    if not (re.fullmatch(r'[A-Z0-9\-_/]{3,50}', job_number)
            and re.fullmatch(r'\d{10}', phone)):
        return redirect_to_track(
            request,
            error='Enter a valid 10-digit mobile number and your job number (e.g., REP-0001).',
            job=job_number, mobile=phone,
        )

    job = (
        RepairJob.objects
        .filter(models.Q(customer__phone__endswith=phone)
                | models.Q(customer__phone__endswith='91' + phone))
        .filter(models.Q(job_number__iexact=job_number)
                | models.Q(job_number__endswith='-' + job_number))
        .order_by('-created_at')
        .first()
    )

    if not job:
        return redirect_to_track(request, error=GENERIC_LOOKUP_ERROR,
                                 job=job_number, mobile=phone)

    return redirect_to_track(request, token=generate_tracking_token(job))