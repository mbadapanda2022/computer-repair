# accounting/views/analytics.py
"""
Staff page: website visitor analytics. Public endpoint: visit "ping"
(beacon from the browser that sharpens the exit time of a visit).
"""

import logging
from datetime import timedelta

from django.http import JsonResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from ..analytics_middleware import VISITOR_COOKIE
from ..models import VisitorSession, PageView

logger = logging.getLogger(__name__)


def friendly_path(path):
    """'/' → 'Website home'; '/blog/my-post/' → 'Blog › My Post'."""
    if path == '/':
        return 'Website home'
    parts = [p.replace('-', ' ').strip().title()
             for p in path.strip('/').split('/') if p]
    return ' › '.join(parts[:3]) if parts else path


@csrf_exempt
def visitor_ping(request):
    """POST /analytics/ping/ — marks the visitor's current visit as active now.

    csrf_exempt is safe here: the only write is last_seen_at on a session
    already identified by the visitor's own HttpOnly cookie, and we require
    a same-origin Origin/Referer header (what sendBeacon always sends).
    """
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    origin = request.headers.get('Origin') or request.headers.get('Referer') or ''
    allowed = f"{request.scheme}://{request.get_host()}"
    if not origin.startswith(allowed):
        return JsonResponse({'ok': False}, status=403)

    import uuid
    try:
        visitor_id = uuid.UUID(request.COOKIES.get(VISITOR_COOKIE, ''))
    except (ValueError, AttributeError, TypeError):
        return JsonResponse({'ok': False}, status=404)

    gap = timezone.now() - timedelta(minutes=30)
    session = VisitorSession.objects.filter(
        visitor_id=visitor_id, last_seen_at__gte=gap,
    ).order_by('-last_seen_at').first()
    if not session:
        return JsonResponse({'ok': False}, status=404)

    session.last_seen_at = timezone.now()
    session.save(update_fields=['last_seen_at'])
    return JsonResponse({'ok': True})


def _sessions_window(days):
    return VisitorSession.objects.filter(
        started_at__gte=timezone.now() - timedelta(days=days))


def visitor_analytics(request):
    """Staff-only Visitors page (AccessControlMiddleware enforces staff)."""
    days = 30
    try:
        if request.GET.get('days') in ('7', '30', '90'):
            days = int(request.GET['days'])
    except (TypeError, ValueError):
        pass

    now = timezone.now()
    today_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    today_qs = VisitorSession.objects.filter(started_at__gte=today_start)
    week_qs = _sessions_window(7)

    from django.db.models import Count
    from django.db.models.functions import TruncDate

    durations = [
        max(0, (s.last_seen_at - s.started_at).total_seconds())
        for s in week_qs.only('started_at', 'last_seen_at')
    ]
    avg_duration = (sum(durations) / len(durations)) if durations else 0
    _m, _s = divmod(int(avg_duration), 60)
    avg_duration_display = f"{_m}m {_s}s" if _m else f"{_s}s"

    daily = (
        VisitorSession.objects
        .filter(started_at__gte=timezone.now() - timedelta(days=min(days, 90)))
        .annotate(d=TruncDate('started_at'))
        .values('d')
        .annotate(visits=Count('id'), unique=Count('visitor_id', distinct=True))
        .order_by('d')
    )
    daily = list(daily)

    top_pages = [
        {'page': friendly_path(p['path']), 'raw': p['path'],
         'views': p['views']}
        for p in (
            PageView.objects
            .filter(visited_at__gte=timezone.now() - timedelta(days=days))
            .values('path')
            .annotate(views=Count('id'))
            .order_by('-views')[:10]
        )
    ]

    sessions = list(
        _sessions_window(days)
        .annotate(num_pages=Count('pageviews'))
        .order_by('-started_at')[:500]
    )
    session_rows = [{
        's': s,
        'entry': friendly_path(s.entry_path),
        'exit': friendly_path(s.exit_path),
    } for s in sessions]

    context = {
        'days': days,
        'today_visits': today_qs.count(),
        'today_unique': today_qs.values('visitor_id').distinct().count(),
        'week_visits': week_qs.count(),
        'week_unique': week_qs.values('visitor_id').distinct().count(),
        'avg_duration_display': avg_duration_display,
        'active_now': VisitorSession.objects.filter(
            last_seen_at__gte=now - timedelta(minutes=5)).count(),
        'daily': daily,
        'top_pages': top_pages,
        'session_rows': session_rows,
    }
    return render(request, 'dashboard/visitor_analytics.html', context)
