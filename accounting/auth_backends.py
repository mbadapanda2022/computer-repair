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
    Supports login via email, username, OR phone number.
    All matching accounts are collected; the password decides which one
    is used (tolerates duplicate Contact/Email rows in real shop data).
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None

        login_input = username.strip()

        # Collect every account this input could refer to, then let the
        # password decide. Earlier code picked ONE candidate (and .first()
        # over duplicate Contact rows is unordered on Postgres), which made
        # correct credentials fail intermittently.
        candidates = []  # (user, matched_via), deduped by pk

        def _add(user, via):
            if any(u.pk == user.pk for u, _ in candidates):
                return
            candidates.append((user, via))

        # ---- 1. Email (case-insensitive; tolerate duplicate emails) ----
        for user in User.objects.filter(email__iexact=login_input).order_by('pk')[:5]:
            _add(user, 'email')

        # ---- 2. Username (exact, then case-insensitive fallback so
        # 'admin' still finds 'Admin' — users don't remember casing) ----
        user = User.objects.filter(username=login_input).first()
        if user is None and login_input:
            user = User.objects.filter(
                username__iexact=login_input).order_by('pk').first()
        if user is not None:
            _add(user, 'username')

        # ---- 3. Phone (via Contact rows that have a portal account) ----
        # Contact.save() normalises every phone to the last 10 digits and
        # the field is unique, so an exact match on the normalised input is
        # the correct lookup. Rows without a portal account must be skipped
        # — picking one blindly made correct credentials fail.
        phone_10 = _normalize_phone(login_input)
        if phone_10:
            try:
                from .models import Contact
                contacts = (
                    Contact.objects
                    .filter(phone=phone_10)
                    .exclude(user__isnull=True)
                    .select_related('user')
                    .order_by('pk')[:5]
                )
                for contact in contacts:
                    _add(contact.user, 'phone')
            except Exception:
                logger.exception("Phone-based authentication lookup failed")

        # ---- Authentication check ----
        for user, matched_via in candidates:
            if user.check_password(password) and self.user_can_authenticate(user):
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
