# accounting/views/analytics.py
"""
Staff page: website visitor analytics. Public endpoint: visit "ping"
(beacon from the browser that sharpens the exit time of a visit).
Staff can also delete visits here — from one row up to the whole history.
"""

import logging
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.http import (
    HttpResponse, JsonResponse, HttpResponseNotAllowed,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_POST

from ..analytics_middleware import VISITOR_COOKIE
from ..models import VisitorSession, PageView

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
DURATION_SAMPLE = 2000
CLEAR_OLDER_CHOICES = (7, 30, 90)


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
        # Staff/bot browsers send the beacon without a visitor cookie —
        # 204 keeps their consoles clean without logging a visit.
        return HttpResponse(status=204)

    gap = timezone.now() - timedelta(minutes=30)
    session = VisitorSession.objects.filter(
        visitor_id=visitor_id, last_seen_at__gte=gap,
    ).order_by('-last_seen_at').first()
    if not session:
        return HttpResponse(status=204)

    session.last_seen_at = timezone.now()
    session.save(update_fields=['last_seen_at'])
    return JsonResponse({'ok': True})


def _sessions_window(days):
    return VisitorSession.objects.filter(
        started_at__gte=timezone.now() - timedelta(days=days))


def _selected_days(value):
    try:
        days = int(value)
    except (TypeError, ValueError):
        return 30
    return days if days in (7, 30, 90) else 30


def visitor_analytics(request):
    """Staff-only Visitors page (AccessControlMiddleware enforces staff)."""
    days = _selected_days(request.GET.get('days'))

    now = timezone.now()
    today_start = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    today_qs = VisitorSession.objects.filter(started_at__gte=today_start)
    week_qs = _sessions_window(7)

    # One aggregate per range instead of separate count/distinct round-trips.
    today_stats = today_qs.aggregate(
        visits=Count('id'), unique=Count('visitor_id', distinct=True))
    week_stats = week_qs.aggregate(
        visits=Count('id'), unique=Count('visitor_id', distinct=True))

    # Bounded sample keeps the average honest without loading a whole week
    # of sessions once the table grows into the tens of thousands.
    pairs = list(
        week_qs.order_by('-started_at')
        .values_list('started_at', 'last_seen_at')[:DURATION_SAMPLE]
    )
    durations = [max(0, (last - start).total_seconds()) for start, last in pairs]
    avg_duration = (sum(durations) / len(durations)) if durations else 0
    _m, _s = divmod(int(avg_duration), 60)
    avg_duration_display = f"{_m}m {_s}s" if _m else f"{_s}s"

    daily = list(
        _sessions_window(min(days, 90))
        .annotate(d=TruncDate('started_at'))
        .values('d')
        .annotate(visits=Count('id'), unique=Count('visitor_id', distinct=True))
        .order_by('d')
    )

    top_pages = [
        {'page': friendly_path(p['path']), 'raw': p['path'],
         'views': p['views']}
        for p in (
            PageView.objects
            .filter(visited_at__gte=now - timedelta(days=days))
            .values('path')
            .annotate(views=Count('id'))
            .order_by('-views')[:10]
        )
    ]

    sessions_qs = (
        _sessions_window(days)
        .annotate(num_pages=Count('pageviews'))
        .order_by('-started_at')
    )
    paginator = Paginator(sessions_qs, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get('page'))
    session_rows = [{
        's': s,
        'entry': friendly_path(s.entry_path),
        'exit': friendly_path(s.exit_path),
    } for s in page_obj]

    cleanup_total = VisitorSession.objects.count()
    oldest = VisitorSession.objects.order_by('started_at').values_list(
        'started_at', flat=True).first()

    context = {
        'days': days,
        'today_visits': today_stats['visits'],
        'today_unique': today_stats['unique'],
        'week_visits': week_stats['visits'],
        'week_unique': week_stats['unique'],
        'avg_duration_display': avg_duration_display,
        'active_now': VisitorSession.objects.filter(
            last_seen_at__gte=now - timedelta(minutes=5)).count(),
        'daily': daily,
        'top_pages': top_pages,
        'session_rows': session_rows,
        'page_obj': page_obj,
        'session_total': paginator.count,
        'cleanup_total': cleanup_total,
        'oldest_visit': oldest,
        'clear_older_choices': CLEAR_OLDER_CHOICES,
    }
    return render(request, 'dashboard/visitor_analytics.html', context)


@login_required
@csrf_protect
@require_POST
def visitor_cleanup(request):
    """Delete visitor analytics rows from the visitor page.

    Never touches books: only VisitorSession rows (and their page views,
    by cascade) are removed. Actions:
      delete_selected — ids from the row/bulk checkboxes
      delete_older    — every visit that started more than N days ago
      delete_all      — the whole visitor history (typed confirmation)
    """
    days = _selected_days(request.POST.get('days'))
    page = request.POST.get('page') or ''
    action = request.POST.get('action', '')
    back = {'days': days}
    if page.isdigit():
        back['page'] = page
    back_url = f"{reverse('accounting:visitor_analytics')}?{urlencode(back)}"

    if action == 'delete_selected':
        raw_ids = request.POST.getlist('ids')
        ids = [i for i in raw_ids if str(i).isdigit()]
        if not ids:
            messages.warning(request, 'Select at least one visit to delete.')
            return redirect(back_url)
        deleted, _details = VisitorSession.objects.filter(pk__in=ids).delete()
        messages.success(
            request,
            f'{len(ids)} selected visit(s) and their page views were deleted.')
        return redirect(back_url)

    if action == 'delete_older':
        try:
            older = int(request.POST.get('older_days', ''))
        except (TypeError, ValueError):
            older = 0
        if older not in CLEAR_OLDER_CHOICES:
            messages.error(request, 'Choose a valid age for the cleanup.')
            return redirect(back_url)
        cutoff = timezone.now() - timedelta(days=older)
        rows = VisitorSession.objects.filter(started_at__lt=cutoff)
        count = rows.count()
        if count:
            rows.delete()
        messages.success(
            request,
            f'Deleted {count} visit(s) older than {older} days '
            '(their page views were removed too).')
        return redirect(back_url)

    if action == 'delete_all':
        if request.POST.get('confirm_text', '').strip().upper() != 'DELETE':
            messages.error(
                request,
                'To clear the whole visitor history, type DELETE in the '
                'confirmation box — nothing was deleted.')
            return redirect(back_url)
        count = VisitorSession.objects.count()
        if count:
            VisitorSession.objects.all().delete()
        messages.success(
            request,
            f'Cleared the whole visitor history — {count} visit(s) deleted. '
            'Books and customer records were not touched.')
        return redirect(back_url)

    messages.error(request, 'Unknown cleanup action — nothing was deleted.')
    return redirect(back_url)
