# accounting/middleware.py
"""
Role-based access control middleware — Production Grade.

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

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.urls import Resolver404, resolve

logger = logging.getLogger(__name__)


class AccessControlMiddleware:

    PUBLIC_URL_NAMES = frozenset({
        'home', 'privacy_policy', 'cookie_policy',
        'contact_message', 'validate_contact_field',
        'login', 'register', 'logout',
        'password_reset', 'password_reset_done',
        'password_reset_complete', 'password_reset_confirm',
        'password_reset_otp',
        'verify_otp', 'resend_otp', 'reset_password_set',
        'validate_login_field', 'validate_register_field',
        'health_check',
    })

    PUBLIC_NAMESPACES = frozenset({'account'})

    PUBLIC_PATH_PREFIXES = (
        '/admin/login/',
        '/auth/validate-',
        '/events/',              # django-eventstream SSE
    )

    def __init__(self, get_response):
        self.get_response = get_response
        self.static_url = self._normalize_url(settings.STATIC_URL)
        self.media_url = self._normalize_url(getattr(settings, 'MEDIA_URL', ''))

    def __call__(self, request):
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

        # 10. Everything else
        return self.get_response(request)

    # ────────────────────────────────────────────────
    @staticmethod
    def _normalize_url(url: str) -> str:
        """Ensure exactly one leading and trailing slash."""
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