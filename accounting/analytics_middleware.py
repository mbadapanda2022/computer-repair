# accounting/analytics_middleware.py
"""
Built-in visitor analytics: records page views and visit sessions for HTML
GET requests. Anonymous only — identified by a random cookie ID, never
linked to user accounts. Recording failures must never break a page, so
everything is wrapped in try/except.
"""

import logging
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

VISITOR_COOKIE = 'anid'
SESSION_GAP = timedelta(minutes=30)
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

BOT_RE = re.compile(
    r'bot|crawler|spider|slurp|headless|curl|wget|python-requests|postman',
    re.I,
)

SKIP_PATH_PREFIXES = ('/analytics/', '/admin/', '/ckeditor5/', '/health')


def parse_user_agent(ua: str):
    """Return (device_type, browser, os_name) or None for bots."""
    if not ua or BOT_RE.search(ua):
        return None
    ua_l = ua.lower()

    if 'iphone' in ua_l or 'windows phone' in ua_l or ('android' in ua_l and 'mobile' in ua_l):
        device = 'mobile'
    elif 'ipad' in ua_l or 'tablet' in ua_l or ('android' in ua_l and 'mobile' not in ua_l):
        device = 'tablet'
    else:
        device = 'desktop'

    if 'edg/' in ua_l or 'edge/' in ua_l:
        browser = 'Edge'
    elif 'opr/' in ua_l or 'opera' in ua_l:
        browser = 'Opera'
    elif 'samsungbrowser' in ua_l:
        browser = 'Samsung Internet'
    elif 'firefox' in ua_l or 'fxos' in ua_l:
        browser = 'Firefox'
    elif 'chrome' in ua_l or 'crios' in ua_l:
        browser = 'Chrome'
    elif 'safari' in ua_l:
        browser = 'Safari'
    else:
        browser = 'Other'

    if 'windows nt 10' in ua_l:
        os_name = 'Windows'
    elif 'windows' in ua_l:
        os_name = 'Windows'
    elif 'android' in ua_l:
        os_name = 'Android'
    elif 'iphone' in ua_l or 'ipad' in ua_l or 'ios' in ua_l:
        os_name = 'iOS'
    elif 'mac os x' in ua_l or 'macintosh' in ua_l:
        os_name = 'macOS'
    elif 'linux' in ua_l or 'cros' in ua_l:
        os_name = 'Linux'
    else:
        os_name = 'Other'

    return device, browser, os_name


def client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def _external_referrer(request):
    ref = request.META.get('HTTP_REFERER', '')[:250]
    if not ref:
        return ''
    try:
        from urllib.parse import urlparse
        if urlparse(ref).hostname != request.get_host().split(':')[0]:
            return ref
    except Exception:
        return ''
    return ''


class AnalyticsRecordingMiddleware:
    """Records one PageView per HTML page GET; groups them into visits."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._record(request, response)
        except Exception:
            logger.warning('Visitor analytics recording failed', exc_info=True)
        return response

    def _record(self, request, response):
        if request.method != 'GET':
            return
        if response.status_code != 200:
            return
        ctype = response.get('Content-Type', '')
        if not ctype.startswith('text/html'):
            return
        if getattr(request, 'htmx', None) and request.htmx:
            return
        path = request.path
        if path.startswith(SKIP_PATH_PREFIXES):
            return
        if settings.STATIC_URL and settings.STATIC_URL != '/' and path.startswith(settings.STATIC_URL):
            return
        if settings.MEDIA_URL and settings.MEDIA_URL != '/' and path.startswith(settings.MEDIA_URL):
            return

        ua_info = parse_user_agent(request.META.get('HTTP_USER_AGENT', ''))
        if ua_info is None:
            return  # bot — not counted as a visitor
        device, browser, os_name = ua_info

        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and user.is_staff:
            return  # staff's own browsing is not website traffic

        from .models import VisitorSession, PageView

        vid_raw = request.COOKIES.get(VISITOR_COOKIE, '')
        try:
            visitor_id = uuid.UUID(vid_raw)
        except (ValueError, AttributeError, TypeError):
            visitor_id = uuid.uuid4()

        now = timezone.now()
        session = VisitorSession.objects.filter(
            visitor_id=visitor_id,
            last_seen_at__gte=now - SESSION_GAP,
        ).order_by('-last_seen_at').first()

        is_new = session is None
        if is_new:
            session = VisitorSession(
                visitor_id=visitor_id,
                last_seen_at=now,
                entry_path=path[:200],
                exit_path=path[:200],
                referrer=_external_referrer(request),
                utm_source=(request.GET.get('utm_source', '') or '')[:100],
                device_type=device,
                browser=browser,
                os_name=os_name,
                ip_address=client_ip(request),
            )
            session.started_at = now  # auto_now_add won't fire before save()
            session.save()
        else:
            session.last_seen_at = now
            session.exit_path = path[:200]
            session.save(update_fields=['last_seen_at', 'exit_path'])

        PageView.objects.create(session=session, path=path[:200])

        if is_new:
            response.set_cookie(
                VISITOR_COOKIE, str(visitor_id),
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                samesite='Lax',
                secure=getattr(settings, 'SESSION_COOKIE_SECURE', False),
            )
