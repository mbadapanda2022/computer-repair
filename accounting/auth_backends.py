# accounting/auth_backends.py
"""
Custom authentication backend.

Supports login via:
  - Email (case-insensitive)
  - Username (exact)
  - Phone number (normalized to last 10 digits, matched against Contact)

Security:
─────────
- Failed attempts logged with MASKED username (no PII leak).
- Successful attempts logged with full username (audit trail).
- Client IP recorded on both success & failure.
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

User = get_user_model()
logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════
def _mask_username(value: str) -> str:
    """Return masked username for safe failure logging."""
    if not value:
        return '***'
    if len(value) <= 4:
        return '****'
    return f"{value[:2]}****{value[-2:]}"


def _get_client_ip(request) -> str:
    """Extract client IP, honoring X-Forwarded-For behind proxies."""
    if request is None:
        return 'unknown'
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


def _normalize_phone(value: str) -> str:
    """Keep only digits; return last 10 digits (Indian mobile)."""
    digits = ''.join(filter(str.isdigit, value or ''))
    return digits[-10:] if len(digits) >= 10 else ''


# ════════════════════════════════════════════════════════════
# BACKEND
# ════════════════════════════════════════════════════════════
class EmailOrPhoneBackend(ModelBackend):
    """
    Authenticate using email, username, OR phone number.
    Tried in this order: email -> username -> phone.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None

        login_input = username.strip()
        user = None
        matched_via = None

        # ---- 1. Email (case-insensitive) ----
        try:
            user = User.objects.get(email__iexact=login_input)
            matched_via = 'email'
        except User.DoesNotExist:
            pass
        except User.MultipleObjectsReturned:
            # Duplicate email in DB — fall through to username
            logger.warning(
                "Duplicate email detected in User table: %s",
                _mask_username(login_input),
            )

        # ---- 2. Username (exact) ----
        if user is None:
            try:
                user = User.objects.get(username=login_input)
                matched_via = 'username'
            except User.DoesNotExist:
                pass

        # ---- 3. Phone (via Contact) ----
        if user is None:
            try:
                from .models import Contact
                phone_10 = _normalize_phone(login_input)
                if phone_10:
                    contact = (
                        Contact.objects
                        .filter(phone=phone_10)
                        .select_related('user')
                        .first()
                    )
                    if contact and contact.user:
                        user = contact.user
                        matched_via = 'phone'
            except Exception:
                logger.exception("Phone-based authentication lookup failed")

        # ---- Authentication check ----
        if user and user.check_password(password) and self.user_can_authenticate(user):
            logger.info(
                "Login OK | user=%s | via=%s | ip=%s",
                user.username, matched_via, _get_client_ip(request),
            )
            return user

        # ---- Failure ----
        logger.warning(
            "Login FAILED | input=%s | ip=%s",
            _mask_username(login_input), _get_client_ip(request),
        )
        return None
