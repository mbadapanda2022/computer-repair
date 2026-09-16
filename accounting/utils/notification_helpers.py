# accounting/utils/notification_helpers.py
"""
Notification helpers — Production Grade.

Design decisions (Render free tier friendly):
─────────────────────────────────────────────
1. NO real SSE (django-eventstream) — in-process SSE doesn't work with
   multi-worker Gunicorn on Render. Instead we rely on HTMX polling
   in the base templates (every 30-60s).
2. `send_notification_sse()` is kept as a NO-OP stub for backward
   compatibility — it silently does nothing but doesn't break any
   existing imports.
3. Bulk insert notifications to staff (avoids N+1 queries).
4. Content-type detection uses regex fallback ONLY when URL resolver
   fails — safer across refactors.
5. Email is opt-in and fail-safe (never crashes the caller).

Public API:
───────────
- send_notification(user, ...)              -> Notification | None
- send_notification_to_staff(...)           -> list[Notification]
- send_notification_to_customer(contact)    -> Notification | None (alias)
- send_notification_to_contact(contact)     -> Notification | None (primary)
- send_notification_to_all_staff(...)       -> alias
- send_notification_sse(user)               -> None (no-op stub)
- get_unread_count(user)                    -> int
"""

import logging
import re

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import resolve
from django.urls.exceptions import Resolver404

from ..models import Notification

logger = logging.getLogger(__name__)
User = get_user_model()


# ════════════════════════════════════════════════════════════
# CONTENT TYPE DETECTION
# ════════════════════════════════════════════════════════════

_URL_PATTERNS = [
    (re.compile(r'/repairs?/(\d+)/'), 'accounting', 'repairjob'),
    (re.compile(r'/sales/(\d+)/'), 'accounting', 'invoice'),
    (re.compile(r'/invoices?/(\d+)/'), 'accounting', 'invoice'),
    (re.compile(r'/purchases?/(\d+)/'), 'accounting', 'purchase'),
    (re.compile(r'/contacts?/(\d+)/'), 'accounting', 'contact'),
    (re.compile(r'/payments?/(\d+)/'), 'accounting', 'payment'),
]


def _extract_content_type_and_id(link):
    """Best-effort detection of (content_type, object_id) from a URL."""
    if not link:
        return None, None

    # 1. URL resolver
    try:
        resolved = resolve(link)
        pk = resolved.kwargs.get('pk')
        if pk:
            view = resolved.func
            view_class = getattr(view, 'view_class', None)
            model = getattr(view_class, 'model', None) if view_class else None
            if model and hasattr(model, '_meta'):
                return ContentType.objects.get_for_model(model), pk
    except (Resolver404, ValueError, AttributeError):
        pass

    # 2. Regex fallback
    for pattern, app_label, model_name in _URL_PATTERNS:
        match = pattern.search(link)
        if match:
            try:
                model = apps.get_model(app_label, model_name)
                return ContentType.objects.get_for_model(model), int(match.group(1))
            except (LookupError, ValueError):
                pass

    return None, None


# ════════════════════════════════════════════════════════════
# CORE: SEND SINGLE NOTIFICATION
# ════════════════════════════════════════════════════════════

def send_notification(
    user,
    title,
    message,
    link=None,
    notif_type='info',
    category='general',
    send_email=False,
):
    """Create a single Notification row for `user`. Optionally email."""
    if not user or not getattr(user, 'is_authenticated', False):
        return None

    content_type, object_id = _extract_content_type_and_id(link)

    try:
        notif = Notification.objects.create(
            recipient=user,
            title=title,
            message=message,
            link=link or None,
            notification_type=notif_type,
            category=category,
            content_type=content_type,
            object_id=object_id,
        )
    except Exception:
        logger.exception(
            "Notification DB create failed | user_id=%s | title=%s",
            getattr(user, 'id', '?'), title,
        )
        return None

    if send_email and getattr(user, 'email', None):
        _send_notification_email(user, title, message, link)

    return notif


def _send_notification_email(user, title, message, link):
    """Fail-safe email dispatch."""
    try:
        subject = f"[A1 Computer] {title}"
        context = {'user': user, 'title': title, 'message': message, 'link': link}
        html_message = render_to_string(
            'notifications/email_notification.html', context,
        )
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            html_message=html_message,
            fail_silently=True,
        )
    except Exception:
        logger.exception(
            "Notification email failed | user_id=%s",
            getattr(user, 'id', '?'),
        )


# ════════════════════════════════════════════════════════════
# BULK: STAFF NOTIFICATIONS
# ════════════════════════════════════════════════════════════

def send_notification_to_staff(
    title, message, link=None,
    notif_type='info', category='general', send_email=False,
):
    """Bulk-create notifications for all active staff."""
    staff_users = list(
        User.objects.filter(is_staff=True, is_active=True)
        .only('id', 'email', 'first_name')
    )
    if not staff_users:
        return []

    content_type, object_id = _extract_content_type_and_id(link)

    notifications = [
        Notification(
            recipient=user,
            title=title,
            message=message,
            link=link or None,
            notification_type=notif_type,
            category=category,
            content_type=content_type,
            object_id=object_id,
        )
        for user in staff_users
    ]

    try:
        with transaction.atomic():
            created = Notification.objects.bulk_create(notifications)
    except Exception:
        logger.exception("Bulk staff notification failed | title=%s", title)
        return []

    if send_email:
        for user in staff_users:
            if user.email:
                _send_notification_email(user, title, message, link)

    return created


# ════════════════════════════════════════════════════════════
# CONTACT-BASED NOTIFICATIONS (customer / vendor / both)
# ════════════════════════════════════════════════════════════

def send_notification_to_contact(
    contact, title, message, link=None,
    notif_type='info', category='general', send_email=False,
):
    """Send notification to a Contact's linked user (any contact_type)."""
    if not contact:
        return None
    linked_user = getattr(contact, 'user', None)
    if not linked_user:
        logger.debug(
            "Notification skipped — Contact #%s has no linked user",
            getattr(contact, 'id', '?'),
        )
        return None
    return send_notification(
        user=linked_user,
        title=title, message=message, link=link,
        notif_type=notif_type, category=category, send_email=send_email,
    )


# ── Backward-compatible aliases ──────────────────────────────
send_notification_to_customer = send_notification_to_contact
send_notification_to_all_staff = send_notification_to_staff


# ════════════════════════════════════════════════════════════
# SSE STUB (no-op) — kept for backward compatibility
# ════════════════════════════════════════════════════════════

def send_notification_sse(user):
    """
    NO-OP STUB — kept for backward compatibility.

    Previously this triggered an in-process SSE event via
    django-eventstream. That approach does NOT work on Render
    free tier (multi-worker Gunicorn: worker A's event never
    reaches worker B's client).

    Real-time badge/dropdown refresh is now handled by HTMX
    polling in the base templates (every 30-60 seconds).

    This function exists only so legacy imports
    (accounting/views/payments.py, accounting/models.py, etc.)
    don't crash. It does nothing.
    """
    return None


# ════════════════════════════════════════════════════════════
# UNREAD COUNT HELPERS
# ════════════════════════════════════════════════════════════

def get_unread_count(user):
    """Fast unread count. Returns 0 on any error."""
    if not user or not getattr(user, 'is_authenticated', False):
        return 0
    try:
        return user.notifications.filter(is_read=False).count()
    except Exception:
        logger.exception("Unread count failed | user_id=%s", user.id)
        return 0