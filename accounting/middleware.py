# accounting/middleware.py
"""
Role-based access control middleware — Production Grade.

Also acts as the request-context provider for audit logging
(via accounting.audit thread-local storage).

Rules (in order):
─────────────────
1. Static / media always allowed (normalised path prefixes).
2. Explicit public path prefixes bypass URL resolution.
3. URL resolved; url_name in PUBLIC_URL_NAMES → allow.
4. Allauth namespace (account) → allow (allauth handles its own auth).
5. Django admin login → allow; other /admin/ → staff-only.
6. Unauthenticated on protected path → redirect to LOGIN_URL.
7. Customer namespace → any authenticated user allowed.
8. Accounting namespace → staff-only; non-staff → customer dashboard.
9. Everything else → allow (defensive default).
"""

import logging
import time

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.urls import Resolver404, resolve
from django.http import HttpResponse
from django.utils import timezone
from .audit import clear_current_request, set_current_request

logger = logging.getLogger(__name__)


class AccessControlMiddleware:

    PUBLIC_URL_NAMES = frozenset({
        'home', 'privacy_policy', 'cookie_policy',
        'terms_of_service', 'return_policy',
        'demo_guide', 'keepalive', 'book_repair',
        'contact_message', 'validate_contact_field',
        'login', 'register', 'logout',
        'password_reset', 'password_reset_done',
        'password_reset_complete', 'password_reset_confirm',
        'password_reset_otp',
        'verify_otp', 'resend_otp', 'reset_password_set',
        'validate_login_field', 'validate_register_field',
        'health_check',
        'post_list', 'category', 'tag', 'post_detail',
        'sitemap', 'robots_txt',
        'analytics_ping',
    })

    PUBLIC_NAMESPACES = frozenset({'account', 'tracking'})

    PUBLIC_PATH_PREFIXES = (
        '/admin/login/',
        '/auth/validate-',
    )

    def __init__(self, get_response):
        self.get_response = get_response
        self.static_url = self._normalize_url(settings.STATIC_URL)
        self.media_url = self._normalize_url(getattr(settings, 'MEDIA_URL', ''))

    def __call__(self, request):
        # Expose request to signal handlers (audit trail).
        set_current_request(request)
        try:
            response = self._handle(request)
        finally:
            # Thread reuse on Gunicorn → must clear.
            clear_current_request()
        self._prevent_sensitive_caching(request, response)
        return response

    def _prevent_sensitive_caching(self, request, response):
        # Authenticated pages must never come from browser cache: a stale
        # render right after POST->redirect misleads staff, and without
        # no-store a logged-out user's Back button can redisplay them.
        if request.user.is_authenticated and not self._is_static_or_media(request.path):
            response['Cache-Control'] = 'no-store, must-revalidate'
            response['Pragma'] = 'no-cache'

    # ────────────────────────────────────────────────
    def _handle(self, request):
        path = request.path

        # 1. Static / media
        if self._is_static_or_media(path):
            return self.get_response(request)

        # 2. Prefix bypass
        for prefix in self.PUBLIC_PATH_PREFIXES:
            if path.startswith(prefix):
                return self.get_response(request)

        # 3. Resolve URL
        try:
            resolved = resolve(path)
        except Resolver404:
            return self.get_response(request)

        namespace = resolved.namespace
        url_name = resolved.url_name

        # 4. Explicit public URL names
        if url_name in self.PUBLIC_URL_NAMES:
            return self.get_response(request)

        # 5. Allauth namespace — pass-through
        if namespace in self.PUBLIC_NAMESPACES:
            return self.get_response(request)

        # 6. Django admin
        if path == '/admin' or path.startswith('/admin/'):
            return self._handle_admin(request, path)

        # 7. Unauthenticated on protected path
        if not request.user.is_authenticated:
            # HTMX/AJAX request detection
            if request.headers.get('HX-Request') == 'true':
                response = HttpResponse(status=401)
                response['HX-Redirect'] = settings.LOGIN_URL 
                return response
            return redirect_to_login(path, login_url=settings.LOGIN_URL)

        # 8. Customer namespace
        if namespace == 'customer':
            return self.get_response(request)

        # 9. Accounting namespace — staff only
        if namespace == 'accounting':
            if request.user.is_staff:
                return self.get_response(request)
            logger.info(
                "Non-staff blocked | user=%s | path=%s",
                request.user.username, path,
            )
            return self._redirect_customer()

        # 10. CKEditor upload endpoint — staff only (otherwise any portal
        # customer can hit the image-upload API and abuse media storage).
        if path.startswith('/ckeditor5/'):
            if request.user.is_staff:
                return self.get_response(request)
            logger.info(
                "Non-staff blocked (ckeditor) | user=%s | path=%s",
                request.user.username, path,
            )
            return self._redirect_customer()

        # 11. Everything else
        return self.get_response(request)

    @staticmethod
    def _normalize_url(url: str) -> str:
        if not url:
            return ''
        if url.startswith(('http://', 'https://')):
            return url
        return '/' + url.strip('/') + '/'

    def _is_static_or_media(self, path: str) -> bool:
        if self.static_url and path.startswith(self.static_url):
            return True
        if self.media_url and self.media_url != '/' and path.startswith(self.media_url):
            return True
        return False

    def _handle_admin(self, request, path):
        if request.user.is_authenticated and request.user.is_staff:
            return self.get_response(request)
        if request.user.is_authenticated:
            return self._redirect_customer()
        return redirect_to_login(path, login_url=settings.LOGIN_URL)

    @staticmethod
    def _redirect_customer():
        try:
            return redirect('customer:customer_dashboard')
        except Exception:
            return redirect('home')

class DemoTrialMiddleware:
    """DEMO_MODE only: 6-month trial enforcement for the demo package.

    - Expired  -> every page becomes the Trial Expired screen with the
      developer's phone/WhatsApp contact.
    - <=30 days -> a warning banner is injected into HTML responses.
    Production never enters this class (first line short-circuits).
    """

    _PASSTHROUGH_PREFIXES = ('/static/', '/media/', '/favicon')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not getattr(settings, 'DEMO_MODE', False):
            return self.get_response(request)

        path = request.path
        if any(path.startswith(p) for p in self._PASSTHROUGH_PREFIXES):
            return self.get_response(request)

        from .demo_trial import get_trial_state, TRIAL_CONTACT
        state = self._state_cached(get_trial_state)

        if state['expired']:
            from django.shortcuts import render
            return render(request, 'demo/trial_expired.html',
                          {'trial': state, 'contact': TRIAL_CONTACT,
                           'today': timezone.localdate()})

        response = self.get_response(request)

        if state['warn'] and not getattr(response, 'streaming', False) \
                and response.status_code == 200 \
                and 'text/html' in response.get('Content-Type', ''):
            self._inject_banner(response, state)
        return response

    _memo = None
    _memo_at = 0.0

    @classmethod
    def _state_cached(cls, getter):
        now = time.time()
        if cls._memo is None or now - cls._memo_at > 60:
            cls._memo = getter()
            cls._memo_at = now
        return cls._memo

    @staticmethod
    def _inject_banner(response, state):
        from django.template.loader import render_to_string
        from .demo_trial import TRIAL_CONTACT
        html = render_to_string('demo/trial_banner.html',
                                {'trial': state, 'contact': TRIAL_CONTACT})
        body = response.content.decode('utf-8', 'ignore')
        idx = body.rfind('</body>')
        if idx != -1:
            body = body[:idx] + html + body[idx:]
            response.content = body.encode('utf-8')
            response['Content-Length'] = str(len(response.content))
