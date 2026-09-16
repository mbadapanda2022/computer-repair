# accounting/utils/otp_helpers.py
"""
OTP (One-Time Password) helpers - Production Grade.

Security guarantees:
────────────────────
1. OTPs generated using `secrets` (cryptographically secure RNG).
2. OTP values are NEVER written to any log.
3. Comparison uses constant-time `secrets.compare_digest`.
4. Each OTP is single-use + 10-minute expiry.
5. Email addresses are masked in logs (PII protection).

Public API (unchanged from previous version):
─────────────────────────────────────────────
- generate_otp()               -> str
- send_otp_email(...)          -> bool
- create_and_send_otp(...)     -> bool
- verify_otp(...)              -> User | None
- OTP_EXPIRY_MINUTES, OTP_RESEND_COOLDOWN_SECONDS
"""

import secrets
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from ..models import EmailOTP

logger = logging.getLogger(__name__)
User = get_user_model()


# ════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════
OTP_LENGTH = 6
OTP_MIN = 10 ** (OTP_LENGTH - 1)          # 100000
OTP_MAX = 10 ** OTP_LENGTH                 # 1000000 (exclusive)
OTP_EXPIRY_MINUTES = 10
OTP_RESEND_COOLDOWN_SECONDS = 60

_PURPOSE_DISPLAY = {
    'signup': 'Signup Verification',
    'reset_password': 'Password Reset',
    'change_email': 'Email Change',
}


# ════════════════════════════════════════════════════════════
# INTERNAL HELPERS (Private)
# ════════════════════════════════════════════════════════════
def _normalize_email(email: str) -> str:
    """Lowercase + strip email. Safe on None."""
    return (email or '').strip().lower()


def _mask_email(email: str) -> str:
    """
    Return masked email for safe logging.
    Example: 'john.doe@example.com' -> 'jo***@example.com'
    """
    if not email or '@' not in email:
        return '***'
    local, _, domain = email.partition('@')
    masked_local = (local[:2] + '***') if len(local) > 2 else '***'
    return f"{masked_local}@{domain}"


def _purpose_display(purpose: str) -> str:
    return _PURPOSE_DISPLAY.get(purpose, 'Verification')


# ════════════════════════════════════════════════════════════
# PUBLIC API
# ════════════════════════════════════════════════════════════
def generate_otp() -> str:
    """
    Generate a cryptographically secure 6-digit OTP.

    Returns:
        str: Zero-padded numeric string (e.g. '042315').
    """
    value = secrets.randbelow(OTP_MAX - OTP_MIN) + OTP_MIN
    return str(value)


def send_otp_email(user, email: str, otp: str, purpose: str) -> bool:
    """
    Send OTP via email (HTML + plain-text fallback).

    IMPORTANT: The OTP value is NEVER logged, even on errors.

    Returns:
        bool: True on success, False on failure.
    """
    purpose_display = _purpose_display(purpose)
    subject = f"Your OTP for {purpose_display}"

    context = {
        'user': user,
        'email': email,
        'otp': otp,
        'purpose': purpose_display,
        'expiry_minutes': OTP_EXPIRY_MINUTES,
    }
    plain_message = (
        f"Your OTP for {purpose_display} is: {otp}. "
        f"It expires in {OTP_EXPIRY_MINUTES} minutes."
    )

    try:
        html_message = render_to_string('auth/email_otp.html', context)
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(
            "OTP email sent | purpose=%s | recipient=%s",
            purpose, _mask_email(email),
        )
        return True
    except Exception:
        logger.exception(
            "OTP email FAILED | purpose=%s | recipient=%s",
            purpose, _mask_email(email),
        )
        return False


def create_and_send_otp(user, email: str, purpose: str) -> bool:
    """
    Create a new OTP record and send it via email.

    Behavior:
    ─────────
    1. Delete any expired unused OTPs for (email, purpose).
    2. Generate a fresh OTP and persist it.
    3. Send email.
    4. If email fails, rollback the OTP record (avoid stale DB rows).

    Returns:
        bool: True on success, False otherwise.
    """
    email = _normalize_email(email)

    # 1. Cleanup expired unused rows
    EmailOTP.objects.filter(
        email=email,
        purpose=purpose,
        is_used=False,
        expires_at__lt=timezone.now(),
    ).delete()

    # 2. Persist new OTP
    otp = generate_otp()
    expires_at = timezone.now() + timedelta(minutes=OTP_EXPIRY_MINUTES)

    otp_record = EmailOTP.objects.create(
        user=user,
        email=email,
        otp=otp,
        purpose=purpose,
        expires_at=expires_at,
    )

    # 3. Send email; rollback on failure
    if not send_otp_email(user, email, otp, purpose):
        otp_record.delete()
        return False

    return True


def verify_otp(email: str, otp: str, purpose: str):
    """
    Verify an OTP for a given (email, purpose).

    Security:
    ─────────
    - Constant-time comparison via `secrets.compare_digest`.
    - Atomic single-use marking (prevents race double-use).

    Returns:
        User instance on success, None on failure.
    """
    if not email or not otp or not purpose:
        return None

    email = _normalize_email(email)
    otp = str(otp).strip()

    # Reject malformed OTPs before hitting DB
    if len(otp) != OTP_LENGTH or not otp.isdigit():
        logger.info(
            "OTP verify rejected (malformed) | purpose=%s | email=%s",
            purpose, _mask_email(email),
        )
        return None

    now = timezone.now()

    candidates = EmailOTP.objects.filter(
        email=email,
        purpose=purpose,
        is_used=False,
        expires_at__gte=now,
    )

    matched = None
    for record in candidates:
        # compare_digest requires both args be str (ASCII) or bytes.
        # Encode to bytes to be 100% safe across DB backends.
        if secrets.compare_digest(record.otp.encode('ascii'), otp.encode('ascii')):
            matched = record
            break

    if matched is None:
        logger.info(
            "OTP verify FAILED | purpose=%s | email=%s",
            purpose, _mask_email(email),
        )
        return None

    # Atomically mark used (race-safe)
    with transaction.atomic():
        updated = EmailOTP.objects.filter(
            pk=matched.pk, is_used=False,
        ).update(is_used=True)
        if not updated:
            # Another request already consumed it.
            return None

    logger.info(
        "OTP verify OK | purpose=%s | email=%s",
        purpose, _mask_email(email),
    )
    return matched.user