# accounting/views/tracking.py
from django.shortcuts import render
from django.http import HttpResponse
from django.views.decorators.http import require_http_methods
from django.core.cache import cache

from ..models import RepairJob, CompanyProfile
from ..utils.tracking import verify_tracking_token, generate_tracking_token


RATE_LIMIT_PER_MIN = 30


@require_http_methods(["GET"])
def repair_track(request, token):
    """
    Public tracking page — no login required.
    Shows only safe fields: status, timeline, device, amount.
    No DB writes. Rate limited.
    """
    # ---- Light rate limit per IP ----
    ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() \
         or request.META.get('REMOTE_ADDR', 'unknown')
    cache_key = f'track_rl_{ip}'
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
        repair = RepairJob.objects.select_related('customer').get(pk=job_id)
    except RepairJob.DoesNotExist:
        return render(request, 'tracking/invalid.html', status=404)

    company = CompanyProfile.get_instance()

    context = {
        'repair': repair,
        'company': company,
    }
    return render(request, 'tracking/track.html', context)